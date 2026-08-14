from src.diff import compute_changes, format_report
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
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_listing_not_in_snapshot_is_new():
    report = compute_changes([SAMPLE], before={})

    assert report.new_listings == [SAMPLE]
    assert report.price_changes == []


def test_listing_with_same_price_is_unchanged():
    before = {"abc123": ("$650,000", 650000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert report.price_changes == []


def test_listing_with_different_price_is_a_price_change():
    before = {"abc123": ("$600,000", 600000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert len(report.price_changes) == 1
    change = report.price_changes[0]
    assert change.listing == SAMPLE
    assert change.old_price == "$600,000"
    assert change.new_price == "$650,000"


def test_format_report_lists_new_listing_address_and_url():
    report = compute_changes([SAMPLE], before={})

    text = format_report(report)

    assert "1 new listing" in text
    assert "1 Test St" in text
    assert "https://example.com/listing/abc123" in text


def test_format_report_lists_price_change_old_and_new():
    before = {"abc123": ("$600,000", 600000.0)}
    report = compute_changes([SAMPLE], before)

    text = format_report(report)

    assert "1 price change" in text
    assert "$600,000" in text
    assert "$650,000" in text


def test_duplicate_listing_id_in_fetched_is_reported_once():
    report = compute_changes([SAMPLE, SAMPLE], before={})

    assert report.new_listings == [SAMPLE]


def test_format_report_no_changes_reads_clean():
    report = compute_changes([SAMPLE], before={"abc123": ("$650,000", 650000.0)})

    text = format_report(report)

    assert "0 new listing" in text
    assert "0 price change" in text
