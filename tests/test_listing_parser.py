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
    assert listing.parking_spaces == 2
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
    assert listing.parking_spaces == 0
    assert listing.year_built == 0
    assert listing.amenities == []
    assert listing.photo_urls == []
    assert listing.property_type == ""
    assert listing.localized_status == ""


def test_parse_listing_object_extracts_status_and_property_type():
    # Shape confirmed live, 2026-08-27, against the real collection API
    # response for an expired listing and an active single-family listing.
    obj = {
        "location": {"prettyAddress": "1 Test St", "city": "X", "state": "CO", "zipCode": "00000"},
        "size": {"bedrooms": 2, "fullBathrooms": 1, "squareFeet": 900, "lotSizeInSquareFeet": 0},
        "price": {"formatted": "$1"},
        "media": [],
        "localizedStatus": "Expired",
        "detailedInfo": {
            "propertyType": {
                "masterType": {
                    "GLOBAL": ["Single Family"],
                    "LOCAL_1": ["Residential"],
                    "LOCAL_2": ["Single Family Residence"],
                }
            }
        },
    }
    listing = parse_listing_object(obj, listing_url="https://example.com/1")
    assert listing.localized_status == "Expired"
    assert listing.property_type == "Single Family"


def test_parse_listing_object_property_type_defaults_empty_when_global_list_empty():
    obj = {
        "location": {"prettyAddress": "1 Test St", "city": "X", "state": "CO", "zipCode": "00000"},
        "size": {"bedrooms": 2, "fullBathrooms": 1, "squareFeet": 900, "lotSizeInSquareFeet": 0},
        "price": {"formatted": "$1"},
        "media": [],
        "detailedInfo": {"propertyType": {"masterType": {"GLOBAL": []}}},
    }
    listing = parse_listing_object(obj, listing_url="https://example.com/1")
    assert listing.property_type == ""


def test_parse_listing_object_sets_hoa_annual_none_when_description_is_silent():
    listing = parse_listing_object(
        {"description": "Charming ranch with a large backyard."}, "https://example.com/1"
    )
    assert listing.hoa_annual is None


def test_parse_listing_object_sets_hoa_annual_zero_from_no_hoa_description():
    listing = parse_listing_object(
        {"description": "With no HOA, friendly neighbors and greenbelt trails."},
        "https://example.com/1",
    )
    assert listing.hoa_annual == 0.0


def test_parse_listing_object_sets_hoa_annual_from_dollar_amount_in_description():
    listing = parse_listing_object(
        {"description": "HOA dues are $150/month and cover the pool."},
        "https://example.com/1",
    )
    assert listing.hoa_annual == 1800.0
