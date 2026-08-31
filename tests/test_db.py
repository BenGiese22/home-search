import pytest
import json
import sqlite3
from pathlib import Path

from src.commute import CommuteResult
from src.db import (
    _SCHEMA,
    tables_child_first,
    tables_parent_first,
    delete_listing,
    delete_listing,
    delete_orphaned_rows,
    get_amenities,
    get_commute,
    get_connection,
    get_listing_ids_missing_commute,
    get_pinned_listing_ids,
    get_price_snapshot,
    get_scores,
    query_listings,
    upsert_commute,
    upsert_listing,
    upsert_score,
)
from src.models import Listing
from src.scoring import ScoreResult

SAMPLE = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$650,000",
    beds=4,
    baths=2.5,
    sqft=2200,
    lot_sqft=5000,
    parking_spaces=2,
    year_built=2005,
    description="desc",
    amenities=["Garage", "Central AC"],
    photo_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "listings.db"


def test_upsert_inserts_queryable_listing(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    rows = query_listings(conn)

    assert len(rows) == 1
    assert rows[0]["listing_id"] == "abc123"
    assert rows[0]["address"] == "1 Test St"
    assert rows[0]["parking_spaces"] == 2
    assert rows[0]["property_type"] == ""
    assert rows[0]["localized_status"] == ""


def test_upsert_stores_property_type_and_localized_status(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    expired = SAMPLE.__class__(
        **{**SAMPLE.__dict__, "property_type": "Single Family", "localized_status": "Expired"}
    )

    upsert_listing(conn, expired)

    rows = query_listings(conn)
    assert rows[0]["property_type"] == "Single Family"
    assert rows[0]["localized_status"] == "Expired"


def test_upsert_parses_price_into_price_numeric(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    rows = query_listings(conn)

    assert rows[0]["price_numeric"] == 650000.0


def test_upsert_stores_amenities(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    amenities = [
        row[0]
        for row in conn.execute(
            "SELECT amenity FROM amenities WHERE listing_id = ? ORDER BY amenity", ("abc123",)
        )
    ]

    assert amenities == ["Central AC", "Garage"]


def test_upsert_stores_photo_urls_in_order(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    urls = [
        row[0]
        for row in conn.execute(
            "SELECT url FROM photo_urls WHERE listing_id = ? ORDER BY position", ("abc123",)
        )
    ]

    assert urls == ["https://example.com/1.jpg", "https://example.com/2.jpg"]


def test_upsert_is_idempotent_and_replaces_existing_row(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    updated = SAMPLE.__class__(**{**SAMPLE.__dict__, "price": "$700,000", "parking_spaces": 3})
    upsert_listing(conn, updated)

    rows = query_listings(conn)

    assert len(rows) == 1
    assert rows[0]["price"] == "$700,000"
    assert rows[0]["parking_spaces"] == 3


def test_upsert_replaces_amenities_not_appends(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    updated = SAMPLE.__class__(**{**SAMPLE.__dict__, "amenities": ["Pool"]})
    upsert_listing(conn, updated)

    amenities = [
        row[0] for row in conn.execute("SELECT amenity FROM amenities WHERE listing_id = ?", ("abc123",))
    ]

    assert amenities == ["Pool"]


def test_query_listings_filters_by_min_parking_spaces(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    one_car = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "onecar", "parking_spaces": 1})
    upsert_listing(conn, one_car)

    rows = query_listings(conn, min_parking_spaces=2)

    assert [row["listing_id"] for row in rows] == ["abc123"]


def test_query_listings_filters_by_price_range(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    pricier = SAMPLE.__class__(
        **{**SAMPLE.__dict__, "listing_id": "pricier", "price": "$900,000"}
    )
    upsert_listing(conn, pricier)

    rows = query_listings(conn, max_price=800_000)

    assert [row["listing_id"] for row in rows] == ["abc123"]


def test_query_listings_returns_empty_list_when_no_matches(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    rows = query_listings(conn, min_parking_spaces=99)

    assert rows == []


def test_get_price_snapshot_returns_price_and_price_numeric_by_listing_id(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    snapshot = get_price_snapshot(conn)

    assert snapshot == {"abc123": ("$650,000", 650000.0)}


def test_get_price_snapshot_empty_db_returns_empty_dict(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))

    assert get_price_snapshot(conn) == {}


def test_upsert_listing_defaults_to_not_pinned(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_pinned_listing_ids(conn) == frozenset()


def test_upsert_listing_with_is_pinned_true(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE, is_pinned=True)

    assert get_pinned_listing_ids(conn) == frozenset({"abc123"})


def test_upsert_listing_preserves_pin_when_caller_passes_it_again(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE, is_pinned=True)
    upsert_listing(conn, SAMPLE, is_pinned=True)

    assert get_pinned_listing_ids(conn) == frozenset({"abc123"})


def test_upsert_listing_without_is_pinned_clears_a_prior_pin(tmp_path: Path):
    # Documents the real behavior: INSERT OR REPLACE fully replaces the
    # row, so a caller that re-upserts a previously-pinned listing without
    # passing is_pinned=True again will silently un-pin it. Callers must
    # look up and preserve the current pin status themselves.
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE, is_pinned=True)
    upsert_listing(conn, SAMPLE)

    assert get_pinned_listing_ids(conn) == frozenset()


def test_get_pinned_listing_ids_empty_db_returns_empty_frozenset(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))

    assert get_pinned_listing_ids(conn) == frozenset()


COMMUTE_SAMPLE = CommuteResult(
    lat=39.85, lon=-105.05,
    denver_miles=12.0, denver_minutes=25.0,
    medtronic_miles=8.0, medtronic_minutes=18.0,
    geocode_failed=False,
)


def test_upsert_commute_then_get_commute(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    row = get_commute(conn, "abc123")
    assert row["denver_minutes"] == 25.0
    assert row["medtronic_minutes"] == 18.0
    assert row["geocode_failed"] == 0


def test_get_commute_returns_none_when_absent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_commute(conn, "abc123") is None


def test_upsert_commute_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    updated = CommuteResult(**{**COMMUTE_SAMPLE.__dict__, "denver_minutes": 30.0})
    upsert_commute(conn, "abc123", updated)

    row = get_commute(conn, "abc123")
    assert row["denver_minutes"] == 30.0


def test_get_listing_ids_missing_commute(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)
    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    assert get_listing_ids_missing_commute(conn) == ["other456"]


SCORE_SAMPLE = ScoreResult(
    hoa_score=52.5,
    commute_score=80.0, sqft_score=50.0, condition_score=90.0,
    outdoor_score=100.0, room_count_score=70.0, parking_score=100.0, composite=79.5,
    passes_filters=True, has_incomplete_data=False,
)


def test_upsert_score_then_get_scores(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_score(conn, "abc123", SCORE_SAMPLE)

    rows = get_scores(conn)
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "abc123"
    assert rows[0]["composite"] == 79.5
    assert rows[0]["passes_filters"] == 1
    assert rows[0]["has_incomplete_data"] == 0


def test_upsert_score_round_trips_has_incomplete_data_true(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    incomplete = ScoreResult(**{**SCORE_SAMPLE.__dict__, "has_incomplete_data": True})
    upsert_score(conn, "abc123", incomplete)

    rows = get_scores(conn)
    assert rows[0]["has_incomplete_data"] == 1


def test_get_scores_orders_by_composite_descending(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)

    upsert_score(conn, "abc123", ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 40.0}))
    upsert_score(conn, "other456", ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 90.0}))

    rows = get_scores(conn)
    assert [row["listing_id"] for row in rows] == ["other456", "abc123"]


def test_upsert_score_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_score(conn, "abc123", SCORE_SAMPLE)

    updated = ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 55.0})
    upsert_score(conn, "abc123", updated)

    rows = get_scores(conn)
    assert len(rows) == 1
    assert rows[0]["composite"] == 55.0


def test_get_amenities_returns_list_for_listing_id(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_amenities(conn, "abc123") == ["Central AC", "Garage"]


def test_get_amenities_empty_for_unknown_listing(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))

    assert get_amenities(conn, "nope") == []


def test_delete_listing_removes_row_and_all_children(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)
    upsert_score(conn, "abc123", SCORE_SAMPLE)

    delete_listing(conn, "abc123")

    assert query_listings(conn) == []
    assert get_amenities(conn, "abc123") == []
    assert get_commute(conn, "abc123") is None
    assert get_scores(conn) == []


def test_delete_listing_also_removes_the_visual_score(tmp_path: Path):
    """visual_scores was added to the schema after delete_listing was written
    and never wired into it, leaving an orphan row behind on every delisting.
    67 had accumulated in the real database, and Turso enforces the foreign
    key, so each one failed to sync to the hosted viewer on every run."""
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)
    assert get_visual_score(conn, "abc123") is not None

    delete_listing(conn, "abc123")

    assert get_visual_score(conn, "abc123") is None


def test_delete_listing_does_not_touch_other_listings(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)

    delete_listing(conn, "abc123")

    assert [row["listing_id"] for row in query_listings(conn)] == ["other456"]


def test_delete_listing_is_safe_when_listing_does_not_exist(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))

    delete_listing(conn, "nope")  # should not raise


from src.vision import VisualScoreResult
from src.db import get_listing_ids_missing_visual_score, get_visual_score, upsert_visual_score

VISUAL_SCORE_SAMPLE = VisualScoreResult(
    condition_photo_score=67.0,
    outdoor_photo_score=70.0,
    has_layout_plan=True,
    layout_plan_clarity_score=9.0,
    garage_attached=False,
    watermarked_staging_detected=False,
    suspected_unwatermarked_staging=False,
    staging_notes="No staging concerns.",
)


def test_upsert_visual_score_then_get_visual_score(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE, raw_response='{"kitchen": {}}')

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] == 67.0
    assert row["outdoor_photo_score"] == 70.0
    assert row["has_layout_plan"] == 1
    assert row["layout_plan_clarity_score"] == 9.0
    assert row["garage_attached"] == 0
    assert row["watermarked_staging_detected"] == 0
    assert row["suspected_unwatermarked_staging"] == 0
    assert row["staging_notes"] == "No staging concerns."
    assert row["photo_score_unavailable"] == 0
    assert row["raw_response"] == '{"kitchen": {}}'


def test_upsert_visual_score_stores_garage_attached_true_and_staging_flags(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    flagged = VisualScoreResult(
        condition_photo_score=50.0,
        outdoor_photo_score=50.0,
        garage_attached=True,
        watermarked_staging_detected=True,
        suspected_unwatermarked_staging=True,
        staging_notes="Watermark visible on 2 photos.",
    )
    upsert_visual_score(conn, "abc123", flagged)

    row = get_visual_score(conn, "abc123")
    assert row["garage_attached"] == 1
    assert row["watermarked_staging_detected"] == 1
    assert row["suspected_unwatermarked_staging"] == 1
    assert row["staging_notes"] == "Watermark visible on 2 photos."


def test_upsert_visual_score_stores_garage_attached_null_when_undeterminable(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(
        conn, "abc123",
        VisualScoreResult(condition_photo_score=50.0, outdoor_photo_score=50.0, garage_attached=None),
    )

    row = get_visual_score(conn, "abc123")
    assert row["garage_attached"] is None


def test_upsert_visual_score_none_marks_unavailable(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_visual_score(conn, "abc123", None)

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] is None
    assert row["outdoor_photo_score"] is None
    assert row["has_layout_plan"] == 0
    assert row["layout_plan_clarity_score"] is None
    assert row["garage_attached"] is None
    assert row["watermarked_staging_detected"] == 0
    assert row["suspected_unwatermarked_staging"] == 0
    assert row["staging_notes"] is None
    assert row["photo_score_unavailable"] == 1


def test_get_visual_score_returns_none_when_absent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_visual_score(conn, "abc123") is None


def test_upsert_visual_score_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)

    updated = VisualScoreResult(condition_photo_score=50.0, outdoor_photo_score=50.0)
    upsert_visual_score(conn, "abc123", updated)

    row = get_visual_score(conn, "abc123")
    assert row["condition_photo_score"] == 50.0


def test_get_listing_ids_missing_visual_score(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)

    assert get_listing_ids_missing_visual_score(conn) == ["other456"]


def test_delete_listing_cleans_every_table_that_references_listings(tmp_path: Path):
    """Derived from the schema rather than a hardcoded list, because a
    hardcoded list is exactly what failed: visual_scores was added to _SCHEMA
    and never added to delete_listing. This fails automatically the next time
    a child table is introduced without wiring it in."""
    conn = get_connection(_db_path(tmp_path))

    child_tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'listings'"
        )
        if any(
            info[1] == "listing_id"
            for info in conn.execute(f"PRAGMA table_info({row[0]})")
        )
    ]
    assert "visual_scores" in child_tables, "sanity: the table this bug was about"

    conn.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
        "listing_url) VALUES ('orphan-probe','a','c','CO','1','$1',1,1,1,1,1,2000,'d','u')"
    )
    for table in child_tables:
        cols = [i[1] for i in conn.execute(f"PRAGMA table_info({table})")]
        notnull = {
            i[1]: i[4]
            for i in conn.execute(f"PRAGMA table_info({table})")
            if i[3] and i[4] is None and i[1] != "listing_id"
        }
        insert_cols = ["listing_id"] + list(notnull)
        placeholders = ", ".join("?" for _ in insert_cols)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})",
            tuple(["orphan-probe"] + ["0"] * len(notnull)),
        )
    conn.commit()

    delete_listing(conn, "orphan-probe")

    leftovers = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE listing_id = 'orphan-probe'"
        ).fetchone()[0]
        for table in child_tables
    }
    assert all(n == 0 for n in leftovers.values()), (
        f"delete_listing left orphans behind: "
        f"{ {t: n for t, n in leftovers.items() if n} }"
    )


