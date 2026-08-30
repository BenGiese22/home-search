import re

# Compass's structured listing JSON has no HOA field at all -- confirmed
# against both payload shapes (the single-listing page embed and the
# collection API) and against every already-scraped listing in
# data/listings/ on 2026-08-30. Free-text description prose is the only
# source of HOA information available today, so this module mines it.
_NO_HOA_RE = re.compile(
    r"\bno\s+hoa\b|\bhoa[-\s]?free\b|\bwithout\s+(?:an?\s+)?hoa\b", re.IGNORECASE
)
_MENTION_RE = re.compile(r"\bhoa\b|\bassociation\s+fee\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

# Ordered most- to least-specific; the first match wins. Each pattern maps a
# cadence to the number of payments per year, so amount * multiplier is the
# annual figure.
_CADENCE_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"per\s*month|/\s*mo(?:nth)?s?\b|monthly", re.IGNORECASE), 12.0),
    (re.compile(r"per\s*quarter|/\s*q(?:tr|uarter)?\b|quarterly", re.IGNORECASE), 4.0),
    (re.compile(r"semi[-\s]?annual(?:ly)?|twice\s+a\s+year", re.IGNORECASE), 2.0),
    (re.compile(r"per\s*year|/\s*y(?:ea)?rs?\b|annually|per\s*annum|yearly", re.IGNORECASE), 1.0),
]
_PROXIMITY_WINDOW = 60


def parse_hoa_from_description(description: str) -> float | None:
    """Best-effort annual HOA fee mined from free-text listing prose.

    Returns:
      - a positive annual dollar figure, when a $amount sits within
        _PROXIMITY_WINDOW characters of an "HOA"/"association fee" mention
        AND an unambiguous cadence keyword appears in that same window
      - 0.0 when the description explicitly states there is no HOA
      - None when neither pattern matches -- this covers both "never
        mentions HOA" and "mentions HOA but with no parseable amount or
        cadence" (e.g. "HOA required, ask agent")

    None is a deliberately conservative "unknown", never a guess. Reading a
    cadence wrong is a 12x error in the scoring input, so an amount with no
    cadence beside it is treated exactly like no amount at all. Callers
    must keep None (unknown) distinct from 0.0 (confirmed no HOA); see
    src.scoring.score_hoa, which scores them differently on purpose.

    An amount-plus-cadence is checked before the no-HOA phrasing, so a fee
    quoted near an incidental "no HOA" aside still wins -- it is the more
    specific and more informative signal of the two.
    """
    for match in _MENTION_RE.finditer(description):
        window = description[
            max(0, match.start() - _PROXIMITY_WINDOW) : match.end() + _PROXIMITY_WINDOW
        ]
        amount_match = _AMOUNT_RE.search(window)
        if not amount_match:
            continue
        cadence = next(
            (multiplier for pattern, multiplier in _CADENCE_PATTERNS if pattern.search(window)),
            None,
        )
        if cadence is None:
            continue
        return float(amount_match.group(1).replace(",", "")) * cadence

    if _NO_HOA_RE.search(description):
        return 0.0

    return None
