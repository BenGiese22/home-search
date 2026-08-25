from dataclasses import dataclass

from src.models import Listing

NEUTRAL_SCORE = 50.0

MEDTRONIC_LEG_WEIGHT = 0.8
DENVER_LEG_WEIGHT = 0.2

WEIGHT_COMMUTE = 0.30
WEIGHT_SQFT = 0.20
WEIGHT_CONDITION = 0.20
WEIGHT_OUTDOOR = 0.15
WEIGHT_ROOM_COUNT = 0.10
WEIGHT_PARKING = 0.05

YEAR_BUILT_MIN = 1955
YEAR_BUILT_MAX = 2005
CONDITION_KEYWORD_WEIGHT = 0.8
CONDITION_YEAR_WEIGHT = 0.2

RENOVATION_KEYWORDS = [
    "renovated",
    "updated kitchen",
    "remodeled",
    "new roof",
    "fully updated",
    "updated",
    "upgraded",
    "new appliances",
    "new flooring",
    "new fixtures",
    "move-in ready",
    "turnkey",
    "freshly painted",
]
CONDITION_KEYWORD_HIT_SCORE = 100.0
# Same reasoning as OUTDOOR_NO_KEYWORD_SCORE below: a missing keyword isn't
# proof of poor condition, just a placeholder until photo scoring exists.
# Previously hardcoded to 0.0, which punished a keyword miss as hard as a
# real negative signal — house-tour calibration found a real listing
# (12307 Utica St) with good actual condition but zero keyword hits, scoring
# near the bottom purely from this fallback. Softened to match outdoor's
# already-weak-not-zero pattern.
CONDITION_NO_KEYWORD_SCORE = 40.0

OUTDOOR_KEYWORDS = [
    "mature trees",
    "private yard",
    "backyard",
    "open floor plan",
    "entertaining",
    "outdoor living",
    "landscaped",
    "landscaping",
    "patio",
    "deck",
    "garden",
    "fire pit",
    "hot tub",
]
OUTDOOR_KEYWORD_HIT_SCORE = 100.0
# Absence of these phrases isn't proof there's no yard — this is an
# explicitly weak placeholder until photo scoring exists, so a miss
# isn't punished as heavily as a real negative signal would be.
OUTDOOR_NO_KEYWORD_SCORE = 40.0

MIN_BATHS = 2.0
MIN_LOT_SQFT = 6000


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _has_any_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _medtronic_leg_score(minutes: float) -> float:
    if minutes <= 20:
        return 100.0
    if minutes <= 30:
        return 100.0 - (minutes - 20) * 6.0
    if minutes <= 40:
        return 40.0 - (minutes - 30) * 4.0
    return 0.0


def _denver_leg_score(minutes: float, denver_min: float, denver_max: float) -> float:
    if denver_max <= denver_min:
        return 100.0
    normalized = (denver_max - minutes) / (denver_max - denver_min)
    return _clamp(normalized * 100.0)


def score_commute(
    medtronic_minutes: float | None,
    denver_minutes: float | None,
    denver_min: float,
    denver_max: float,
) -> float:
    medtronic_score = (
        _medtronic_leg_score(medtronic_minutes) if medtronic_minutes is not None else NEUTRAL_SCORE
    )
    denver_score = (
        _denver_leg_score(denver_minutes, denver_min, denver_max)
        if denver_minutes is not None
        else NEUTRAL_SCORE
    )
    return MEDTRONIC_LEG_WEIGHT * medtronic_score + DENVER_LEG_WEIGHT * denver_score


def score_sqft(sqft: int, sqft_min: int, sqft_max: int) -> float:
    if not sqft:
        return NEUTRAL_SCORE
    if sqft_max <= sqft_min:
        return 100.0
    return _clamp((sqft - sqft_min) / (sqft_max - sqft_min) * 100.0)


def score_condition(description: str, amenities: list[str], year_built: int) -> float:
    combined = f"{description} {' '.join(amenities)}"
    keyword_score = (
        CONDITION_KEYWORD_HIT_SCORE
        if _has_any_keyword(combined, RENOVATION_KEYWORDS)
        else CONDITION_NO_KEYWORD_SCORE
    )
    if not year_built:
        year_score = NEUTRAL_SCORE
    else:
        normalized = (year_built - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN)
        year_score = _clamp(normalized * 100.0)
    return CONDITION_KEYWORD_WEIGHT * keyword_score + CONDITION_YEAR_WEIGHT * year_score


