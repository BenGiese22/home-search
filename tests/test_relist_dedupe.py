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

from src.db import (
    duplicate_address_groups,
    find_relisted,
    find_relisted_all,
    find_relisted_by_property_id,
    listing_ids_missing_property_id,
    upsert_property_id,
)
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


# --- the property id, which is the non-heuristic answer -------------------
#
# Address is a proxy for "same house" and it fails in both directions:
# Compass re-enters "James Circle" as "James Cir" and the rows never group,
# while a genuine duplex shares an address without being one property. The
# `_pid` in Compass's own canonical URL is its answer to the same question,
# and it survives a relist -- which is exactly what a listing id does not.


def hosted(conn, lid, count):
    """`count` hosted photo rows, so the completeness tiebreak has something
    to compare."""
    for i in range(count):
        conn.execute(
            "INSERT INTO hosted_photos (listing_id, position, blob_url)"
            " VALUES (?, ?, ?)",
            (lid, i + 100, f"https://blob/{lid}/{i}"),
        )
    conn.commit()


def pid(conn, listing_id, property_id):
    upsert_property_id(conn, listing_id, property_id)



def test_two_ids_sharing_a_property_id_are_a_relist(conn):
    add(conn, "old", address="12651 James Circle")
    add(conn, "new", address="12651 James Circle")
    pid(conn, "old", "131FZM")
    pid(conn, "new", "131FZM")

    assert find_relisted_by_property_id(conn, {"new"}) == [("131FZM", "new", "old")]


def test_both_ids_live_is_resolved_rather_than_left_alone(conn):
    """The case the address rule deliberately declines. It was right to: two
    live ids at one address might be a duplex. Two live ids at one PROPERTY
    id cannot be -- Compass has said they are the same house -- so there is
    no ambiguity left to protect against, and leaving it means verify fails
    every run until Compass happens to drop one."""
    add(conn, "old", address="12651 James Circle")
    add(conn, "new", address="12651 James Circle")
    pid(conn, "old", "131FZM")
    pid(conn, "new", "131FZM")

    # The address rule sees two live ids and does nothing, by design.
    assert find_relisted(conn, {"old", "new"}) == []
    # The property id rule resolves it.
    assert len(find_relisted_by_property_id(conn, {"old", "new"})) == 1


def test_a_relist_whose_address_text_changed_is_still_caught(conn):
    """"James Cir" for "James Circle" is enough to hide a duplicate from
    every address-based check, including the verify invariant. The property
    id does not care how the address was typed."""
    add(conn, "old", address="12651 James Circle")
    add(conn, "new", address="12651 James Cir")
    pid(conn, "old", "131FZM")
    pid(conn, "new", "131FZM")

    assert find_relisted(conn, {"new"}) == []
    assert find_relisted_by_property_id(conn, {"new"}) == [("131FZM", "new", "old")]


def test_a_fetched_id_beats_an_unfetched_one(conn):
    """Compass just told us which id is current by returning it. That beats
    any tiebreak we could invent."""
    add(conn, "old", address="A")
    add(conn, "new", address="A")
    pid(conn, "old", "P1")
    pid(conn, "new", "P1")
    hosted(conn, "old", 40)  # more photos, but not fetched
    hosted(conn, "new", 2)

    assert find_relisted_by_property_id(conn, {"new"}) == [("P1", "new", "old")]


def test_among_equally_live_ids_the_more_complete_one_wins(conn):
    add(conn, "a", address="A")
    add(conn, "b", address="A")
    pid(conn, "a", "P1")
    pid(conn, "b", "P1")
    hosted(conn, "a", 34)
    hosted(conn, "b", 30)

    assert find_relisted_by_property_id(conn, {"a", "b"}) == [("P1", "a", "b")]


def test_distinct_properties_at_one_address_are_left_alone(conn):
    """A duplex. Same address, different property ids -- two real listings,
    and deleting either would lose a house."""
    add(conn, "unit_a", address="100 Duplex Way")
    add(conn, "unit_b", address="100 Duplex Way")
    pid(conn, "unit_a", "AAA")
    pid(conn, "unit_b", "BBB")

    assert find_relisted_by_property_id(conn, {"unit_a", "unit_b"}) == []


def test_a_listing_with_no_resolved_property_id_is_not_grouped(conn):
    add(conn, "old", address="A")
    add(conn, "new", address="A")
    pid(conn, "new", "P1")

    assert find_relisted_by_property_id(conn, {"new"}) == []


def test_only_the_listings_still_present_are_grouped(conn):
    """A property_ids row outlives nothing: the delete cascade removes it
    with its listing. But a stale row must never resurrect a deleted listing
    into a group, so the grouping joins against listings."""
    add(conn, "new", address="A")
    pid(conn, "new", "P1")
    conn.execute(
        "INSERT INTO property_ids (listing_id, property_id, resolved_at)"
        " VALUES ('ghost', 'P1', '2026-09-06T00:00:00+00:00')"
    )
    conn.commit()

    assert find_relisted_by_property_id(conn, {"new"}) == []


# --- how the two rules combine -------------------------------------------


def test_the_address_rule_still_runs_for_unresolved_listings(conn):
    add(conn, "old", address="A")
    add(conn, "new", address="A")

    assert find_relisted_all(conn, {"new"}) == [("A, Broomfield", "new", "old")]


def test_the_property_id_wins_where_the_rules_overlap(conn):
    """Not merely deduplicated by drop id. If the two rules disagreed about
    which id to KEEP, honouring both would delete every row in the group."""
    add(conn, "old", address="A")
    add(conn, "new", address="A")
    pid(conn, "old", "P1")
    pid(conn, "new", "P1")
    hosted(conn, "old", 40)
    hosted(conn, "new", 2)

    result = find_relisted_all(conn, {"old", "new"})

    assert result == [("P1", "old", "new")]
    dropped = {drop for _l, _k, drop in result}
    kept = {keep for _l, keep, _d in result}
    assert not (dropped & kept), "a listing was both kept and dropped"


def test_a_duplex_is_not_deleted_by_the_address_rule_either(conn):
    """The property ids say these are different houses. The address rule must
    not then get a second go at them and drop one."""
    add(conn, "unit_a", address="100 Duplex Way")
    add(conn, "unit_b", address="100 Duplex Way")
    pid(conn, "unit_a", "AAA")
    pid(conn, "unit_b", "BBB")

    assert find_relisted_all(conn, {"unit_a"}) == []


def test_deleting_a_listing_removes_its_property_id(conn):
    """property_ids declares a foreign key to listings, and Turso enforces
    foreign keys the local database only declares. An orphan here fails to
    sync on every subsequent run."""
    from src.db import delete_listing

    add(conn, "gone", address="A")
    pid(conn, "gone", "P1")

    delete_listing(conn, "gone")

    assert conn.execute("SELECT COUNT(*) FROM property_ids").fetchone()[0] == 0


def test_only_unresolved_listings_are_selected_for_lookup(conn):
    """What keeps this cheap. A property id cannot change, so a listing pays
    for the request once ever -- a steady-state run resolves nothing."""
    from src.db import listing_ids_missing_property_id

    add(conn, "known", address="A")
    add(conn, "fresh", address="B")
    pid(conn, "known", "P1")

    assert listing_ids_missing_property_id(conn) == ["fresh"]
