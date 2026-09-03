from dataclasses import dataclass, field


@dataclass
class Listing:
    listing_id: str
    address: str
    city: str
    state: str
    zip_code: str
    price: str
    beds: int
    baths: float
    sqft: int
    lot_sqft: int
    parking_spaces: int
    year_built: int
    description: str
    amenities: list[str]
    photo_urls: list[str]
    listing_url: str
    # Defaulted so Listing(**data) still works for JSON store files saved
    # before these fields existed. property_type is Compass's coarse
    # "GLOBAL" classification (e.g. "Single Family", "Condo") from
    # detailedInfo.propertyType.masterType -- not yet used for filtering,
    # just captured for a future property-type check. localized_status is
    # the collection API's own human-readable status ("Active", "Expired",
    # "Sold", etc.) -- see is_active_status() below.
    property_type: str = ""
    localized_status: str = ""
    # Annual HOA dues in dollars, normalized from whatever cadence the
    # listing quoted. Three distinct states, unlike the other numeric
    # fields on this dataclass: None means unknown/not disclosed, 0.0
    # means confirmed no HOA, and a positive value is a known annual fee.
    # 0.0 is real data here, not a missing-value sentinel -- src.scoring
    # rewards it and treats None as neutral, so the two must not collide.
    hoa_annual: float | None = None
    # Annual property tax in dollars, or None when unknown. Captured for
    # reference and display only -- deliberately NOT scored: it would
    # double-count cost-of-ownership against hoa_annual and the value lens,
    # and the figure is owner-contaminated (assessor and MLS disagree on
    # 15/85 listings, consistent with exemptions that end at sale).
    tax_annual: float | None = None
    # Finished-area square footage split. above is present on 85/85.
    # below is None when Compass omits the key, which happens on exactly
    # the listings reporting Basement: No -- so None means "no basement",
    # while 0 means "basement present, no finished area". Both are real
    # data; squareFeet above is the MLS total footprint and is larger than
    # above+below on 45/85 listings because it counts unfinished space.
    sqft_above_grade: int | None = None
    sqft_below_grade: int | None = None
    # Structured outdoor features (e.g. ["Deck", "Patio"]). Kept because
    # score_outdoor keyword-matches description prose that is empty on
    # 78/85 listings, and because it grounds the photo-scoring prompt.
    outdoor_spaces: list[str] = field(default_factory=list)


def is_active_status(localized_status: str) -> bool:
    """True for a status worth keeping in the purchase-consideration
    dataset: blank/unknown (an empty status isn't proof a listing has gone
    inactive), any "Active"-prefixed status (covers "Active" and "Active /
    Backup"), or "Coming Soon". Everything else -- Pending, Closed,
    Expired, Withdrawn, and any other non-Active status -- is excluded.
    Confirmed live, 2026-08-27: Ben's explicit call after seeing a mixed
    batch of Pending/Closed/Expired/Withdrawn listings alongside one
    Coming Soon and one Active/Backup -- only the latter two should stay.
    Excluding a listing here doesn't stop tracking it entirely: scrape.py/
    check.py still fetch its status on every run, and it reappears
    automatically (as a "new" listing) if it ever comes back to Active.
    See docs/journal/decisions.md for the incident (an expired listing
    with no remaining data) this was first built to catch, and the
    over-broad first attempt (excluding Pending too, before Ben confirmed
    that was actually also wanted) that motivated this docstring."""
    return (
        not localized_status
        or localized_status.startswith("Active")
        or localized_status == "Coming Soon"
    )


def is_pending_status(localized_status: str) -> bool:
    """True for a listing under contract but not yet closed.

    Kept separate from is_active_status rather than folded into it: Ben's
    2026-08-27 call was that Pending is excluded for listings generally, and
    that still holds. This exists only so favorites can be treated
    differently -- see select_present_listings.
    """
    return bool(localized_status) and localized_status.startswith("Pending")


def select_present_listings(listings, pinned_ids, favorite_ids=frozenset()):
    """The listings worth keeping in the dataset this run.

    One function because both scrape.py and check.py delist off this decision
    and they must not drift: a listing that check.py considers absent gets
    hard-deleted, rows, photos and paid vision scores together. That drift is
    exactly what made favorites deletable before they were ever fetched.

    Three ways to stay:

    - pinned, which overrides status entirely (an explicit pin means Ben wants
      it tracked whatever the MLS says);
    - an active status, per is_active_status;
    - a favorite that has gone Pending. A favorite is the strongest interest
      signal in the system and was, until this, the least protected -- only
      Ben or Megan move a listing into favorites, and only they move it back
      out. Pending deals fall through and return to Active, so deleting one
      throws away photos and vision scoring that will be needed again in a
      fortnight. Deliberately Pending only: a Closed or Expired favorite is
      genuinely gone and would otherwise accumulate forever (issue #50).
    """
    return [
        listing for listing in listings
        if listing.listing_id in pinned_ids
        or is_active_status(listing.localized_status)
        or (
            listing.listing_id in favorite_ids
            and is_pending_status(listing.localized_status)
        )
    ]
