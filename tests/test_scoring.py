from src.scoring import (
    passes_filters,
    score_commute,
    score_condition,
    score_outdoor,
    score_parking,
    score_sqft,
)


def test_score_commute_full_marks_at_or_under_twenty_minutes():
    # denver leg pinned at its own minimum, so it legitimately scores 100 on
    # its own terms — isolates the medtronic leg's behavior at 20 minutes
    assert score_commute(20.0, 15.0, 15.0, 30.0) == 100.0


def test_score_commute_linear_slide_between_twenty_and_thirty():
    # medtronic leg only: 25min -> 70; denver leg pinned at its own min -> 100
    result = score_commute(25.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 70.0 + 0.2 * 100.0


def test_score_commute_forty_minutes_medtronic_leg_is_zero():
    result = score_commute(40.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 0.0 + 0.2 * 100.0


def test_score_commute_beyond_forty_minutes_stays_zero():
    result = score_commute(55.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 0.0 + 0.2 * 100.0


def test_score_commute_denver_leg_min_max_normalized():
    # denver leg at the collection's max minutes scores 0
    result = score_commute(20.0, 30.0, 15.0, 30.0)
    assert result == 0.8 * 100.0 + 0.2 * 0.0


def test_score_commute_missing_data_is_neutral():
    result = score_commute(None, None, 15.0, 30.0)
    assert result == 50.0


def test_score_commute_no_variance_in_denver_range_scores_full():
    result = score_commute(20.0, 20.0, 20.0, 20.0)
    assert result == 100.0


def test_score_sqft_min_max_normalizes_across_collection():
    assert score_sqft(2000, 1000, 3000) == 50.0
    assert score_sqft(1000, 1000, 3000) == 0.0
    assert score_sqft(3000, 1000, 3000) == 100.0


def test_score_sqft_missing_is_neutral():
    assert score_sqft(0, 1000, 3000) == 50.0


def test_score_sqft_no_variance_scores_full():
    assert score_sqft(2000, 2000, 2000) == 100.0


def test_score_condition_renovation_keyword_dominates():
    high = score_condition("Beautifully Renovated kitchen", [], 1960)
    low = score_condition("Original condition", [], 1960)
    assert high > low


def test_score_condition_keyword_match_is_case_insensitive_and_checks_amenities():
    result = score_condition("Charming home", ["Fully Remodeled"], 1980)
    year_component = 0.2 * ((1980 - 1955) / (2005 - 1955) * 100.0)
    assert result == 0.8 * 100.0 + year_component


def test_score_condition_missing_year_built_is_neutral_secondary_signal():
    with_keyword_no_year = score_condition("Renovated", [], 0)
    assert with_keyword_no_year == 0.8 * 100.0 + 0.2 * 50.0


def test_score_condition_newer_year_scores_higher_without_keyword():
    older = score_condition("Original condition", [], 1955)
    newer = score_condition("Original condition", [], 2005)
    assert newer > older


def test_score_outdoor_keyword_hit_scores_high():
    result = score_outdoor("Private yard with mature trees", [])
    assert result == 100.0


def test_score_outdoor_checks_amenities_too():
    result = score_outdoor("Charming home", ["Great for Entertaining"])
    assert result == 100.0


def test_score_outdoor_no_keyword_is_weak_not_zero():
    result = score_outdoor("A house", [])
    assert 0.0 < result < 100.0


def test_score_parking_two_or_more_spaces_is_full():
    assert score_parking(2) == 100.0
    assert score_parking(4) == 100.0


def test_score_parking_one_space_is_high_but_not_full():
    assert score_parking(1) == 90.0


def test_score_parking_zero_or_missing_is_zero():
    assert score_parking(0) == 0.0


def test_passes_filters_requires_both_thresholds():
    assert passes_filters(baths=2.0, lot_sqft=6000) is True
    assert passes_filters(baths=1.5, lot_sqft=6000) is False
    assert passes_filters(baths=2.0, lot_sqft=5999) is False
