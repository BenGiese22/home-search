from pathlib import Path

from src.models import Listing
from src.store import delete_stored_listing, is_scraped, load_all_listings, save_listing

SAMPLE = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$1",
    beds=2,
    baths=1.0,
    sqft=900,
    lot_sqft=1000,
    parking_spaces=1,
    year_built=2000,
    description="desc",
    amenities=["A", "B"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_is_scraped_false_when_never_saved(tmp_path: Path):
    assert is_scraped(tmp_path, "abc123") is False


def test_save_and_is_scraped(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)
    assert is_scraped(tmp_path, "abc123") is True


def test_load_all_listings_round_trips(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)
    loaded = load_all_listings(tmp_path)
    assert loaded == [SAMPLE]


def test_load_all_listings_empty_dir_returns_empty_list(tmp_path: Path):
    assert load_all_listings(tmp_path / "does-not-exist") == []


def test_load_all_listings_skips_corrupt_file(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)
    (tmp_path / "corrupt.json").write_text("{not valid json")

    loaded = load_all_listings(tmp_path)

    assert loaded == [SAMPLE]


def test_delete_stored_listing_removes_file(tmp_path: Path):
    save_listing(tmp_path, SAMPLE)

    delete_stored_listing(tmp_path, "abc123")

    assert is_scraped(tmp_path, "abc123") is False


def test_delete_stored_listing_is_safe_when_file_missing(tmp_path: Path):
    delete_stored_listing(tmp_path, "nope")  # should not raise
