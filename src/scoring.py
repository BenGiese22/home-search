from dataclasses import dataclass

from src.models import Listing

NEUTRAL_SCORE = 50.0


# All six pre-HOA weights scaled by (1 - WEIGHT_HOA) = 0.955, which
# preserves their importance relative to each other instead of taking the
# whole cost out of one arbitrary donor factor.
WEIGHT_COMMUTE = 0.2865
WEIGHT_SQFT = 0.191
WEIGHT_CONDITION = 0.191
WEIGHT_OUTDOOR = 0.14325
WEIGHT_ROOM_COUNT = 0.0955
WEIGHT_PARKING = 0.04775
WEIGHT_HOA = 0.045

HOA_NO_FEE_BONUS = 2.5
HOA_MAX_PENALTY = 50.0
# The annual fee costing exactly half of HOA_MAX_PENALTY.
HOA_HALF_PENALTY_AT = 3850.0
# How sharply the curve turns around that point.
HOA_PENALTY_STEEPNESS = 2.6

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


def score_commute(medtronic_minutes: float | None) -> float:
    """The commute factor is Megan's drive to Lafayette, and only that.

    The Denver leg used to carry 20% of this score, normalised against the
    corpus's own min and max. Two problems with that, and Ben settled both on
    2026-09-05: Ben has no commute -- the Denver destination is a coworking
    space he uses occasionally -- and a min/max normalisation makes a
    listing's score depend on which *other* listings happen to be in the
    collection this week, so a house could move in the ranking without
    anything about it changing.

    The leg is still computed, stored and displayed. It just does not vote.
    """
    if medtronic_minutes is None:
        return NEUTRAL_SCORE
    return _medtronic_leg_score(medtronic_minutes)


def finished_sqft(listing: Listing) -> int:
    """Finished living area: above-grade plus any finished below-grade.

    score_sqft exists to compare living space like-for-like, and
    Listing.sqft (Compass's squareFeet) does not: it is the MLS total
    footprint, which counts unfinished basement area. It exceeds
    above+below on 45 of 85 corpus listings, by up to 1,560 sqft -- so a
    home with a large unfinished basement was outranking a fully finished
    home of equal living area.

    sqft_below_grade is None for "no basement" and 0 for "basement with no
    finished area"; both contribute nothing here, but the distinction is
    preserved in storage. Falls back to Listing.sqft when the split is
    unavailable (defensive only -- above-grade is present on 85/85 today).
    """
    if listing.sqft_above_grade is None:
        return listing.sqft
    return listing.sqft_above_grade + (listing.sqft_below_grade or 0)


def score_sqft(sqft: int, sqft_min: int, sqft_max: int) -> float:
    if not sqft:
        return NEUTRAL_SCORE
    if sqft_max <= sqft_min:
        return 100.0
    return _clamp((sqft - sqft_min) / (sqft_max - sqft_min) * 100.0)


def score_condition(
    description: str,
    amenities: list[str],
    year_built: int,
    visual_condition_score: float | None = None,
) -> float:
    if visual_condition_score is not None:
        condition_component = visual_condition_score
    else:
        combined = f"{description} {' '.join(amenities)}"
        condition_component = (
            CONDITION_KEYWORD_HIT_SCORE
            if _has_any_keyword(combined, RENOVATION_KEYWORDS)
            else CONDITION_NO_KEYWORD_SCORE
        )
    if not year_built:
        year_score = NEUTRAL_SCORE
    else:
        normalized = (year_built - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN)
        year_score = _clamp(normalized * 100.0)
    return CONDITION_KEYWORD_WEIGHT * condition_component + CONDITION_YEAR_WEIGHT * year_score


def score_outdoor(
    description: str,
    amenities: list[str],
    visual_outdoor_score: float | None = None,
) -> float:
    if visual_outdoor_score is not None:
        return visual_outdoor_score
    combined = f"{description} {' '.join(amenities)}"
    return (
        OUTDOOR_KEYWORD_HIT_SCORE
        if _has_any_keyword(combined, OUTDOOR_KEYWORDS)
        else OUTDOOR_NO_KEYWORD_SCORE
    )


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


