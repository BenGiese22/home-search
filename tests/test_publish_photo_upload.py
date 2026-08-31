"""What remains publish-specific.

publish.py is otherwise deliberately untested -- thin orchestration plus real
network calls, matching score_photos.py/compute_commutes.py. The photo-upload
logic that used to live here moved to src/photo_upload.py in #21 and is
covered by tests/test_photo_upload.py; what is left is the prune ordering
(which encodes a foreign-key bug that shipped twice) and the photo cap.
"""
import importlib
import sqlite3

import pytest

import publish


def test_prunable_tables_deletes_children_before_listings():
    """Turso enforces foreign keys that local SQLite does not. Pruning the
    parent row first aborts the whole publish, so `listings` must be last."""
    from publish import PRUNABLE_TABLES

    assert PRUNABLE_TABLES[-1] == "listings"
    for child in ("commute", "scores", "visual_scores", "amenities", "photo_urls"):
        assert PRUNABLE_TABLES.index(child) < PRUNABLE_TABLES.index("listings")


def test_prune_deleted_listings_succeeds_with_foreign_keys_enforced():
    """Regression: reproduces the live failure by turning FK enforcement on,
    which is how Turso behaves and how local sqlite3 does not."""
    from publish import _prune_deleted_listings
    from src.db import _SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS hosted_photos (
               listing_id TEXT NOT NULL, position INTEGER NOT NULL,
               blob_url TEXT NOT NULL, PRIMARY KEY (listing_id, position));"""
    )
    conn.execute("PRAGMA foreign_keys = ON")
    for lid in ("keep", "drop"):
        conn.execute(
            "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
            "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
            "listing_url) VALUES (?, 'a', 'b', 'CO', '80020', '$1', 1, 1, 1, 1, 1, 1, 'd', 'u')",
            (lid,),
        )
        conn.execute("INSERT INTO amenities (listing_id, amenity) VALUES (?, 'Pool')", (lid,))
    conn.commit()

    pruned = _prune_deleted_listings(conn, ["keep"])

    assert pruned == 2  # the dropped listing's own row plus its amenity
    assert [r[0] for r in conn.execute("SELECT listing_id FROM listings")] == ["keep"]
    assert [r[0] for r in conn.execute("SELECT listing_id FROM amenities")] == ["keep"]


def test_the_default_cap_is_uncapped():
    """The cap existed because Vercel's Hobby tier included only 2,000
    Advanced Operations per month and a 3,000-photo backfill once consumed
    11,000, suspending the store for 30 days. On Pro that no longer binds."""
    assert publish.DEFAULT_MAX_PHOTOS_PER_LISTING == 0


def test_the_env_override_is_retained(monkeypatch):
    """Uncapped by default, but still capped on demand -- and the value must
    come through the merged lookup, so process env wins over .env."""
    monkeypatch.setenv("MAX_PHOTOS_PER_LISTING", "5")
    reloaded = importlib.reload(publish)
    try:
        assert reloaded.MAX_PHOTOS_PER_LISTING == 5
    finally:
        monkeypatch.delenv("MAX_PHOTOS_PER_LISTING", raising=False)
        importlib.reload(publish)
