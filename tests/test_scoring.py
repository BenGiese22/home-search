import pytest

from src.models import Listing
from src.scoring import (
    CONDITION_KEYWORD_WEIGHT,
    CONDITION_YEAR_WEIGHT,
    CollectionStats,
    WEIGHT_COMMUTE,
    WEIGHT_CONDITION,
    WEIGHT_OUTDOOR,
    WEIGHT_PARKING,
    WEIGHT_ROOM_COUNT,
    WEIGHT_SQFT,
    YEAR_BUILT_MAX,
    YEAR_BUILT_MIN,
    compute_collection_stats,
    passes_filters,
    score_commute,
    score_condition,
    score_listing,
    score_outdoor,
    score_parking,
    score_room_count,
    score_sqft,
)

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$650,000",
    beds=4,
    baths=2.5,
    sqft=2000,
    lot_sqft=6500,
    parking_spaces=2,
    year_built=1980,
    description="Renovated kitchen, private yard with mature trees",
    amenities=["Garage"],
    photo_urls=[],
    listing_url="https://example.com/listing/abc123",
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


def test_score_room_count_min_max_normalizes_across_collection():
    assert score_room_count(beds=4, baths=2.0, room_count_min=4.0, room_count_max=8.0) == 50.0
    assert score_room_count(beds=2, baths=2.0, room_count_min=4.0, room_count_max=8.0) == 0.0
    assert score_room_count(beds=6, baths=2.0, room_count_min=4.0, room_count_max=8.0) == 100.0


def test_score_room_count_missing_is_neutral():
    assert score_room_count(beds=0, baths=0.0, room_count_min=4.0, room_count_max=8.0) == 50.0


def test_score_room_count_no_variance_scores_full():
    assert score_room_count(beds=4, baths=2.0, room_count_min=6.0, room_count_max=6.0) == 100.0


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


def test_compute_collection_stats_returns_min_and_max():
    stats = compute_collection_stats([1000, 2000, 3000], [10.0, 20.0, 30.0])

    assert stats == CollectionStats(
        sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0
    )


def test_compute_collection_stats_returns_room_count_min_and_max():
    stats = compute_collection_stats([1000, 2000], [10.0, 20.0], [4.0, 6.5, 8.0])

    assert stats.room_count_min == 4.0
    assert stats.room_count_max == 8.0


def test_compute_collection_stats_handles_empty_input():
    stats = compute_collection_stats([], [])

    assert stats == CollectionStats(0, 0, 0.0, 0.0)


def test_score_listing_combines_sub_scores_with_named_weights():
    stats = CollectionStats(sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0)

    result = score_listing(LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats)

    expected = (
        WEIGHT_COMMUTE * result.commute_score
        + WEIGHT_SQFT * result.sqft_score
        + WEIGHT_CONDITION * result.condition_score
        + WEIGHT_OUTDOOR * result.outdoor_score
        + WEIGHT_ROOM_COUNT * result.room_count_score
        + WEIGHT_PARKING * result.parking_score
    )
    assert result.composite == expected


def test_score_listing_sets_passes_filters_flag():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)

    passing = score_listing(LISTING, 18.0, 15.0, stats)
    failing_listing = LISTING.__class__(**{**LISTING.__dict__, "baths": 1.0})
    failing = score_listing(failing_listing, 18.0, 15.0, stats)

    assert passing.passes_filters is True
    assert failing.passes_filters is False


def test_score_listing_flags_incomplete_data_when_commute_missing():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)

    missing_medtronic = score_listing(LISTING, None, 15.0, stats)
    missing_denver = score_listing(LISTING, 18.0, None, stats)

    assert missing_medtronic.has_incomplete_data is True
    assert missing_denver.has_incomplete_data is True


def test_score_listing_flags_incomplete_data_when_sqft_missing():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)
    no_sqft = LISTING.__class__(**{**LISTING.__dict__, "sqft": 0})

    result = score_listing(no_sqft, 18.0, 15.0, stats)

    assert result.has_incomplete_data is True


def test_score_listing_flags_incomplete_data_when_year_built_missing():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)
    no_year_built = LISTING.__class__(**{**LISTING.__dict__, "year_built": 0})

    result = score_listing(no_year_built, 18.0, 15.0, stats)

    assert result.has_incomplete_data is True


def test_score_listing_flags_incomplete_data_when_beds_missing():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)
    no_beds = LISTING.__class__(**{**LISTING.__dict__, "beds": 0})

    result = score_listing(no_beds, 18.0, 15.0, stats)

    assert result.has_incomplete_data is True


def test_score_listing_has_incomplete_data_false_when_all_present():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)

    result = score_listing(LISTING, 18.0, 15.0, stats)

    assert result.has_incomplete_data is False


def test_score_condition_uses_visual_score_when_provided():
    with_visual = score_condition("no renovation keywords here", [], 1980, visual_condition_score=90.0)
    without_visual = score_condition("no renovation keywords here", [], 1980)

    assert with_visual > without_visual


def test_score_condition_visual_score_replaces_keyword_component_exactly():
    year_score = (1980 - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN) * 100.0
    expected = CONDITION_KEYWORD_WEIGHT * 90.0 + CONDITION_YEAR_WEIGHT * year_score

    result = score_condition("irrelevant text with no keywords", [], 1980, visual_condition_score=90.0)
    assert result == pytest.approx(expected)


def test_score_outdoor_uses_visual_score_when_provided():
    assert score_outdoor("no outdoor keywords here", [], visual_outdoor_score=75.0) == 75.0


def test_score_outdoor_falls_back_to_keywords_when_visual_score_absent():
    assert score_outdoor("Private yard with mature trees", []) == 100.0


def test_score_listing_passes_visual_scores_through():
    stats = CollectionStats(sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0)

    with_visual = score_listing(
        LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats,
        visual_condition_score=90.0, visual_outdoor_score=80.0,
    )
    without_visual = score_listing(LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats)

    assert with_visual.condition_score != without_visual.condition_score
    assert with_visual.outdoor_score != without_visual.outdoor_score
    # everything else this task doesn't touch should be identical
    assert with_visual.room_count_score == without_visual.room_count_score
    assert with_visual.parking_score == without_visual.parking_score
    assert with_visual.has_incomplete_data == without_visual.has_incomplete_data
