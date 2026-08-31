import json
from pathlib import Path

from src.models import Listing
from src.store import needs_field_backfill, delete_stored_listing, is_scraped, load_all_listings, save_listing

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


def test_needs_field_backfill_true_when_key_absent(tmp_path: Path):
    """A listing stored before the field existed has no such key at all."""
    (tmp_path / "abc.json").write_text(json.dumps({"listing_id": "abc", "address": "x"}))
    assert needs_field_backfill(tmp_path, "abc", ("tax_annual",)) is True


def test_needs_field_backfill_true_when_value_is_null(tmp_path: Path):
    (tmp_path / "abc.json").write_text(json.dumps({"listing_id": "abc", "tax_annual": None}))
    assert needs_field_backfill(tmp_path, "abc", ("tax_annual",)) is True


def test_needs_field_backfill_false_when_all_fields_present(tmp_path: Path):
    (tmp_path / "abc.json").write_text(
        json.dumps({"listing_id": "abc", "tax_annual": 4407.0, "hoa_annual": 0.0})
    )
    assert needs_field_backfill(tmp_path, "abc", ("tax_annual", "hoa_annual")) is False


def test_needs_field_backfill_false_for_unscraped_listing(tmp_path: Path):
    """New work, not backfill -- the normal is_scraped path handles it."""
    assert needs_field_backfill(tmp_path, "nope", ("tax_annual",)) is False


def test_needs_field_backfill_true_for_corrupt_store_file(tmp_path: Path):
    (tmp_path / "abc.json").write_text("{not json")
    assert needs_field_backfill(tmp_path, "abc", ("tax_annual",)) is True
