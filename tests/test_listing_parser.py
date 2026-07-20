import json
from pathlib import Path

from src.listing_parser import parse_listing_object

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canossa_dr_listing.json"
LISTING_URL = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/12NNXK_pid/"


def test_parse_listing_object_from_real_fixture():
    obj = json.loads(FIXTURE_PATH.read_text())
    listing = parse_listing_object(obj, listing_url=LISTING_URL)

    assert listing.listing_id == "2145067054346865465"
    assert listing.address == "2765 Canossa Drive"
    assert listing.city == "Broomfield"
    assert listing.state == "CO"
    assert listing.zip_code == "80020"
    assert listing.price == "$649,500"
    assert listing.beds == 4
    assert listing.baths == 3.5  # 3 full + 1 half
    assert listing.sqft == 2268
    assert listing.lot_sqft == 6726
    assert listing.year_built == 1999
    assert "renovated" in listing.description.lower()
    assert "Renovated Kitchen" in listing.amenities
    assert len(listing.photo_urls) == 2
    assert listing.photo_urls[0].startswith("https://www.compass.com/m/")
    assert listing.listing_url == LISTING_URL


def test_parse_listing_object_missing_optional_fields_defaults_safely():
    obj = {
        "location": {"prettyAddress": "1 Test St", "city": "X", "state": "CO", "zipCode": "00000"},
        "size": {"bedrooms": 2, "fullBathrooms": 1, "squareFeet": 900, "lotSizeInSquareFeet": 0},
        "price": {"formatted": "$1"},
        "media": [],
    }
    listing = parse_listing_object(obj, listing_url="https://example.com/1")
    assert listing.baths == 1.0
    assert listing.year_built == 0
    assert listing.amenities == []
    assert listing.photo_urls == []
