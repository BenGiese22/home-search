from pathlib import Path

from src.commute import CommuteResult
from src.db import (
    delete_listing,
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
