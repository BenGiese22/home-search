"""The invariants a finished run must satisfy.

These exist because of a failure mode this project keeps reproducing: a run
that exits 0, returns 200, writes plausible row counts, raises nothing, and
has quietly done the wrong thing. Three defects in a row had that shape.

Every check here converts one such silence into a non-zero exit code, which
is the signal every layer above already reacts to.
"""
import sqlite3

import pytest

from verify import (
    CHECKS,
    check_addresses_are_unique,
    check_active_listings_have_photos,
    check_corpus_is_not_empty,
    check_every_listing_is_scored,
    check_no_orphaned_children,
    main,
    run_checks,
)
from src.turso_db import ensure_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


_CURRENT_SOURCE = "mapbox-arrive-0815/v1"
_UNSET = object()


def add_listing(
    conn,
    lid,
    address="A House",
    urls=0,
    hosted=0,
    scored=True,
    commute=True,
    commute_source=_UNSET,
    medtronic_minutes=18.0,
):
    # The schema is almost entirely NOT NULL, so a fixture has to be complete.
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, is_pinned, property_type, localized_status
           ) VALUES (?, ?, 'Arvada', 'CO', '80003', '$500,000', 3, 2.0,
                     1800, 7000, 2, 1990, 'a house',
                     'https://x/l', 0, 'Single Family', 'Active')""",
        (lid, address),
    )
    for i in range(urls):
        conn.execute(
            "INSERT INTO photo_urls (listing_id, position, url) VALUES (?, ?, ?)",
            (lid, i, f"https://x/{lid}/{i}"),
        )
    for i in range(hosted):
        conn.execute(
            "INSERT INTO hosted_photos (listing_id, position, blob_url) VALUES (?, ?, ?)",
            (lid, i + 1, f"https://blob/{lid}/{i}"),
        )
    if commute:
        conn.execute(
            """INSERT INTO commute (
                 listing_id, lat, lon, denver_miles, denver_minutes,
                 medtronic_miles, medtronic_minutes, geocode_failed,
                 computed_at, commute_source, arrive_by, route_error
               ) VALUES (?, 39.8, -105.1, 14.0, 24.0, 9.0, ?, 0,
                         '2026-09-05T00:00:00+00:00', ?, '2026-09-09T08:15', NULL)""",
            (
                lid,
                medtronic_minutes,
                _CURRENT_SOURCE if commute_source is _UNSET else commute_source,
            ),
        )
    if scored:
        conn.execute(
            """INSERT INTO scores (
                 listing_id, commute_score, sqft_score, condition_score,
                 outdoor_score, room_count_score, parking_score, hoa_score,
                 composite, passes_filters, has_incomplete_data, computed_at
               ) VALUES (?, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0,
                         50.0, 1, 0, '2026-09-05T00:00:00+00:00')""",
            (lid,),
        )
    conn.commit()


# --- the open bug this was written for ------------------------------------

def test_a_listing_with_urls_but_no_hosted_photos_is_a_violation(conn):
    """Issue #70. An orphaned pin sat in exactly this state while every run
    reported success, because no stage is responsible for noticing that work
    it had identified as pending never happened."""
    add_listing(conn, "L1", "5012 West 77th Drive", urls=37, hosted=0)

    v = check_active_listings_have_photos(conn)

    assert v is not None
    assert "5012 West 77th Drive" in v.rows[0]


def test_a_fully_hosted_listing_passes(conn):
    add_listing(conn, "L1", urls=37, hosted=37)
    assert check_active_listings_have_photos(conn) is None


def test_a_listing_with_no_photo_urls_at_all_is_not_a_violation(conn):
    """Nothing to host is not the same as failing to host."""
    add_listing(conn, "L1", urls=0, hosted=0)
    assert check_active_listings_have_photos(conn) is None


# --- the catastrophic case ------------------------------------------------

def test_an_empty_corpus_is_a_violation(conn):
    """The cheapest check and the worst failure. A scrape returning nothing --
    expired session, changed selector, WAF block -- can delist everything, and
    every stage downstream then succeeds perfectly against zero rows."""
    assert check_corpus_is_not_empty(conn) is not None


def test_a_populated_corpus_passes(conn):
    add_listing(conn, "L1")
    assert check_corpus_is_not_empty(conn) is None


# --- scores ---------------------------------------------------------------

def test_an_unscored_listing_is_a_violation(conn):
    """The viewer ranks on scores, so a listing without a row is invisible to
    the ordering rather than merely last."""
    add_listing(conn, "L1", "No Score Lane", scored=False)

    v = check_every_listing_is_scored(conn)

    assert v is not None and "No Score Lane" in v.rows[0]


# --- orphans --------------------------------------------------------------

def test_orphaned_child_rows_are_a_violation(conn):
    """Orphaned hosted_photos are the expensive kind: each is a blob nothing
    will ever delete."""
    add_listing(conn, "L1", urls=2, hosted=2)
    conn.execute("DELETE FROM listings WHERE listing_id='L1'")
    conn.commit()

    v = check_no_orphaned_children(conn)

    assert v is not None
    assert any("hosted_photos" in row for row in v.rows)


def test_a_consistent_database_has_no_orphans(conn):
    add_listing(conn, "L1", urls=2, hosted=2)
    assert check_no_orphaned_children(conn) is None


# --- exit codes -----------------------------------------------------------

def test_a_clean_database_exits_zero(conn, capsys):
    add_listing(conn, "L1", urls=3, hosted=3)

    assert main([], conn=conn) == 0
    assert "all invariants hold" in capsys.readouterr().out


def test_a_violation_exits_non_zero(conn):
    """The whole point. A semantic error becomes an exit code, which
    pipeline.py stops on, run.py records in the done marker, and the reaper
    turns into a push notification."""
    add_listing(conn, "L1", urls=37, hosted=0)

    assert main([], conn=conn) == 1


def test_warn_mode_reports_without_failing(conn, capsys):
    """For a first deploy, where the invariants are known to be violated by
    pre-existing data and failing every run would just train people to ignore
    it."""
    add_listing(conn, "L1", urls=37, hosted=0)

    assert main(["--warn"], conn=conn) == 0
    assert "not failing the run" in capsys.readouterr().out


def test_every_check_is_registered(conn):
    """A check that exists but is not in CHECKS runs never and protects
    nothing."""
    import verify

    defined = {
        v for k, v in vars(verify).items()
        if k.startswith("check_") and callable(v)
    }
    assert defined == set(CHECKS)


def test_checks_report_each_result_by_name(conn, capsys):
    add_listing(conn, "L1", urls=1, hosted=1)

    run_checks(conn)

    out = capsys.readouterr().out
    for check in CHECKS:
        assert check.__name__ in out


def test_two_listings_at_one_address_is_a_violation(conn):
    """Two rows for one address means Compass reissued the listing under a
    new id -- a relist. The cost is not cosmetic: the duplicate is scored
    separately, appears twice in the ranking, and has been paid for twice at
    the vision API, because visual_scores is keyed on listing_id and knows
    nothing about addresses."""
    add_listing(conn, "old", "12651 James Circle")
    add_listing(conn, "new", "12651 James Circle")

    v = check_addresses_are_unique(conn)

    assert v is not None
    assert "12651 James Circle" in v.rows[0]


def test_distinct_addresses_pass(conn):
    add_listing(conn, "a", "1 Main St")
    add_listing(conn, "b", "2 Oak Ave")
    assert check_addresses_are_unique(conn) is None


# --- the commute invariants -----------------------------------------------
#
# We use Mapbox knowingly against its terms (docs/routing-provider-terms.md),
# so the realistic failure is not legal, it is operational: a key switched off
# at 3am. After that, new listings never get a current-source commute row and
# the ranking goes quietly wrong for exactly the newest listings -- which are
# the ones anyone is actually looking at. These two checks are what turns that
# into an exit code instead of a slow drift nobody can see.


def test_a_listing_the_commutes_stage_never_reached_is_a_violation(conn):
    add_listing(conn, "L1", urls=3, hosted=3, commute=False)

    violations = run_checks(conn)

    assert [v.check for v in violations] == ["listings_without_a_commute_row"]


def test_a_recorded_routing_failure_is_not_a_violation(conn):
    """A row with NULL minutes is an honest answer: the stage asked and could
    not route it. That listing scores on the neutral fallback and is flagged
    incomplete, which is the system working. The violation is a listing
    nothing even tried."""
    add_listing(conn, "L1", urls=3, hosted=3, medtronic_minutes=None)

    assert run_checks(conn) == []


def test_a_corpus_measured_two_different_ways_is_a_violation(conn):
    """The reason commute_source exists. An 18-minute free-flow drive scores
    100 where the same drive measured at 08:15 scores 88, so a half-migrated
    corpus ranks the un-recomputed listings above the recomputed ones -- with
    complete rows, plausible numbers and an exit code of 0."""
    add_listing(conn, "L1", address="One", urls=3, hosted=3)
    add_listing(conn, "L2", address="Two", urls=3, hosted=3, commute_source=None)

    violations = run_checks(conn)

    assert [v.check for v in violations] == ["mixed_commute_sources"]
    assert "more than one kind" in violations[0].detail


def test_a_corpus_measured_entirely_the_old_way_is_a_violation(conn):
    """One source, but the wrong one. This is what a deploy looks like if the
    commutes stage never ran -- uniform, complete, and answering a question
    nobody asked."""
    add_listing(conn, "L1", urls=3, hosted=3, commute_source=None)

    violations = run_checks(conn)

    assert [v.check for v in violations] == ["mixed_commute_sources"]
    assert "None" in violations[0].detail


def test_a_corpus_on_the_current_source_holds(conn):
    add_listing(conn, "L1", address="One", urls=3, hosted=3)
    add_listing(conn, "L2", address="Two", urls=3, hosted=3)

    assert run_checks(conn) == []


def test_an_unrouted_row_does_not_break_uniformity(conn):
    """A row that routed nothing claims nothing about how it was measured, so
    it must not be counted as a second source -- otherwise one transient
    routing failure fails the whole run."""
    add_listing(conn, "L1", address="One", urls=3, hosted=3)
    add_listing(
        conn, "L2", address="Two", urls=3, hosted=3,
        commute_source=None, medtronic_minutes=None,
    )

    assert run_checks(conn) == []


def test_an_empty_commute_table_does_not_fail_uniformity_on_its_own(conn):
    """With no listings there is nothing to be inconsistent about, and the
    empty-corpus check already speaks to that case."""
    assert [v.check for v in run_checks(conn)] == ["empty_corpus"]
