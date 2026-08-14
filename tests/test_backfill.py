from src.backfill import dedupe_by_listing_id
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

OTHER = Listing(
    listing_id="def456",
    address="2 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$500,000",
    beds=3,
    baths=2.0,
    sqft=1800,
    lot_sqft=6000,
    parking_spaces=2,
    year_built=1998,
    description="desc",
    amenities=[],
    photo_urls=[],
    listing_url="https://example.com/listing/def456",
)


def test_dedupe_keeps_single_listing_unchanged():
    assert dedupe_by_listing_id([SAMPLE]) == [SAMPLE]


def test_dedupe_drops_repeated_listing_id():
    assert dedupe_by_listing_id([SAMPLE, SAMPLE]) == [SAMPLE]


def test_dedupe_keeps_first_occurrence_order():
    assert dedupe_by_listing_id([SAMPLE, OTHER, SAMPLE]) == [SAMPLE, OTHER]


def test_dedupe_empty_list_returns_empty_list():
    assert dedupe_by_listing_id([]) == []
