"""Recording what changed, so a later stage can report it.

The report has to outlive the stage that produces it. `scrape` knows what is
new; rank and composite do not exist until `score` has run; and a digest
worth reading needs both. Stages here talk through the database, so this is
the seam.
"""

import sqlite3

import pytest

from src.db import (
    KIND_DELISTED,
    KIND_NEW,
    KIND_PRICE,
    delete_orphaned_rows,
    mark_change_events_notified,
    record_change_events,
    unnotified_change_events,
)
from src.turso_db import ensure_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def test_events_are_recorded_and_come_back_unnotified(conn):
    record_change_events(conn, [(KIND_NEW, "L1", None), (KIND_PRICE, "L2", "$599,000 -> $579,000")])

    rows = unnotified_change_events(conn)

    assert {r["kind"] for r in rows} == {KIND_NEW, KIND_PRICE}
    assert all(r["notified_at"] is None for r in rows)


def test_recording_the_same_change_twice_in_a_day_is_one_event(conn):
    """A stage that runs twice over the same fetch must not produce a second
    email about the same price drop."""
    for _ in range(2):
        record_change_events(conn, [(KIND_PRICE, "L2", "$599,000 -> $579,000")])

    assert len(unnotified_change_events(conn)) == 1


def test_notified_events_stop_coming_back(conn):
    record_change_events(conn, [(KIND_NEW, "L1", None)])
    rows = unnotified_change_events(conn)

    mark_change_events_notified(conn, [r["event_id"] for r in rows])

    assert unnotified_change_events(conn) == []


def test_nothing_is_stamped_until_a_send_succeeds(conn):
    """The stamp is the only thing that prevents a re-send, so a failed email
    has to leave the events exactly where they were."""
    record_change_events(conn, [(KIND_NEW, "L1", None)])

    mark_change_events_notified(conn, [])

    assert len(unnotified_change_events(conn)) == 1


def test_the_record_survives_the_listing_it_describes(conn):
    """A delisting event is ABOUT a row that has just been deleted. A foreign
    key would either block the delete or take the record away with it."""
    record_change_events(conn, [(KIND_DELISTED, "gone-forever", "12651 James Circle")])

    rows = unnotified_change_events(conn)

    assert rows[0]["listing_ref"] == "gone-forever"


def test_the_orphan_sweeper_does_not_claim_change_events(conn):
    """The reason the column is `listing_ref` and not `listing_id`.

    delete_orphaned_rows discovers child tables by that column name and
    prunes rows whose listing is gone -- which would delete every delisting
    event, the one kind that exists precisely because the listing is gone.
    Naming it differently makes the table invisible to that sweep, with no
    exclusion list to keep in step."""
    record_change_events(conn, [(KIND_DELISTED, "gone-forever", "an address")])

    delete_orphaned_rows(conn)

    assert len(unnotified_change_events(conn)) == 1


def test_change_events_is_not_in_the_delete_cascade():
    """Enrolling it would mean a delisting deletes its own record of itself."""
    from src.db import tables_child_first

    assert "change_events" not in tables_child_first(extra_tables=("hosted_photos",))


def test_recording_nothing_writes_nothing(conn):
    assert record_change_events(conn, []) == 0
    assert unnotified_change_events(conn) == []
