"""The --rescore-all selection rule.

Isolated from score_photos' module-level API-key/env work by testing the
selection logic directly against a real sqlite db.
"""
import sqlite3
from pathlib import Path

from src.db import get_connection, get_listing_ids_missing_visual_score, upsert_listing, upsert_visual_score
from src.models import Listing
from src.vision import VisualScoreResult


def _listing(lid):
    return Listing(
        listing_id=lid, address=f"{lid} St", city="Denver", state="CO", zip_code="80202",
        price="$500,000", beds=3, baths=2.0, sqft=1800, lot_sqft=7000, parking_spaces=2,
        year_built=1990, description="d", amenities=[], photo_urls=[], listing_url="u",
    )


def _select(conn, rescore_all, already_submitted=frozenset()):
    """Mirrors score_photos.main()'s candidate selection."""
    listings_by_id = {r["listing_id"]: r for r in conn.execute("SELECT * FROM listings")}
    candidates = list(listings_by_id) if rescore_all else get_listing_ids_missing_visual_score(conn)
    return [lid for lid in candidates if lid not in already_submitted]


def _seed(tmp_path):
    conn = get_connection(tmp_path / "t.db")
    for lid in ("scored", "unscored"):
        upsert_listing(conn, _listing(lid))
    upsert_visual_score(conn, "scored", VisualScoreResult(
        condition_photo_score=80.0, outdoor_photo_score=70.0, garage_attached=None))
    conn.commit()
    return conn


def test_default_scores_only_listings_without_a_score(tmp_path: Path):
    conn = _seed(tmp_path)
    assert _select(conn, rescore_all=False) == ["unscored"]


def test_rescore_all_includes_already_scored_listings(tmp_path: Path):
    conn = _seed(tmp_path)
    assert sorted(_select(conn, rescore_all=True)) == ["scored", "unscored"]


def test_rescore_all_still_honours_the_submitted_checkpoint(tmp_path: Path):
    """The checkpoint exists so an interrupted run never pays twice;
    --rescore-all must not defeat it."""
    conn = _seed(tmp_path)
    selected = _select(conn, rescore_all=True, already_submitted={"scored"})
    assert selected == ["unscored"]
