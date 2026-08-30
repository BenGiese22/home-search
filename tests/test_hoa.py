import pytest

from src.hoa import parse_hoa_from_description


@pytest.mark.parametrize(
    "description",
    [
        "With no HOA, friendly neighbors, and greenbelt trails nearby.",
        "Fully remodeled ranch in Broomfield's HOA-free Northmoor neighborhood!",
        "A rare find without an HOA in this part of town.",
        "Quiet cul-de-sac with no hoa and mature trees.",
    ],
)
def test_no_hoa_phrase_returns_zero(description):
    assert parse_hoa_from_description(description) == 0.0


def test_no_mention_at_all_returns_none():
    assert parse_hoa_from_description("Charming ranch with a large backyard.") is None


def test_empty_description_returns_none():
    assert parse_hoa_from_description("") is None


@pytest.mark.parametrize(
    "description,expected",
    [
        ("HOA dues are $150/month for this home.", 1800.0),
        ("HOA fee of $300 per quarter keeps the grounds tidy.", 1200.0),
        ("A $600 semi-annual HOA payment covers the pool.", 1200.0),
        ("HOA: $1,200/year, billed each January.", 1200.0),
        ("Association fee $75 monthly.", 900.0),
        ("HOA $2,400 annually.", 2400.0),
    ],
)
def test_amount_normalizes_to_annual(description, expected):
    assert parse_hoa_from_description(description) == expected


def test_comma_formatted_amount_parses_correctly():
    assert parse_hoa_from_description("HOA is $1,250 per year.") == 1250.0


def test_amount_without_cadence_returns_none():
    """Deliberately conservative: guessing a cadence wrong is a 12x error
    in the scoring input, so an unparseable amount is treated as unknown."""
    assert parse_hoa_from_description("HOA $150, ask agent for details.") is None


def test_hoa_mentioned_with_no_amount_returns_none():
    assert parse_hoa_from_description("HOA required, contact agent.") is None


def test_case_insensitive_matching():
    assert parse_hoa_from_description("Hoa fee $100/MO.") == 1200.0


def test_unrelated_dollar_amount_outside_window_is_not_mistaken_for_hoa():
    description = (
        "Priced at $650,000 per the seller. "
        + "Filler text. " * 20
        + "The neighborhood has an HOA."
    )
    assert parse_hoa_from_description(description) is None


def test_amount_and_no_hoa_phrase_both_present_amount_wins():
    description = "Unlike the no HOA homes nearby, this one has an HOA of $100 per month."
    assert parse_hoa_from_description(description) == 1200.0
