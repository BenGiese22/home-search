import json
from pathlib import Path

import pytest

from src.structured_fields import (
    extract_hoa_annual,
    extract_outdoor_spaces,
    extract_sqft_above_grade,
    extract_sqft_below_grade,
    extract_tax_annual,
    parse_dollars,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def payload(charges=None, msc=None, association=None, description="", size=None, detailed=None):
    price = {}
    if charges is not None:
        price["charges"] = charges
    if msc is not None:
        price["monthlySalesCharges"] = msc
    obj = {"price": price, "description": description, "size": size or {}}
    d = dict(detailed or {})
    if association is not None:
        d["listingDetails"] = [{"key": "Association", "value": association}]
    obj["detailedInfo"] = d
    return obj


TAX = {"chargeAmount": 4407, "paymentFrequentType": 3, "chargeType": 0}


# --- HOA precedence chain -------------------------------------------------

def test_association_no_returns_confirmed_zero():
    assert extract_hoa_annual(payload(charges=[TAX], association="No")) == 0.0


def test_association_yes_with_charge_returns_annual_amount():
    obj = payload(charges=[TAX, {"chargeAmount": 1000, "chargeType": 2, "paymentFrequentType": 3}],
                  association="Yes")
    assert extract_hoa_annual(obj) == 1000.0


def test_association_yes_without_any_amount_returns_none_not_zero():
    """The residual-risk guard: an undisclosed fee must never earn the
    no-HOA bonus. Never observed live, but structurally possible."""
    assert extract_hoa_annual(payload(charges=[TAX], association="Yes")) is None


def test_multiple_hoa_charges_sum():
    obj = payload(charges=[TAX,
                           {"chargeAmount": 2796, "chargeType": 2, "paymentFrequentType": 3},
                           {"chargeAmount": 270, "chargeType": 2, "paymentFrequentType": 3}])
    assert extract_hoa_annual(obj) == 3066.0


def test_tax_charge_present_but_no_hoa_charge_returns_zero():
    """The collection-payload no-HOA signature: charges populated (tax
    entry present) with no chargeType:2 entry."""
    assert extract_hoa_annual(payload(charges=[TAX])) == 0.0


def test_empty_charges_and_zero_monthly_sales_charges_returns_none():
    """A sum over nothing is ambiguity, not confirmed absence."""
    assert extract_hoa_annual(payload(charges=[], msc=0)) is None
    assert extract_hoa_annual(payload(msc=0)) is None


def test_monthly_sales_charges_used_when_charges_missing():
    assert extract_hoa_annual(payload(msc=65.13)) == pytest.approx(781.56)


def test_charges_wins_over_disagreeing_monthly_sales_charges():
    obj = payload(charges=[TAX, {"chargeAmount": 1200, "chargeType": 2}], msc=999.0)
    assert extract_hoa_annual(obj) == 1200.0


def test_non_annual_payment_frequency_is_not_trusted():
    """Never observed; guessing a multiplier would be a silent 12x error."""
    obj = payload(charges=[{"chargeAmount": 100, "chargeType": 2, "paymentFrequentType": 1}])
    assert extract_hoa_annual(obj) is None


def test_description_fallback_only_when_structured_absent():
    obj = payload(description="HOA dues are $150/month.")
    assert extract_hoa_annual(obj) == 1800.0


def test_structured_zero_beats_positive_description_regex():
    obj = payload(charges=[TAX], description="HOA dues are $150/month.")
    assert extract_hoa_annual(obj) == 0.0


def test_returns_none_when_nothing_resolves():
    assert extract_hoa_annual({}) is None


# --- Real fixtures --------------------------------------------------------

def test_extracts_hoa_from_real_association_yes_fixture():
    assert extract_hoa_annual(fixture("detail_hoa_association_yes.json")) == 1000.0


def test_zero_from_real_association_no_fixture():
    assert extract_hoa_annual(fixture("detail_sfh_association_no.json")) == 0.0


# --- Property tax ---------------------------------------------------------

def test_tax_annual_from_charge_type_zero():
    assert extract_tax_annual(payload(charges=[TAX])) == 4407.0


def test_tax_annual_falls_back_to_assessor_formatted_string():
    obj = {"detailedInfo": {"assessorDetails": {"assessorInfo": {"propertyTax": {"tax": "$3,799"}}}}}
    assert extract_tax_annual(obj) == 3799.0


def test_tax_annual_none_when_no_source():
    assert extract_tax_annual({}) is None


def test_tax_annual_from_real_fixture():
    assert extract_tax_annual(fixture("detail_hoa_association_yes.json")) > 0


@pytest.mark.parametrize("text,expected", [
    ("$4,407", 4407.0), ("$367", 367.0), ("$1,234.56", 1234.56),
    ("4407", 4407.0), ("", None), ("n/a", None), (None, None), ("-", None),
])
def test_parse_dollars_handles_commas_cents_and_garbage(text, expected):
    assert parse_dollars(text) == expected


# --- Grade-split sqft -----------------------------------------------------

def test_grade_sqft_extracted_from_real_fixture():
    obj = fixture("detail_hoa_association_yes.json")
    assert extract_sqft_above_grade(obj) == 1862
    assert extract_sqft_below_grade(obj) == 725


def test_below_grade_absent_returns_none_not_zero():
    """Absence means 'no basement' (verified against Basement: No on 11/11),
    which is real data -- it must stay distinct from a finished-zero."""
    obj = fixture("detail_no_basement.json")
    assert extract_sqft_above_grade(obj) == 1867
    assert extract_sqft_below_grade(obj) is None


def test_below_grade_zero_preserved_as_zero():
    obj = payload(size={"aboveGradeTotalAreaSquareFeet": 1739,
                        "belowGradeTotalAreaSquareFeet": 0})
    assert extract_sqft_below_grade(obj) == 0


def test_above_grade_none_when_absent():
    assert extract_sqft_above_grade({}) is None


# --- Outdoor spaces -------------------------------------------------------

def test_outdoor_spaces_from_real_fixture():
    spaces = extract_outdoor_spaces(fixture("detail_no_basement.json"))
    assert spaces == ["Deck", "Patio", "Private Outdoor Space"]


def test_outdoor_spaces_empty_when_absent():
    assert extract_outdoor_spaces({}) == []
