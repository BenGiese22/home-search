"""Carrying a paid-for vision score across a relist.

A relist is the same house under a new listing id. The old row is superseded
and deleted, and its `visual_scores` row goes with it -- so the new id has no
score, `score_photos.py` submits it to the vision API, and we pay a second
time to look at the same photographs. Three relists so far.

The whole question is *when* that is safe. A relist often happens because
something changed, and if the seller renovated and re-photographed, the old
condition score is not merely stale -- it is wrong in the direction that
hides a good house. So the carry-over is gated on the photo sets actually
being the same photographs.
"""

import sqlite3

import pytest

from src.db import (
    carry_visual_score,
    get_visual_score,
    photo_url_overlap,
    upsert_visual_score,
)
from src.turso_db import ensure_schema
from src.vision import VisualScoreResult


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def add(conn, lid, address="12651 James Circle"):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, is_pinned, property_type, localized_status
           ) VALUES (?, ?, 'Broomfield', 'CO', '80020', '$599,000', 3, 2.0,
                     1800, 7000, 2, 1990, 'd', 'https://x/l', 0,
                     'Single Family', 'Active')""",
        (lid, address),
    )
    conn.commit()


def photos(conn, lid, urls):
    for i, url in enumerate(urls):
        conn.execute(
            "INSERT INTO photo_urls (listing_id, position, url) VALUES (?, ?, ?)",
            (lid, i, url),
        )
    conn.commit()


def scored(conn, lid, condition=82.0, outdoor=71.0):
    upsert_visual_score(
        conn,
        lid,
        VisualScoreResult(
            condition_photo_score=condition,
            outdoor_photo_score=outdoor,
            has_layout_plan=True,
            layout_plan_clarity_score=64.0,
            garage_attached=True,
            staging_notes="bright, recently painted",
        ),
        raw_response='{"condition":82}',
    )


def unscorable(conn, lid):
    """The row written when a listing had too few photos or the call failed."""
    upsert_visual_score(conn, lid, None)


URLS = [f"https://cdn.compass.com/{i}.jpg" for i in range(10)]


# --- how much of the photography is shared? ------------------------------


def test_identical_photo_sets_overlap_completely(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)

    assert photo_url_overlap(conn, "old", "new") == 1.0


def test_a_relist_that_added_photos_still_overlaps_fully(conn):
    """Measured against the SMALLER set on purpose. Compass re-uploading the
    same twenty photographs plus four new ones is the same photography, and a
    plain Jaccard would score it 0.83 and re-buy it."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS + ["https://cdn.compass.com/extra.jpg"])

    assert photo_url_overlap(conn, "old", "new") == 1.0


def test_a_completely_new_photo_set_does_not_overlap(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", [f"https://cdn.compass.com/reshoot-{i}.jpg" for i in range(10)])

    assert photo_url_overlap(conn, "old", "new") == 0.0


def test_a_partial_reshoot_is_reported_as_a_fraction(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS[:5] + [f"https://cdn.compass.com/new-{i}.jpg" for i in range(5)])

    assert photo_url_overlap(conn, "old", "new") == 0.5


def test_a_listing_with_no_photos_overlaps_nothing(conn):
    """Zero rather than one. An empty set is trivially a subset of anything,
    and returning 1.0 would carry a score onto a listing we have never seen
    a photograph of."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)

    assert photo_url_overlap(conn, "old", "new") == 0.0
    assert photo_url_overlap(conn, "new", "old") == 0.0


# --- carrying the score ---------------------------------------------------


def test_the_score_is_carried_when_the_photos_are_the_same(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)
    scored(conn, "old")

    assert carry_visual_score(conn, "old", "new") is True

    carried = get_visual_score(conn, "new")
    original = get_visual_score(conn, "old")
    for field in (
        "condition_photo_score", "outdoor_photo_score", "has_layout_plan",
        "layout_plan_clarity_score", "garage_attached",
        "watermarked_staging_detected", "suspected_unwatermarked_staging",
        "staging_notes", "photo_score_unavailable", "raw_response",
    ):
        assert carried[field] == original[field], field


def test_the_original_measurement_date_is_kept(conn):
    """Not restamped to now. computed_at answers "when did we look at these
    photographs", and the answer is unchanged by copying the row."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)
    scored(conn, "old")

    carry_visual_score(conn, "old", "new")

    assert get_visual_score(conn, "new")["computed_at"] == get_visual_score(conn, "old")["computed_at"]


def test_a_reshot_listing_is_not_carried(conn):
    """The error that matters. A relist often happens BECAUSE something
    changed -- and if the seller renovated and re-photographed, carrying the
    old condition score keeps a house ranked on how it used to look. Paying
    for one vision call is cheaper than not seeing a house that got better."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", [f"https://cdn.compass.com/reshoot-{i}.jpg" for i in range(10)])
    scored(conn, "old")

    assert carry_visual_score(conn, "old", "new") is False
    assert get_visual_score(conn, "new") is None


def test_a_half_reshot_listing_is_not_carried(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS[:5] + [f"https://cdn.compass.com/new-{i}.jpg" for i in range(5)])
    scored(conn, "old")

    assert carry_visual_score(conn, "old", "new") is False


def test_a_failed_score_is_not_carried(conn):
    """photo_score_unavailable means "we could not score this". Copying it
    forward buys nothing and suppresses the retry that would fix it."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)
    unscorable(conn, "old")

    assert carry_visual_score(conn, "old", "new") is False
    assert get_visual_score(conn, "new") is None


def test_an_existing_score_on_the_survivor_is_never_overwritten(conn):
    """The survivor's own score was computed from its own photographs. The
    superseded row is the older measurement by definition."""
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)
    scored(conn, "old", condition=82.0)
    scored(conn, "new", condition=44.0)

    assert carry_visual_score(conn, "old", "new") is False
    assert get_visual_score(conn, "new")["condition_photo_score"] == 44.0


def test_nothing_to_carry_is_not_an_error(conn):
    add(conn, "old"), add(conn, "new", address="B")
    photos(conn, "old", URLS)
    photos(conn, "new", URLS)

    assert carry_visual_score(conn, "old", "new") is False
