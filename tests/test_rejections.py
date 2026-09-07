"""Rejecting a house, without deleting everything we know about it.

Marking a listing "not interested" on Compass moves it out of both fetched
tabs, so today it lands in the delisting cascade: the row, its photos, their
Blob objects and its paid vision score all go, and the digest reports it as
DELISTED -- indistinguishable from a house that sold.

Both halves of that are wrong. A rejection is not news, and it is not a
reason to throw away work we have paid for.
"""

import sqlite3

import pytest

from src.db import (
    delete_orphaned_rows,
    is_rejected,
    mark_rejections_synced,
    reject_property,
    rejected_property_ids,
    rejections_pending_compass_sync,
    unreject_property,
    upsert_property_id,
)
from src.models import Listing, select_present_listings
from src.turso_db import ensure_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def listing(lid, status="Active"):
    return Listing(
        listing_id=lid, address="1 Test St", city="Arvada", state="CO",
        zip_code="80003", price="$1", beds=3, baths=2.0, sqft=1800,
        lot_sqft=7000, parking_spaces=2, year_built=1990, description="d",
        amenities=[], photo_urls=[], listing_url="https://x/l",
        localized_status=status,
    )


def add(conn, lid, pid=None):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, property_type, localized_status
           ) VALUES (?, '1 Test St', 'Arvada', 'CO', '80003', '$1', 3, 2.0,
                     1800, 7000, 2, 1990, 'd', 'https://x/l',
                     'Single Family', 'Active')""",
        (lid,),
    )
    if pid:
        upsert_property_id(conn, lid, pid)
    conn.commit()


# --- the record ----------------------------------------------------------


def test_a_rejection_is_recorded_against_the_property(conn):
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", reason="backyard is dirt")

    assert rejected_property_ids(conn) == frozenset({"131FZM"})
    assert is_rejected(conn, "131FZM") is True


def test_a_rejection_survives_the_listing_it_was_made_against(conn):
    """The whole reason it is keyed on the property. Compass keys its own
    notInterested on the LISTING id, so a relist mints a new one and the
    rejection is forgotten -- we would re-scrape, re-photograph and re-pay for
    vision scoring on a house Ben has already said no to."""
    add(conn, "old", pid="131FZM")
    reject_property(conn, "131FZM")

    conn.execute("DELETE FROM listings WHERE listing_id = 'old'")
    conn.commit()
    add(conn, "new_lid_after_relist", pid="131FZM")

    assert is_rejected(conn, "131FZM") is True


def test_rejecting_twice_is_not_an_error(conn):
    reject_property(conn, "131FZM")
    reject_property(conn, "131FZM", reason="still no")
    assert rejected_property_ids(conn) == frozenset({"131FZM"})


def test_a_rejection_can_be_undone(conn):
    reject_property(conn, "131FZM")
    unreject_property(conn, "131FZM")
    assert rejected_property_ids(conn) == frozenset()


def test_unrejecting_something_never_rejected_is_not_an_error(conn):
    unreject_property(conn, "never-seen")
    assert rejected_property_ids(conn) == frozenset()


def test_the_reason_is_kept(conn):
    reject_property(conn, "131FZM", reason="backyard is dirt")
    row = conn.execute(
        "SELECT reason FROM rejections WHERE property_id = '131FZM'"
    ).fetchone()
    assert row["reason"] == "backyard is dirt"


def test_the_table_is_not_enrolled_in_the_delete_cascade():
    """A rejection must outlive every listing of the house it is about.
    An FK to listings would enrol it in the cascade it exists to survive --
    the same reasoning that keeps change_events and vision_batches out."""
    from src.db import tables_child_first

    assert "rejections" not in tables_child_first(extra_tables=("hosted_photos",))


# --- suppression, not deletion -------------------------------------------


def test_a_rejected_property_is_dropped_from_the_present_set(conn):
    """Suppressed at the one chokepoint scrape.py and check.py share, so the
    two can never drift about what counts as present."""
    present = select_present_listings(
        [listing("L1"), listing("L2")],
        rejected_property_ids=frozenset({"131FZM"}),
        property_id_by_listing={"L1": "131FZM"},
    )
    assert [l.listing_id for l in present] == ["L2"]


def test_an_active_listing_with_no_property_id_is_unaffected(conn):
    """Property ids are resolved after the fact, so a listing that arrived
    this run may not have one yet. It must not be suppressed by accident --
    an unknown property is not a rejected one."""
    present = select_present_listings(
        [listing("L1")],
        rejected_property_ids=frozenset({"131FZM"}),
        property_id_by_listing={},
    )
    assert [l.listing_id for l in present] == ["L1"]


def test_rejection_beats_the_favorite_exemption(conn):
    """A favorite that has gone Pending is normally protected. An explicit
    rejection is the more recent and more specific statement of intent."""
    present = select_present_listings(
        [listing("L1", status="Pending")],
        favorite_ids=frozenset({"L1"}),
        rejected_property_ids=frozenset({"131FZM"}),
        property_id_by_listing={"L1": "131FZM"},
    )
    assert present == []


def test_nothing_changes_when_nothing_is_rejected(conn):
    present = select_present_listings([listing("L1"), listing("L2")])
    assert [l.listing_id for l in present] == ["L1", "L2"]


# --- legibility after the listing is gone ---------------------------------


def test_the_address_is_kept_on_the_rejection(conn):
    """A rejection outlives every listing of the house it is about, so by the
    time anyone reads one back there is nothing left to join to. Without this
    the record is six opaque characters and a date."""
    reject_property(
        conn, "131FZM", reason="backyard is dirt",
        address="5012 West 77th Drive", city="Westminster",
        listing_url="https://compass.com/x",
    )
    row = conn.execute("SELECT * FROM rejections WHERE property_id='131FZM'").fetchone()
    assert row["address"] == "5012 West 77th Drive"
    assert row["city"] == "Westminster"
    assert row["listing_url"] == "https://compass.com/x"


def test_the_address_survives_the_listing_being_deleted(conn):
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", address="5012 West 77th Drive", city="Westminster")
    conn.execute("DELETE FROM listings WHERE listing_id='L1'")
    conn.commit()

    row = conn.execute("SELECT address FROM rejections WHERE property_id='131FZM'").fetchone()
    assert row["address"] == "5012 West 77th Drive"


def test_a_repeat_rejection_does_not_blank_what_was_captured(conn):
    """Idempotent must not mean destructive. A second call from a path that
    has no address to hand keeps the one already recorded."""
    reject_property(conn, "131FZM", address="5012 West 77th Drive", city="Westminster")
    reject_property(conn, "131FZM", reason="changed my mind, still no")

    row = conn.execute("SELECT * FROM rejections WHERE property_id='131FZM'").fetchone()
    assert row["address"] == "5012 West 77th Drive"
    assert row["reason"] == "changed my mind, still no"


# --- telling Compass (#92) ----------------------------------------------
#
# Our record is the one that governs what Ben sees, and it survives a relist.
# Compass's is the one his wife and their agent look at. Keeping them in step
# is one PUT, and every test here is about the bookkeeping around it, because
# the PUT itself answers `200 {}` and tells us nothing.


def test_the_compass_listing_id_is_captured_at_rejection_time(conn):
    """It has to be captured now. Rejecting a house is what removes it from
    the corpus on the next run, and property_ids goes with the listing -- by
    the time we want to tell Compass, nothing else knows which listing it
    was."""
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", listing_ref="L1")

    assert [r["listing_ref"] for r in rejections_pending_compass_sync(conn)] == ["L1"]


def test_the_listing_id_outlives_the_listing(conn):
    """The delete_orphaned_rows sweep finds child tables by a `listing_id`
    column. The column here is `listing_ref` for exactly that reason: naming
    it `listing_id` would enrol the rejections table in a sweep that deletes
    precisely the rejections doing their job."""
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", listing_ref="L1", address="1 Test St")
    conn.execute("DELETE FROM listings WHERE listing_id = 'L1'")
    delete_orphaned_rows(conn)

    pending = rejections_pending_compass_sync(conn)
    assert [r["property_id"] for r in pending] == ["131FZM"]
    assert pending[0]["listing_ref"] == "L1"


def test_a_synced_rejection_stops_being_pending(conn):
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", listing_ref="L1")
    mark_rejections_synced(conn, ["131FZM"])

    assert rejections_pending_compass_sync(conn) == []


def test_marking_nothing_synced_is_not_an_error(conn):
    mark_rejections_synced(conn, [])


def test_a_backlog_comes_back_oldest_first(conn):
    """plan_sync sends the first few and defers the rest, so the order here
    decides who waits -- and the one who has waited longest should not."""
    for i, pid in enumerate(("AAA", "BBB", "CCC")):
        add(conn, f"L{i}", pid=pid)
        reject_property(conn, pid, listing_ref=f"L{i}")
        conn.execute(
            "UPDATE rejections SET rejected_at = ? WHERE property_id = ?",
            (f"2026-09-0{i + 1}T00:00:00+00:00", pid),
        )
    conn.commit()

    assert [r["property_id"] for r in rejections_pending_compass_sync(conn)] == [
        "AAA", "BBB", "CCC",
    ]


def test_re_rejecting_a_relisted_house_tells_compass_again(conn):
    """Compass keys notInterested on the listing, so a relist is a house it
    considers un-rejected under an id it has never been told about. Ours
    survived the relist; Compass's did not, and the sync has to run again."""
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM", listing_ref="L1", address="1 Test St")
    mark_rejections_synced(conn, ["131FZM"])
    assert rejections_pending_compass_sync(conn) == []

    add(conn, "L2", pid="131FZM")
    reject_property(conn, "131FZM", listing_ref="L2")

    pending = rejections_pending_compass_sync(conn)
    assert [r["listing_ref"] for r in pending] == ["L2"]
    # ...and the address captured the first time is still there.
    assert pending[0]["address"] == "1 Test St"


def test_a_rejection_predating_the_column_finds_its_listing_anyway(conn):
    """The three rejections already in production were recorded before
    listing_ref existed. While their listing is still in the corpus the
    mapping is right there in property_ids, and not using it would make them
    permanently unsendable."""
    add(conn, "L1", pid="131FZM")
    reject_property(conn, "131FZM")
    conn.execute("UPDATE rejections SET listing_ref = NULL")
    conn.commit()

    assert [r["listing_ref"] for r in rejections_pending_compass_sync(conn)] == ["L1"]


def test_a_rejection_with_no_listing_id_never_becomes_pending_forever(conn):
    """It is returned, so it is visible; plan_sync is what skips it. Hiding
    it here would make a rejection Compass can never hear about look
    synced."""
    reject_property(conn, "131FZM")

    pending = rejections_pending_compass_sync(conn)
    assert [r["property_id"] for r in pending] == ["131FZM"]
    assert pending[0]["listing_ref"] is None