def score_outdoor(description: str, amenities: list[str]) -> float:
    combined = f"{description} {' '.join(amenities)}"
    return OUTDOOR_KEYWORD_HIT_SCORE if _has_any_keyword(combined, OUTDOOR_KEYWORDS) else OUTDOOR_NO_KEYWORD_SCORE


def score_room_count(beds: int, baths: float, room_count_min: float, room_count_max: float) -> float:
    """Min-max normalized against the collection's own beds+baths spread,
    same treatment as score_sqft. Exists because sqft alone doesn't capture
    "enough distinct rooms for our needs" -- house-tour calibration found two
    listings rejected specifically for feeling short on rooms (bath count,
    then overall bed+bath count) despite adequate square footage."""
    total = (beds or 0) + (baths or 0)
    if not total:
        return NEUTRAL_SCORE
    if room_count_max <= room_count_min:
        return 100.0
    return _clamp((total - room_count_min) / (room_count_max - room_count_min) * 100.0)


def score_parking(parking_spaces: int) -> float:
    if parking_spaces >= 2:
        return 100.0
    if parking_spaces == 1:
        return 90.0
    return 0.0


def passes_filters(baths: float, lot_sqft: int) -> bool:
    return baths >= MIN_BATHS and lot_sqft >= MIN_LOT_SQFT


@dataclass
class CollectionStats:
    sqft_min: int
    sqft_max: int
    denver_minutes_min: float
    denver_minutes_max: float
    room_count_min: float = 0.0
    room_count_max: float = 0.0


def compute_collection_stats(
    sqft_values: list[int],
    denver_minutes_values: list[float],
    room_count_values: list[float] | None = None,
) -> CollectionStats:
    room_count_values = room_count_values or []
    return CollectionStats(
        sqft_min=min(sqft_values) if sqft_values else 0,
        sqft_max=max(sqft_values) if sqft_values else 0,
        denver_minutes_min=min(denver_minutes_values) if denver_minutes_values else 0.0,
        denver_minutes_max=max(denver_minutes_values) if denver_minutes_values else 0.0,
        room_count_min=min(room_count_values) if room_count_values else 0.0,
        room_count_max=max(room_count_values) if room_count_values else 0.0,
    )


@dataclass
class ScoreResult:
    commute_score: float
    sqft_score: float
    condition_score: float
    outdoor_score: float
    room_count_score: float
    parking_score: float
    composite: float
    passes_filters: bool
    has_incomplete_data: bool


def score_listing(
    listing: Listing,
    medtronic_minutes: float | None,
    denver_minutes: float | None,
    stats: CollectionStats,
) -> ScoreResult:
    commute_score = score_commute(
        medtronic_minutes, denver_minutes, stats.denver_minutes_min, stats.denver_minutes_max
    )
    sqft_score = score_sqft(listing.sqft, stats.sqft_min, stats.sqft_max)
    condition_score = score_condition(listing.description, listing.amenities, listing.year_built)
    outdoor_score = score_outdoor(listing.description, listing.amenities)
    room_count_score = score_room_count(
        listing.beds, listing.baths, stats.room_count_min, stats.room_count_max
    )
    parking_score = score_parking(listing.parking_spaces)
    composite = (
        WEIGHT_COMMUTE * commute_score
        + WEIGHT_SQFT * sqft_score
        + WEIGHT_CONDITION * condition_score
        + WEIGHT_OUTDOOR * outdoor_score
        + WEIGHT_ROOM_COUNT * room_count_score
        + WEIGHT_PARKING * parking_score
    )
    # Same missing-data conditions that trigger a NEUTRAL_SCORE fallback
    # inside score_commute/score_sqft/score_condition — mirrored here so a
    # listing that leaned on a neutral fallback is visibly flagged, per the
    # spec's "Error handling" section.
    has_incomplete_data = (
        medtronic_minutes is None
        or denver_minutes is None
        or not listing.sqft
        or not listing.year_built
        or not listing.beds
    )
    return ScoreResult(
        commute_score=commute_score,
        sqft_score=sqft_score,
        condition_score=condition_score,
        outdoor_score=outdoor_score,
        room_count_score=room_count_score,
        parking_score=parking_score,
        composite=composite,
        passes_filters=passes_filters(listing.baths, listing.lot_sqft),
        has_incomplete_data=has_incomplete_data,
    )