def test_delete_orphaned_rows_removes_children_with_no_listing(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    # Foreign keys are enforced now (matching Turso), so an orphan can no
    # longer be created through the normal path -- which is the point. This
    # cleanup exists for rows that predate enforcement, so the test has to
    # turn it off to manufacture one.
    conn.execute("PRAGMA foreign_keys = OFF")
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)
    upsert_visual_score(conn, "ghost", VISUAL_SCORE_SAMPLE)
    conn.commit()

    removed = delete_orphaned_rows(conn)

    assert removed == {"visual_scores": 1}
    assert get_visual_score(conn, "abc123") is not None, "the live listing must survive"
    assert get_visual_score(conn, "ghost") is None


def test_delete_orphaned_rows_is_a_no_op_on_a_clean_database(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)
    conn.commit()

    assert delete_orphaned_rows(conn) == {}
    assert get_visual_score(conn, "abc123") is not None


def test_upsert_listing_roundtrips_hoa_annual_unknown(tmp_path: Path):
    """None must survive as None, not collapse to 0 -- 0.0 means confirmed
    no HOA and is scored as a small positive."""
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE)
    row = conn.execute("SELECT hoa_annual FROM listings").fetchone()
    assert row["hoa_annual"] is None


def test_upsert_listing_roundtrips_hoa_annual_confirmed_zero(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE.__class__(**{**SAMPLE.__dict__, "hoa_annual": 0.0}))
    row = conn.execute("SELECT hoa_annual FROM listings").fetchone()
    assert row["hoa_annual"] == 0.0
    assert row["hoa_annual"] is not None


