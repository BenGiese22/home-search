"""Extraction of structured fields from Compass's embedded listing payload.

Compass ships far more than the parser historically read: a ~45KB object with
~164 leaf fields, including HOA dues, property tax, and a finished-area square
footage split. None of it was used, because the test fixtures had been trimmed
to exactly the fields the parser already consumed -- see
docs/superpowers/plans/2026-08-30-structured-listing-fields.md.

Two payload shapes reach this module and they differ. The collection API
response (how ~83 of 85 listings enter) carries `price.charges`,
`price.monthlySalesCharges`, `detailedInfo.assessorDetails` and the grade-split
sqft, but NOT `description`, `keyDetails`, `listingDetails`, or
`regionalKeyDetails`. The detail-page embed carries everything. Every extractor
here degrades when its field is absent rather than assuming a shape.
"""

import re

from src.hoa import parse_hoa_from_description

CHARGE_TYPE_TAX = 0
CHARGE_TYPE_HOA = 2
# Compass has only ever emitted 3 (annual) here, and it describes the cadence
# of the already-aggregated total. Any other value means the aggregation is
# not what we think it is, so we decline to guess a multiplier -- the same
# conservative posture src/hoa.py takes with an amount that has no cadence.
PAYMENT_FREQUENCY_ANNUAL = 3

_DOLLARS_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def parse_dollars(text: str | None) -> float | None:
    """"$4,407" -> 4407.0. None for anything without a number (e.g. "-")."""
    if not text:
        return None
    match = _DOLLARS_RE.search(str(text).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _detail_value(obj: dict, key: str) -> str | None:
    """Look a key out of detailedInfo's several {key,value} list blocks.
    Detail payloads only -- collection payloads carry none of these."""
    detailed = obj.get("detailedInfo") or {}
    for block in ("listingDetails", "regionalKeyDetails", "keyDetails"):
        entries = detailed.get(block)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("key") == key:
                    return entry.get("value")
        elif isinstance(entries, dict) and key in entries:
            return entries[key]
    return None


def _charges(obj: dict) -> list[dict]:
    charges = (obj.get("price") or {}).get("charges")
    return [c for c in charges if isinstance(c, dict)] if isinstance(charges, list) else []


def _sum_charges(obj: dict, charge_type: int) -> float | None:
    """Summed annual amount for a charge type, or None when the feed gave us
    nothing to sum. Returns None rather than 0.0 for an absent charges list:
    a sum over nothing is ambiguity, not a confirmed absence."""
    charges = _charges(obj)
    if not charges:
        return None
    matching = [c for c in charges if c.get("chargeType") == charge_type]
    if not matching:
        return 0.0
    for charge in matching:
        frequency = charge.get("paymentFrequentType")
        if frequency is not None and frequency != PAYMENT_FREQUENCY_ANNUAL:
            return None
    return float(sum(c.get("chargeAmount", 0) or 0 for c in matching))


def extract_hoa_annual(obj: dict) -> float | None:
    """Annual HOA dues in dollars, or 0.0 for a confirmed absence, or None
    when genuinely unknown.

    Precedence, each rung skipped when its field is absent from the payload
    shape at hand:
      1. `Association` Yes/No -- ground truth for HOA *presence* (detail
         payloads only). "No" is authoritative. "Yes" means the amount must
         come from a lower rung, and if none is found the answer is None,
         NEVER 0.0 -- otherwise an undisclosed fee would earn the no-HOA bonus.
      2. charges[chargeType==2] -- the authoritative annual amount, summed
         across multiple associations (sub + master HOAs both appear).
         A populated charges list with no HOA entry is a confirmed 0.0: it is
         the exact structural signature of Association: No, corroborated on
         77/77 no-HOA listings.
      3. monthlySalesCharges * 12 -- derived from the same source, so it
         loses to rung 2 on disagreement, but serves when charges is absent.
      4. The description regex, for the handful of listings with prose.
         A structured 0.0 beats a positive regex hit.
    """
    association = _detail_value(obj, "Association")
    has_association = association in ("Yes", "No")

    if association == "No":
        return 0.0

    charged = _sum_charges(obj, CHARGE_TYPE_HOA)
    if charged:
        return charged

    monthly = (obj.get("price") or {}).get("monthlySalesCharges")
    if charged is None and isinstance(monthly, (int, float)) and monthly > 0:
        return round(float(monthly) * 12.0, 2)

    if association == "Yes":
        # An HOA exists but no amount is resolvable. Unknown, not free.
        return None

    if charged == 0.0:
        return 0.0

    if not has_association:
        from_description = parse_hoa_from_description(obj.get("description") or "")
        if from_description is not None:
            return from_description

    return None


def extract_tax_annual(obj: dict) -> float | None:
    """Annual property tax in dollars.

    Prefers charges[chargeType==0] -- numeric, present on 85/85, and matching
    the figure Compass displays. The assessor record is a fallback only: the
    two disagree on 15/85 listings, sometimes threefold, in a pattern
    consistent with owner-specific exemptions that do not survive a sale.
    """
    charged = _sum_charges(obj, CHARGE_TYPE_TAX)
    if charged:
        return charged
    assessor = (
        ((obj.get("detailedInfo") or {}).get("assessorDetails") or {})
        .get("assessorInfo", {})
        .get("propertyTax", {})
    )
    return parse_dollars(assessor.get("tax"))


def extract_sqft_above_grade(obj: dict) -> int | None:
    value = (obj.get("size") or {}).get("aboveGradeTotalAreaSquareFeet")
    return int(value) if isinstance(value, (int, float)) else None


def extract_sqft_below_grade(obj: dict) -> int | None:
    """None means no basement -- the key is absent on exactly the listings
    where Compass reports Basement: No (11/11). A 0 means a basement exists
    with no finished area, which is different information and is preserved."""
    value = (obj.get("size") or {}).get("belowGradeTotalAreaSquareFeet")
    return int(value) if isinstance(value, (int, float)) else None


def extract_outdoor_spaces(obj: dict) -> list[str]:
    """Structured outdoor features, e.g. ["Deck", "Patio"]. Worth capturing
    because score_outdoor currently keyword-matches free-text description
    prose that is empty on 78 of 85 listings."""
    spaces = (obj.get("detailedInfo") or {}).get("outdoorSpace")
    return [str(s) for s in spaces] if isinstance(spaces, list) else []
