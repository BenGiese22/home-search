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


# --- Real-payload fixture faithfulness ------------------------------------
# These assert only CURRENT behavior against complete (untrimmed) payloads,
# proving the fixtures are faithful before any extraction changes land.

import json as _json
from pathlib import Path as _Path

FIXTURES = _Path(__file__).parent / "fixtures"


def _fixture(name):
    return _json.loads((FIXTURES / name).read_text())


def test_real_detail_payload_parses_with_current_parser():
    obj = _fixture("detail_hoa_association_yes.json")
    listing = parse_listing_object(obj, "https://example.com/z")
    assert listing.address == "10191 Zenobia Circle"
    assert listing.sqft == 2668
    assert listing.beds > 0


def test_real_payloads_carry_the_structured_fields_the_parser_ignores():
    """Guards the fixtures against being re-trimmed to the parser's field
    list -- the exact mistake that hid the HOA field for a whole feature."""
    obj = _fixture("detail_hoa_association_yes.json")
    charges = obj["price"]["charges"]
    assert any(c["chargeType"] == 2 for c in charges), "HOA charge must survive trimming"
    assert obj["price"]["monthlySalesCharges"] > 0
    assert obj["size"]["aboveGradeTotalAreaSquareFeet"] == 1862
    assert obj["detailedInfo"]["assessorDetails"]["assessorInfo"]["propertyTax"]["tax"]
    assert obj["detailedInfo"]["outdoorSpace"]


def test_no_basement_fixture_omits_below_grade_key_entirely():
    obj = _fixture("detail_no_basement.json")
    assert "belowGradeTotalAreaSquareFeet" not in obj["size"]
    assert obj["size"]["aboveGradeTotalAreaSquareFeet"] == 1867


def test_association_no_fixture_has_tax_charge_but_no_hoa_charge():
    obj = _fixture("detail_sfh_association_no.json")
    charges = obj["price"]["charges"]
    assert any(c["chargeType"] == 0 for c in charges), "tax charge present"
    assert not any(c["chargeType"] == 2 for c in charges), "no HOA charge"


def test_parse_listing_object_extracts_structured_fields_from_real_payload():
    listing = parse_listing_object(_fixture("detail_hoa_association_yes.json"), "u")
    assert listing.hoa_annual == 1000.0
    assert listing.tax_annual > 0
    assert listing.sqft_above_grade == 1862
    assert listing.sqft_below_grade == 725
    assert "Patio" in listing.outdoor_spaces


def test_parse_listing_object_no_basement_keeps_below_grade_none():
    listing = parse_listing_object(_fixture("detail_no_basement.json"), "u")
    assert listing.sqft_above_grade == 1867
    assert listing.sqft_below_grade is None


def test_parse_listing_object_prefers_structured_hoa_over_description():
    obj = _fixture("detail_sfh_association_no.json")
    obj["description"] = "HOA dues are $150/month."
    assert parse_listing_object(obj, "u").hoa_annual == 0.0
