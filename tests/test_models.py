import pytest
from src.models import Listing, is_active_status, is_pending_status, select_present_listings


def test_listing_construction():
    listing = Listing(
        listing_id="2145067054346865465",
        address="2765 Canossa Drive",
        city="Broomfield",
        state="CO",
        zip_code="80020",
        price="$649,500",
        beds=4,
        baths=3.5,
        sqft=2268,
        lot_sqft=6726,
        parking_spaces=2,
        year_built=1999,
        description="Beautifully renovated...",
        amenities=["Renovated Kitchen", "Private Yard"],
        photo_urls=["https://example.com/1.jpg"],
        listing_url="https://www.compass.com/homedetails/2765-Canossa-Dr/",
    )
    assert listing.address == "2765 Canossa Drive"
    assert listing.beds == 4
    assert listing.baths == 3.5
    assert listing.parking_spaces == 2
    assert listing.amenities == ["Renovated Kitchen", "Private Yard"]


def test_listing_property_type_and_status_default_to_empty_string():
    listing = Listing(
        listing_id="abc123", address="1 Test St", city="X", state="CO", zip_code="00000",
        price="$1", beds=1, baths=1.0, sqft=1, lot_sqft=1, parking_spaces=1, year_built=2000,
        description="", amenities=[], photo_urls=[], listing_url="https://example.com/1",
    )
    assert listing.property_type == ""
    assert listing.localized_status == ""


def test_is_active_status_true_for_active():
    assert is_active_status("Active") is True


def test_is_active_status_true_for_blank_or_unknown():
    # Absence of status isn't proof a listing is inactive -- only an
    # explicit non-Active value counts.
    assert is_active_status("") is True


def test_is_active_status_false_for_expired():
    assert is_active_status("Expired") is False


def test_is_active_status_false_for_other_non_active_values():
    assert is_active_status("Sold") is False
    assert is_active_status("Withdrawn") is False
    assert is_active_status("Pending") is False
    assert is_active_status("Closed") is False


def test_is_active_status_true_for_coming_soon():
    assert is_active_status("Coming Soon") is True


def test_is_active_status_true_for_active_prefixed_variants():
    # "Active / Backup" (an accepted backup offer, still technically for
    # sale) confirmed live, 2026-08-27, as a real status Compass returns.
    assert is_active_status("Active / Backup") is True


def test_listing_hoa_annual_defaults_to_none_for_backward_compatible_construction():
    """Store files written before hoa_annual existed must still load, and a
    listing with no HOA information must read as unknown (None), never as
    0.0 -- 0.0 means "confirmed no HOA" and scores differently."""
    listing = Listing(
        listing_id="abc123",
        address="1 Test St",
        city="Testville",
        state="CO",
        zip_code="80020",
        price="$500,000",
        beds=3,
        baths=2.0,
        sqft=1800,
        lot_sqft=7000,
        parking_spaces=2,
        year_built=1990,
        description="A home.",
        amenities=[],
        photo_urls=[],
        listing_url="https://example.com/1",
    )
    assert listing.hoa_annual is None


# --- the favorites Pending exemption (issue #50) ---------------------------

def _l(listing_id, status):
    return Listing(
        listing_id=listing_id, address=f"{listing_id} St", city="Arvada", state="CO",
        zip_code="80003", price="$600,000", beds=3, baths=2.0, sqft=2000,
        lot_sqft=7000, parking_spaces=2, year_built=2000, description="d",
        amenities=[], photo_urls=[], listing_url="https://example.com/x",
        localized_status=status,
    )


@pytest.mark.parametrize(
    "status, expected",
    [("Pending", True), ("Pending / Backup", True), ("Active", False),
     ("Closed", False), ("Expired", False), ("", False)],
)
def test_is_pending_status(status, expected):
    assert is_pending_status(status) == expected


def test_pending_favorite_is_kept():
    """The whole point of #50: a favorite that goes under contract keeps its
    row, photos and paid vision scoring, because Pending deals fall through."""
    kept = select_present_listings(
        [_l("fav", "Pending")], pinned_ids=frozenset(), favorite_ids=frozenset({"fav"})
    )
    assert [l.listing_id for l in kept] == ["fav"]


def test_pending_non_favorite_is_still_excluded():
    """Ben's 2026-08-27 call stands for listings generally -- the exemption is
    scoped to favorites, not a change to is_active_status."""
    assert select_present_listings(
        [_l("match", "Pending")], pinned_ids=frozenset(), favorite_ids=frozenset()
    ) == []


@pytest.mark.parametrize("status", ["Closed", "Expired", "Withdrawn"])
def test_dead_favorite_is_still_excluded(status):
    """Pending only. A Closed or Expired favorite is genuinely gone and would
    otherwise accumulate forever."""
    assert select_present_listings(
        [_l("fav", status)], pinned_ids=frozenset(), favorite_ids=frozenset({"fav"})
    ) == []


def test_active_and_pinned_paths_are_unchanged():
    listings = [_l("active", "Active"), _l("coming", "Coming Soon"),
                _l("pinned", "Expired"), _l("dead", "Closed")]
    kept = select_present_listings(listings, pinned_ids=frozenset({"pinned"}))
    assert [l.listing_id for l in kept] == ["active", "coming", "pinned"]


def test_favorite_ids_defaults_to_empty():
    """Callers that pass no favorites get exactly the old behaviour."""
    assert select_present_listings([_l("fav", "Pending")], pinned_ids=frozenset()) == []
