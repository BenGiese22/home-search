"""Query-count discipline for score.py.

score.py used to issue three SELECTs per listing (amenities, commute,
visual score) plus one INSERT per listing. Free against local SQLite, which
is why it survived so long -- but every Turso statement is a ~240ms HTTP
round-trip, so 129 listings meant ~93 seconds of pure latency the day the
database moved. These tests pin the set-at-a-time shape so the per-listing
pattern cannot silently come back.
"""
import sqlite3
from pathlib import Path

import pytest

import score
from src.commute import CommuteResult
from src.db import (
    _SCHEMA,
    get_amenities,
    get_amenities_by_listing,
    get_commute,
    get_commutes_by_listing,
    get_connection,
    get_scores,
    get_visual_score,
    get_visual_scores_by_listing,
    upsert_commute,
    upsert_listing,
    upsert_score,
    upsert_scores,
)
from src.models import Listing
from src.scoring import ScoreResult
from src.vision import VisualScoreResult


def _listing(n: int) -> Listing:
    return Listing(
        listing_id=f"L{n:04d}",
        address=f"{n} Test St",
        city="Arvada",
        state="CO",
        zip_code="80003",
        price="$600,000",
        beds=3,
        baths=2.0,
        sqft=2000,
        lot_sqft=7000,
        parking_spaces=2,
        year_built=2000,
        description="desc",
        amenities=[f"Amenity {n}A", f"Amenity {n}B"],
        photo_urls=["https://example.com/1.jpg"],
        listing_url=f"https://example.com/{n}",
    )


def _score_result(composite: float = 7.0) -> ScoreResult:
    return ScoreResult(
        commute_score=1.0,
        sqft_score=2.0,
        condition_score=3.0,
        outdoor_score=4.0,
        room_count_score=5.0,
        parking_score=6.0,
        hoa_score=7.0,
        composite=composite,
        passes_filters=True,
        has_incomplete_data=False,
    )


def _seed(conn: sqlite3.Connection, count: int) -> None:
    for n in range(count):
        listing = _listing(n)
        upsert_listing(conn, listing)
        upsert_commute(
            conn,
            listing.listing_id,
            CommuteResult(
                lat=39.8, lon=-105.1,
                denver_miles=14.0, denver_minutes=24.0,
                medtronic_miles=9.0, medtronic_minutes=18.0,
                geocode_failed=False,
            ),
        )
        from src.db import upsert_visual_score
        upsert_visual_score(
            conn,
            listing.listing_id,
            VisualScoreResult(
                condition_photo_score=8.0,
                outdoor_photo_score=6.0,
                has_layout_plan=False,
                layout_plan_clarity_score=None,
                garage_attached=True,
                watermarked_staging_detected=False,
                suspected_unwatermarked_staging=False,
                staging_notes=None,
            ),
        )


# --- the set-at-a-time readers -------------------------------------------

def test_get_amenities_by_listing_matches_per_listing_reads(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 3)

    batched = get_amenities_by_listing(conn)

    for n in range(3):
        listing_id = f"L{n:04d}"
        assert batched[listing_id] == get_amenities(conn, listing_id)


def test_get_amenities_by_listing_uses_one_statement(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 25)

    with _counted(conn) as count:
        get_amenities_by_listing(conn)

    assert count() == 1


def test_get_amenities_by_listing_covers_listings_with_no_amenities(tmp_path: Path):
    """A listing with no amenity rows must still appear, with an empty list --
    otherwise score.py's dict lookup raises KeyError instead of scoring it."""
    conn = get_connection(tmp_path / "db.sqlite")
    listing = _listing(1)
    listing.amenities = []
    upsert_listing(conn, listing)

    assert get_amenities_by_listing(conn)[listing.listing_id] == []


def test_get_commutes_by_listing_matches_per_listing_reads(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 3)

    batched = get_commutes_by_listing(conn)

    for n in range(3):
        listing_id = f"L{n:04d}"
        expected = get_commute(conn, listing_id)
        assert dict(batched[listing_id]) == dict(expected)


def test_get_commutes_by_listing_uses_one_statement(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 25)

    with _counted(conn) as count:
        get_commutes_by_listing(conn)

    assert count() == 1


def test_get_visual_scores_by_listing_matches_per_listing_reads(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 3)

    batched = get_visual_scores_by_listing(conn)

    for n in range(3):
        listing_id = f"L{n:04d}"
        expected = get_visual_score(conn, listing_id)
        assert dict(batched[listing_id]) == dict(expected)


def test_get_visual_scores_by_listing_uses_one_statement(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 25)

    with _counted(conn) as count:
        get_visual_scores_by_listing(conn)

    assert count() == 1


def test_missing_child_rows_are_absent_rather_than_raising(tmp_path: Path):
    """Listings with no commute/visual row must simply not be in the dict, so
    callers can use .get() and fall back, exactly as the per-listing readers
    returned None."""
    conn = get_connection(tmp_path / "db.sqlite")
    upsert_listing(conn, _listing(1))

    assert get_commutes_by_listing(conn) == {}
    assert get_visual_scores_by_listing(conn) == {}


# --- the batched write ----------------------------------------------------

def test_upsert_scores_writes_every_row(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 5)

    upsert_scores(conn, [(f"L{n:04d}", _score_result(float(n))) for n in range(5)])

    rows = get_scores(conn)
    assert len(rows) == 5
    assert {row["listing_id"] for row in rows} == {f"L{n:04d}" for n in range(5)}


def test_upsert_scores_matches_upsert_score_field_for_field(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 1)
    result = _score_result()

    upsert_score(conn, "L0000", result)
    singular = dict(get_scores(conn)[0])

    upsert_scores(conn, [("L0000", result)])
    batched = dict(get_scores(conn)[0])

    singular.pop("computed_at")
    batched.pop("computed_at")
    assert singular == batched