def score_hoa(annual_hoa: float | None) -> float:
    """Unknown (None) is neutral -- no bonus, no penalty -- exactly like
    every other missing-data case in this module. A confirmed absence of
    HOA (0.0) earns a small bonus.

    A positive fee is penalized on a saturating curve rather than a plain
    power curve: very shallow at the low end, so a cheap HOA barely dents
    a home that is good on the things that matter; steepest through the
    middle where the fee starts being real money; then flattening as it
    approaches HOA_MAX_PENALTY without ever reaching it. Because it only
    approaches its asymptote there is no hard cap and so no flat region --
    a $9,600 and a $20,000 HOA still rank differently, which a ceiling
    could not do -- and the sub-score stays above 0 for any finite fee, so
    an expensive HOA drags a listing down without ever vetoing it outright.

    Two knobs, and it matters which one you reach for. HOA_HALF_PENALTY_AT
    slides the whole curve: lower it to make every fee cost more. Only
    HOA_PENALTY_STEEPNESS changes the shape: raising it punishes expensive
    fees harder while making cheap ones cheaper. If only the expensive end
    feels wrong, sliding the curve will quietly make cheap HOAs costly too.
    """
    if annual_hoa is None:
        return NEUTRAL_SCORE
    if annual_hoa <= 0:
        return NEUTRAL_SCORE + HOA_NO_FEE_BONUS
    fee = annual_hoa**HOA_PENALTY_STEEPNESS
    midpoint = HOA_HALF_PENALTY_AT**HOA_PENALTY_STEEPNESS
    return _clamp(NEUTRAL_SCORE - HOA_MAX_PENALTY * fee / (fee + midpoint))


def score_parking(parking_spaces: int) -> float:
    if parking_spaces >= 2:
        return 100.0
    if parking_spaces == 1:
        return 90.0
    return 0.0


def passes_filters(baths: float, lot_sqft: int) -> bool:
    return baths >= MIN_BATHS and lot_sqft >= MIN_LOT_SQFT


@dataclass(kw_only=True)
class CollectionStats:
    """The corpus-relative bounds the rubric normalises against.

    Keyword-only since the Denver bounds were removed from the middle of the
    field list: a positional construction that used to mean
    (sqft_min, sqft_max, denver_min, denver_max) would still be accepted, and
    would silently put commute minutes into room_count_min.
    """

    sqft_min: int
    sqft_max: int
    room_count_min: float = 0.0
    room_count_max: float = 0.0


def compute_collection_stats(
    *,
    sqft_values: list[int],
    room_count_values: list[float] | None = None,
) -> CollectionStats:
    room_count_values = room_count_values or []
    return CollectionStats(
        sqft_min=min(sqft_values) if sqft_values else 0,
        sqft_max=max(sqft_values) if sqft_values else 0,
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
    hoa_score: float
    composite: float
    passes_filters: bool
    has_incomplete_data: bool


def score_listing(
    listing: Listing,
    *,
    medtronic_minutes: float | None,
    stats: CollectionStats,
    visual_condition_score: float | None = None,
    visual_outdoor_score: float | None = None,
) -> ScoreResult:
    commute_score = score_commute(medtronic_minutes)
    sqft_score = score_sqft(finished_sqft(listing), stats.sqft_min, stats.sqft_max)
    condition_score = score_condition(
        listing.description, listing.amenities, listing.year_built, visual_condition_score
    )
    outdoor_score = score_outdoor(listing.description, listing.amenities, visual_outdoor_score)
    room_count_score = score_room_count(
        listing.beds, listing.baths, stats.room_count_min, stats.room_count_max
    )
    parking_score = score_parking(listing.parking_spaces)
    hoa_score = score_hoa(listing.hoa_annual)
    composite = (
        WEIGHT_COMMUTE * commute_score
        + WEIGHT_SQFT * sqft_score
        + WEIGHT_CONDITION * condition_score
        + WEIGHT_OUTDOOR * outdoor_score
        + WEIGHT_ROOM_COUNT * room_count_score
        + WEIGHT_PARKING * parking_score
        + WEIGHT_HOA * hoa_score
    )
    # Same missing-data conditions that trigger a NEUTRAL_SCORE fallback
    # inside score_commute/score_sqft/score_condition — mirrored here so a
    # listing that leaned on a neutral fallback is visibly flagged, per the
    # spec's "Error handling" section.
    has_incomplete_data = (
        medtronic_minutes is None
        # denver_minutes is deliberately absent: it no longer feeds any
        # score, so a missing one is a gap in what is displayed, not a
        # listing that was ranked on a guess.
        or not listing.sqft
        or not listing.year_built
        or not listing.beds
        # None means the listing never disclosed an HOA. A confirmed 0.0 is
        # real data and deliberately does not set this flag.
        or listing.hoa_annual is None
    )
    return ScoreResult(
        commute_score=commute_score,
        sqft_score=sqft_score,
        condition_score=condition_score,
        outdoor_score=outdoor_score,
        room_count_score=room_count_score,
        parking_score=parking_score,
        hoa_score=hoa_score,
        composite=composite,
        passes_filters=passes_filters(listing.baths, listing.lot_sqft),
        has_incomplete_data=has_incomplete_data,
    )