def test_upsert_listing_roundtrips_hoa_annual_known_value(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE.__class__(**{**SAMPLE.__dict__, "hoa_annual": 1200.0}))
    row = conn.execute("SELECT hoa_annual FROM listings").fetchone()
    assert row["hoa_annual"] == 1200.0


def test_init_db_migrates_existing_listings_table_missing_hoa_annual_column(tmp_path: Path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE listings (
            listing_id TEXT PRIMARY KEY, address TEXT NOT NULL, city TEXT NOT NULL,
            state TEXT NOT NULL, zip_code TEXT NOT NULL, price TEXT NOT NULL,
            price_numeric REAL, beds INTEGER NOT NULL, baths REAL NOT NULL,
            sqft INTEGER NOT NULL, lot_sqft INTEGER NOT NULL,
            parking_spaces INTEGER NOT NULL, year_built INTEGER NOT NULL,
            description TEXT NOT NULL, listing_url TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    assert "hoa_annual" in columns


def test_upsert_score_then_get_scores_includes_hoa_score(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE)
    upsert_score(conn, "abc123", ScoreResult(**{**SCORE_SAMPLE.__dict__, "hoa_score": 12.0}))
    rows = get_scores(conn)
    assert rows[0]["hoa_score"] == 12.0


def test_init_db_migrates_existing_scores_table_missing_hoa_score_column(tmp_path: Path):
    db_path = tmp_path / "oldscores.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE scores (
            listing_id TEXT PRIMARY KEY, commute_score REAL NOT NULL,
            sqft_score REAL NOT NULL, condition_score REAL NOT NULL,
            outdoor_score REAL NOT NULL, parking_score REAL NOT NULL,
            composite REAL NOT NULL, passes_filters INTEGER NOT NULL,
            computed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
    assert "hoa_score" in columns


def test_upsert_listing_roundtrips_structured_fields(tmp_path: Path):
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE.__class__(**{**SAMPLE.__dict__, "tax_annual": 4407.0,
                                             "sqft_above_grade": 1862, "sqft_below_grade": 725,
                                             "outdoor_spaces": ["Deck", "Patio"]}))
    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["tax_annual"] == 4407.0
    assert row["sqft_above_grade"] == 1862
    assert row["sqft_below_grade"] == 725
    assert json.loads(row["outdoor_spaces"]) == ["Deck", "Patio"]


def test_upsert_listing_keeps_below_grade_null_distinct_from_zero(tmp_path: Path):
    """NULL means no basement; 0 means a basement with no finished area."""
    conn = get_connection(tmp_path / "t.db")
    upsert_listing(conn, SAMPLE.__class__(**{**SAMPLE.__dict__, "sqft_below_grade": None}))
    assert conn.execute("SELECT sqft_below_grade FROM listings").fetchone()[0] is None
    upsert_listing(conn, SAMPLE.__class__(**{**SAMPLE.__dict__, "sqft_below_grade": 0}))
    assert conn.execute("SELECT sqft_below_grade FROM listings").fetchone()[0] == 0


def test_init_db_migrates_existing_listings_table_missing_structured_columns(tmp_path: Path):
    db_path = tmp_path / "old2.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE listings (
            listing_id TEXT PRIMARY KEY, address TEXT NOT NULL, city TEXT NOT NULL,
            state TEXT NOT NULL, zip_code TEXT NOT NULL, price TEXT NOT NULL,
            price_numeric REAL, beds INTEGER NOT NULL, baths REAL NOT NULL,
            sqft INTEGER NOT NULL, lot_sqft INTEGER NOT NULL,
            parking_spaces INTEGER NOT NULL, year_built INTEGER NOT NULL,
            description TEXT NOT NULL, listing_url TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    for col in ("tax_annual", "sqft_above_grade", "sqft_below_grade", "outdoor_spaces"):
        assert col in columns


def test_get_connection_enforces_foreign_keys_like_turso(tmp_path: Path):
    """The divergence that made FK bugs production-only: Turso enforces
    foreign keys, local SQLite defaults them off."""
    conn = get_connection(_db_path(tmp_path))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_inserting_a_child_without_its_listing_now_fails_locally(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO amenities (listing_id, amenity) VALUES ('ghost', 'Pool')")


def test_tables_child_first_puts_listings_last():
    order = tables_child_first()
    assert order[-1] == "listings"
    for child in ("amenities", "photo_urls", "commute", "scores", "visual_scores"):
        assert order.index(child) < order.index("listings")


def test_tables_parent_first_is_the_exact_reverse():
    assert tables_parent_first() == list(reversed(tables_child_first()))


def test_extra_tables_are_treated_as_children_of_listings():
    order = tables_child_first(extra_tables=("hosted_photos",))
    assert order.index("hosted_photos") < order.index("listings")


def test_ordering_adapts_to_a_new_referencing_table():
    """The point of deriving it: a new child sorts itself, with no list to
    remember to update."""
    schema = _SCHEMA + """
    CREATE TABLE IF NOT EXISTS notes (
        listing_id TEXT NOT NULL REFERENCES listings(listing_id),
        body TEXT NOT NULL
    );
    """
    order = tables_child_first(schema)
    assert order.index("notes") < order.index("listings")


def test_delete_listing_removes_children_under_fk_enforcement(tmp_path: Path):
    """Regression for the publish.py prune bug, in the other delete path."""
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_visual_score(conn, "abc123", VISUAL_SCORE_SAMPLE)
    conn.commit()

    delete_listing(conn, "abc123")

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM visual_scores").fetchone()[0] == 0