def test_upsert_scores_replaces_rather_than_accumulating(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 3)
    rows = [(f"L{n:04d}", _score_result(1.0)) for n in range(3)]

    upsert_scores(conn, rows)
    upsert_scores(conn, [(lid, _score_result(9.0)) for lid, _ in rows])

    scores = get_scores(conn)
    assert len(scores) == 3
    assert {row["composite"] for row in scores} == {9.0}


def test_upsert_scores_statement_count_does_not_grow_per_row(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")
    _seed(conn, 60)

    with _counted(conn) as count:
        upsert_scores(conn, [(f"L{n:04d}", _score_result()) for n in range(60)])

    # 60 rows chunked -- a handful of statements, nowhere near one per row.
    assert count() <= 8


def test_upsert_scores_on_empty_input_is_a_no_op(tmp_path: Path):
    conn = get_connection(tmp_path / "db.sqlite")

    with _counted(conn) as count:
        upsert_scores(conn, [])

    assert count() == 0


# --- the end-to-end invariant --------------------------------------------

def test_score_main_statement_count_is_independent_of_listing_count(
    tmp_path: Path, monkeypatch, capsys
):
    """The real regression guard: scoring 30 listings must cost the same
    number of SQL statements as scoring 3. Any reintroduced per-listing
    query or write shows up here as a growing count."""
    counts = {}
    for size in (3, 30):
        db_path = tmp_path / f"db-{size}.sqlite"
        conn = get_connection(db_path)
        _seed(conn, size)

        counter = {"n": 0}
        conn.set_trace_callback(lambda _sql, c=counter: c.__setitem__("n", c["n"] + 1))
        monkeypatch.setattr(score, "stage_connection", lambda c=conn: c)
        monkeypatch.setattr(score, "RANKED_CSV_PATH", tmp_path / f"ranked-{size}.csv")
        monkeypatch.setattr(score.sys, "argv", ["score.py"])

        score.main()
        counts[size] = counter["n"]

    assert counts[3] == counts[30], (
        f"statement count grew with listing count: {counts}"
    )


class _counted:
    """Counts SQL statements executed on a sqlite3 connection, which is the
    unit that maps 1:1 to a Turso HTTP round-trip."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.n = 0

    def __enter__(self):
        self.conn.set_trace_callback(self._bump)
        return lambda: self.n

    def _bump(self, _sql: str) -> None:
        self.n += 1

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


# --- a stale commute row is not scored -----------------------------------
#
# Merge order is a hard constraint in the commute rebuild: the stage that
# writes traffic-aware durations has to reach production before the scorer
# stops weighting the Denver leg. These tests are what makes getting that
# wrong loud rather than silent. A free-flow duration against a curve drawn
# for rush hour scores nearly every listing 100 -- so scoring the two kinds
# of row together would rank the un-recomputed half above the recomputed
# half and look entirely normal doing it.


def _score_one(tmp_path: Path, monkeypatch, source):
    """Seed a single listing whose commute row carries `source`, run the real
    score.main(), and hand back its scores row."""
    from src.commute import CommuteResult

    import dataclasses

    db_path = tmp_path / f"stale-{source}.sqlite"
    conn = get_connection(db_path)
    # A disclosed HOA, so has_incomplete_data reflects the commute and only
    # the commute. _listing() leaves hoa_annual None, which legitimately
    # raises the flag on its own and would make every assertion below pass
    # for the wrong reason.
    listing = dataclasses.replace(_listing(0), hoa_annual=0.0)
    upsert_listing(conn, listing)
    upsert_commute(
        conn,
        listing.listing_id,
        CommuteResult(
            lat=39.8, lon=-105.1,
            denver_miles=14.0, denver_minutes=24.0,
            medtronic_miles=9.0, medtronic_minutes=18.0,
            geocode_failed=False,
            commute_source=source,
        ),
    )
    monkeypatch.setattr(score, "stage_connection", lambda c=conn: c)
    monkeypatch.setattr(score, "RANKED_CSV_PATH", tmp_path / f"ranked-{source}.csv")
    monkeypatch.setattr(score.sys, "argv", ["score.py"])
    score.main()
    # main() closes the connection it was handed, so read the result back
    # through a fresh one rather than reaching into a closed handle.
    return get_connection(db_path).execute(
        "SELECT * FROM scores WHERE listing_id = ?", (listing.listing_id,)
    ).fetchone()


def test_a_current_source_commute_is_scored(tmp_path: Path, monkeypatch, capsys):
    from src.commute import COMMUTE_SOURCE

    row = _score_one(tmp_path, monkeypatch, COMMUTE_SOURCE)
    # 18 minutes is inside the flat region of the curve.
    assert row["commute_score"] == 100.0
    assert row["has_incomplete_data"] == 0


def test_a_row_measured_a_different_way_scores_as_missing(
    tmp_path: Path, monkeypatch, capsys
):
    row = _score_one(tmp_path, monkeypatch, "osrm-freeflow/v1")
    assert row["commute_score"] == 50.0
    assert row["has_incomplete_data"] == 1


def test_a_row_from_before_the_source_column_scores_as_missing(
    tmp_path: Path, monkeypatch, capsys
):
    """Every row the free-flow pipeline wrote has commute_source NULL. If the
    scorer ran before the commutes stage had migrated them, this is what
    stops 18 free-flow minutes being read as a 20-minute rush-hour drive."""
    row = _score_one(tmp_path, monkeypatch, None)
    assert row["commute_score"] == 50.0
    assert row["has_incomplete_data"] == 1
