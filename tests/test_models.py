from src.models import Listing


def test_listing_construction():
    listing = Listing(
        listing_id="2145067054346865465",
        address="2765 Canossa Drive",
        city="Broomfield",
        state="CO",
        zip_code="80020",
        price="$649,500",
        beds=4,
        baths=3.5,
        sqft=2268,
        lot_sqft=6726,
        year_built=1999,
        description="Beautifully renovated...",
        amenities=["Renovated Kitchen", "Private Yard"],
        photo_urls=["https://example.com/1.jpg"],
        listing_url="https://www.compass.com/homedetails/2765-Canossa-Dr/",
    )
    assert listing.address == "2765 Canossa Drive"
    assert listing.beds == 4
    assert listing.baths == 3.5
    assert listing.amenities == ["Renovated Kitchen", "Private Yard"]
