"""A vision-batch result naming a listing that no longer exists must not
crash the run.

2026-09-19, production traceback: a vision-scoring batch submitted before an
11-day pipeline outage was resumed and processed on the first run back. One
of its results named a listing_id that had since been rejected and deleted
from `listings`. Writing that result via `upsert_visual_score` hit
`visual_scores`' foreign key to `listings` and raised an uncaught
`IntegrityError`, killing the entire pipeline run.

This recurs by construction: `vision_batches` is deliberately excluded from
the delisting cascade (see the comment on it in src/db.py) so that deleting
a listing doesn't destroy the record of a batch other listings are still
waiting on -- which means any listing rejected while its batch is in flight
hits this exact crash the next time results are processed.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from score_photos import _process_batch_results
from src.db import delete_listing, upsert_visual_score
from src.turso_db import ensure_schema

FULL_RESPONSE = {
    "kitchen": {"status": "present", "score": 8, "notes": "Updated cabinets, newer appliances."},
    "bathrooms": {"status": "present", "score": 6, "notes": "Original tile, dated but clean."},
    "living_space": {"status": "present", "score": 7, "notes": "Open and bright."},
    "basement": {"status": "present", "score": 5, "notes": "Finished but low ceilings."},
    "garage": {
        "status": "present", "score": 4, "notes": "Detached, smaller door.",
        "attached": False,
    },
    "staging_flags": {
        "watermarked_staging_detected": False,
        "suspected_unwatermarked_staging": False,
        "notes": "No staging concerns noticed.",
    },
    "backyard": {
        "present": True, "tree_coverage": 8, "hosting_suitability": 6,
        "notes": "Large shaded patio.",
    },
    "layout_plan": {"present": True, "clarity_score": 9, "notes": "Crisp labeled floor plan photo."},
}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def add(conn, lid):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, property_type, localized_status
           ) VALUES (?, 'A St', 'Arvada', 'CO', '80003', '$1', 3, 2.0, 1800,
                     7000, 2, 1990, 'd', 'https://x/l', 'Single Family', 'Active')""",
        (lid,),
    )
    conn.commit()


def succeeded(listing_id, payload):
    return SimpleNamespace(custom_id=listing_id, result=SimpleNamespace(
        type="succeeded",
        message=SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))]),
    ))


def errored(listing_id):
    return SimpleNamespace(custom_id=listing_id, result=SimpleNamespace(type="errored"))


def client_returning(results):
    return SimpleNamespace(messages=SimpleNamespace(
        batches=SimpleNamespace(results=lambda batch_id: iter(results))
    ))


def _row(conn, lid):
    return conn.execute(
        "SELECT * FROM visual_scores WHERE listing_id = ?", (lid,)
    ).fetchone()


def test_the_fixture_enforces_the_foreign_key_like_turso(conn):
    """Sanity check that this fixture is actually exercising the real bug and
    not passing on a lenient connection -- the default sqlite3 connection
    does not enforce foreign keys at all."""
    with pytest.raises(sqlite3.IntegrityError):
        upsert_visual_score(conn, "ghost", None)


def test_a_result_for_a_listing_that_no_longer_exists_is_discarded_not_fatal(conn, capsys):
    add(conn, "L1")
    add(conn, "L2")
    add(conn, "L3")
    delete_listing(conn, "L2")

    # Stale-first on purpose: proves a stale result doesn't take down the
    # rest of the batch.
    results = [
        succeeded("L2", FULL_RESPONSE),
        succeeded("L1", FULL_RESPONSE),
        errored("L3"),
    ]
    client = client_returning(results)

    _process_batch_results(
        client, conn, "batch_1",
        garage_expected_by_id={"L1": True, "L2": True, "L3": False},
    )

    l1 = _row(conn, "L1")
    assert l1["photo_score_unavailable"] == 0
    assert l1["condition_photo_score"] is not None

    l3 = _row(conn, "L3")
    assert l3["photo_score_unavailable"] == 1

    assert _row(conn, "L2") is None

    out = capsys.readouterr().out
    assert "L2" in out
    assert "no longer" in out


def test_a_stale_result_that_failed_at_the_api_is_also_discarded(conn):
    add(conn, "L1")
    delete_listing(conn, "L1")

    client = client_returning([errored("L1")])

    _process_batch_results(client, conn, "batch_1", garage_expected_by_id={"L1": True})

    assert _row(conn, "L1") is None


def test_a_stale_result_that_does_not_parse_is_also_discarded(conn):
    """Guards the pre-existing except-handler write path, which would ALSO
    hit the foreign key if the liveness check weren't ahead of the try
    block."""
    add(conn, "L1")
    delete_listing(conn, "L1")

    client = client_returning([succeeded("L1", {"garbage": True})])

    _process_batch_results(client, conn, "batch_1", garage_expected_by_id={"L1": True})

    assert _row(conn, "L1") is None


def test_discarded_stale_results_are_counted_once_per_batch(conn, capsys):
    """A long outage can leave a batch naming many deleted listings. One line
    each is noise; the count is what an operator actually reads."""
    add(conn, "L1")
    for lid in ("L2", "L3"):
        add(conn, lid)
        delete_listing(conn, lid)

    client = client_returning([
        succeeded("L2", FULL_RESPONSE),
        succeeded("L1", FULL_RESPONSE),
        errored("L3"),
    ])

    _process_batch_results(
        client, conn, "batch_1",
        garage_expected_by_id={"L1": True, "L2": True, "L3": True},
    )

    out = capsys.readouterr().out
    assert "batch batch_1: discarded 2 result(s) for listings no longer in listings" in out


def test_no_stale_summary_when_nothing_was_discarded(conn, capsys):
    add(conn, "L1")

    _process_batch_results(
        client_returning([succeeded("L1", FULL_RESPONSE)]), conn, "batch_1",
        garage_expected_by_id={"L1": True},
    )

    assert "discarded" not in capsys.readouterr().out


def test_liveness_costs_one_statement_per_batch_not_per_result(conn):
    for lid in ("L1", "L2", "L3", "L4", "L5"):
        add(conn, lid)

    results = [succeeded(lid, FULL_RESPONSE) for lid in ("L1", "L2", "L3", "L4", "L5")]
    client = client_returning(results)
    garage_expected_by_id = {lid: True for lid in ("L1", "L2", "L3", "L4", "L5")}

    statements = []
    conn.set_trace_callback(statements.append)
    try:
        _process_batch_results(client, conn, "batch_1", garage_expected_by_id)
    finally:
        conn.set_trace_callback(None)

    liveness_selects = [
        s for s in statements if s.strip().upper().startswith("SELECT LISTING_ID FROM LISTINGS")
    ]
    assert len(liveness_selects) == 1
