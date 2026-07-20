import csv
from pathlib import Path

from src.csv_writer import write_csv
from src.models import Listing

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$500,000",
    beds=3,
    baths=2.5,
    sqft=1800,
    lot_sqft=6000,
    year_built=1995,
    description="A lovely home",
    amenities=["Renovated Kitchen", "Private Yard"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_write_csv_round_trips_fields(tmp_path: Path):
    csv_path = tmp_path / "listings.csv"
    photos_root = tmp_path / "photos"

    write_csv([LISTING], photos_root, csv_path)

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    row = rows[0]
    assert row["listing_id"] == "abc123"
    assert row["address"] == "1 Test St"
    assert row["price"] == "$500,000"
    assert row["beds"] == "3"
    assert row["baths"] == "2.5"
    assert row["amenities"] == "Renovated Kitchen; Private Yard"
    assert row["listing_url"] == "https://example.com/listing/abc123"
    assert row["photo_dir"] == str(photos_root / "abc123")
