"""One property, one row.

A relist arrives as a NEW listing_id for a house already in the corpus, so
the delisting cascade never sees it: the old row is not "absent from the
collection" in any way the cascade recognises, and pinned rows are exempt
from it regardless. Two properties were sitting in the corpus twice before
this existed -- one scored 62.7 and 60.8, adjacent in the ranking, and both
paid for at the vision API.
"""
import sqlite3
from pathlib import Path

import pytest

from src.db import duplicate_address_groups, find_relisted
from src.diff import supersede_relisted
from src.turso_db import ensure_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def add(conn, lid, address="12651 James Circle", city="Broomfield", blob=None):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, is_pinned, property_type, localized_status
           ) VALUES (?, ?, ?, 'CO', '80020', '$599,000', 3, 2.0, 1800, 7000,
                     2, 1990, 'd', 'https://x/l', 0, 'Single Family', 'Active')""",
        (lid, address, city),
    )
    if blob:
        conn.execute(
            "INSERT INTO hosted_photos (listing_id, position, blob_url) VALUES (?, 1, ?)",
            (lid, blob),
        )
    conn.commit()


# --- detection -------------------------------------------------------------

def test_duplicate_addresses_are_found(conn):
    add(conn, "old")
    add(conn, "new")
    groups = duplicate_address_groups(conn)
    assert len(groups) == 1
    assert groups[0][0] == "12651 James Circle, Broomfield"
    assert sorted(groups[0][1]) == ["new", "old"]


def test_the_same_street_in_two_cities_is_not_a_duplicate(conn):
    """An address alone is not unique across the four suburbs this corpus
    covers, so the key has to include the city."""
    add(conn, "a", address="5012 West 77th Drive", city="Westminster")
    add(conn, "b", address="5012 West 77th Drive", city="Arvada")
    assert duplicate_address_groups(conn) == []


def test_a_clean_corpus_has_no_groups(conn):
    add(conn, "a", address="1 Main St")
    add(conn, "b", address="2 Oak Ave")
    assert duplicate_address_groups(conn) == []


# --- resolution ------------------------------------------------------------

def test_the_fetched_id_wins(conn):
    """Compass just told us which id is current by returning it. Guessing
    from price, status or id ordering would invent a heuristic where the
    scrape already has the answer."""
    add(conn, "old")
    add(conn, "new")

    assert find_relisted(conn, {"new"}) == [
        ("12651 James Circle, Broomfield", "new", "old")
    ]


def test_nothing_happens_when_neither_id_was_fetched(conn):
    """Two stale rows for one address is a real problem, but not one this
    can resolve -- deleting on a guess is worse than leaving it."""
    add(conn, "old")
    add(conn, "new")
    assert find_relisted(conn, set()) == []


def test_two_live_ids_for_one_address_are_left_alone(conn):
    """A genuine duplex, or Compass listing a property twice. Either way it
    is not a relist and must not be silently halved."""
    add(conn, "a")
    add(conn, "b")
    assert find_relisted(conn, {"a", "b"}) == []


def test_an_unaffected_address_is_never_returned(conn):
    add(conn, "solo", address="9 Elm St")
    assert find_relisted(conn, {"solo"}) == []


# --- removal ---------------------------------------------------------------

def test_superseding_removes_the_stale_row(conn, tmp_path: Path):
    add(conn, "old")
    add(conn, "new")

    dropped = supersede_relisted(
        conn, find_relisted(conn, {"new"}), photos_dir=tmp_path
    )

    assert dropped == ["old"]
    ids = [r[0] for r in conn.execute("SELECT listing_id FROM listings").fetchall()]
    assert ids == ["new"]


def test_the_stale_row_s_blobs_are_reclaimed(conn, tmp_path: Path):
    """hosted_photos.blob_url is the ONLY record of an uploaded image, so
    the URLs must be read before the rows go. Deleting first is how 1,813
    orphans accumulated once already."""
    add(conn, "old", blob="https://blob/old-1.jpg")
    add(conn, "new", blob="https://blob/new-1.jpg")
    deleted = []

    supersede_relisted(
        conn,
        find_relisted(conn, {"new"}),
        photos_dir=tmp_path,
        blob_token="tok",
        delete_fn=lambda urls, _t: deleted.extend(urls),
    )

    assert deleted == ["https://blob/old-1.jpg"]


def test_the_surviving_listing_keeps_its_blobs(conn, tmp_path: Path):
    add(conn, "old", blob="https://blob/old-1.jpg")
    add(conn, "new", blob="https://blob/new-1.jpg")
    deleted = []

    supersede_relisted(
        conn, find_relisted(conn, {"new"}), photos_dir=tmp_path,
        blob_token="tok", delete_fn=lambda urls, _t: deleted.extend(urls),
    )

    assert "https://blob/new-1.jpg" not in deleted
    rows = conn.execute("SELECT listing_id FROM hosted_photos").fetchall()
    assert [r[0] for r in rows] == ["new"]


def test_nothing_to_supersede_is_a_no_op(conn, tmp_path: Path):
    add(conn, "solo")
    assert supersede_relisted(conn, [], photos_dir=tmp_path) == []
