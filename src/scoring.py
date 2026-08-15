NEUTRAL_SCORE = 50.0

MEDTRONIC_LEG_WEIGHT = 0.8
DENVER_LEG_WEIGHT = 0.2

YEAR_BUILT_MIN = 1955
YEAR_BUILT_MAX = 2005
CONDITION_KEYWORD_WEIGHT = 0.8
CONDITION_YEAR_WEIGHT = 0.2

RENOVATION_KEYWORDS = [
    "renovated",
    "updated kitchen",
    "remodeled",
    "new roof",
    "newly renovated",
    "fully updated",
    "gut renovated",
]

OUTDOOR_KEYWORDS = [
    "mature trees",
    "private yard",
    "backyard",
    "open floor plan",
    "entertaining",
    "outdoor living",
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
    keyword_score = 100.0 if _has_any_keyword(combined, RENOVATION_KEYWORDS) else 0.0
    if not year_built:
        year_score = NEUTRAL_SCORE
    else:
        normalized = (year_built - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN)
        year_score = _clamp(normalized * 100.0)
    return CONDITION_KEYWORD_WEIGHT * keyword_score + CONDITION_YEAR_WEIGHT * year_score


def score_outdoor(description: str, amenities: list[str]) -> float:
    combined = f"{description} {' '.join(amenities)}"
    return OUTDOOR_KEYWORD_HIT_SCORE if _has_any_keyword(combined, OUTDOOR_KEYWORDS) else OUTDOOR_NO_KEYWORD_SCORE


def score_parking(parking_spaces: int) -> float:
    if parking_spaces >= 2:
        return 100.0
    if parking_spaces == 1:
        return 90.0
    return 0.0


def passes_filters(baths: float, lot_sqft: int) -> bool:
    return baths >= MIN_BATHS and lot_sqft >= MIN_LOT_SQFT
