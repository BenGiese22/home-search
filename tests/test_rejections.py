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
    is_rejected,
    reject_property,
    rejected_property_ids,
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
