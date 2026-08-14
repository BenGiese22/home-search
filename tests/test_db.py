from pathlib import Path

from src.db import get_connection, query_listings, upsert_listing
from src.models import Listing

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
