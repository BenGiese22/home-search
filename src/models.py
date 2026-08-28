from dataclasses import dataclass


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
