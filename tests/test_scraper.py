import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from src.models import Listing
from src.scraper import (
    derive_listing_id_from_url,
    derive_pinned_ids_from_urls,
    extract_collection_id,
    fetch_collection_listings,
    fetch_collection_tabs,
    parse_collection_response,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "collection_response_sample.json"


def test_derive_listing_id_from_url_homedetails_lid():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/2145067054346865465_lid/"
    assert derive_listing_id_from_url(url) == "2145067054346865465"


def test_derive_listing_id_from_url_listing_view():
    url = "https://www.compass.com/listing/2130651237632606465/view?agent_id=688995414728a40001928728"
    assert derive_listing_id_from_url(url) == "2130651237632606465"


def test_derive_listing_id_from_url_no_id_returns_none():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/"
    assert derive_listing_id_from_url(url) is None


def test_derive_pinned_ids_from_urls_extracts_ids():
    urls = [
        "https://www.compass.com/listing/2130651237632606465/view",
        "https://www.compass.com/listing/2120506603298373729/view",
    ]

    assert derive_pinned_ids_from_urls(urls) == frozenset(
        {"2130651237632606465", "2120506603298373729"}
    )


def test_derive_pinned_ids_from_urls_skips_urls_with_no_id():
    urls = [
        "https://www.compass.com/listing/2130651237632606465/view",
        "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/",
    ]

    assert derive_pinned_ids_from_urls(urls) == frozenset({"2130651237632606465"})


def test_derive_pinned_ids_from_urls_empty_list_returns_empty_frozenset():
    assert derive_pinned_ids_from_urls([]) == frozenset()


def test_extract_collection_id_from_app_collection_url():
    url = (
        "https://www.compass.com/app/collection/6a27426b698343000129b139/matches"
        "?source=deals&page=1&pageSize=120&savedSearchId=984690897574691433&sort=time_added"
    )
    assert extract_collection_id(url) == "6a27426b698343000129b139"


def test_extract_collection_id_no_match_raises():
    with pytest.raises(ValueError, match="collection ID"):
        extract_collection_id("https://www.compass.com/overview/")


def test_parse_collection_response_from_real_fixture():
    data = json.loads(FIXTURE_PATH.read_text())
    listings = parse_collection_response(data)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.listing_id == "2153779980783572393"
    assert listing.address == "6330 West 112th Place"
    assert listing.city == "Broomfield"
    assert listing.price == "$525,000"
    assert listing.beds == 3
    assert listing.baths == 1.75  # 1 full + 0 half + 1 three-quarter*0.75
    assert listing.sqft == 1779
    assert listing.parking_spaces == 2
    assert listing.year_built == 1979
    assert "Attached Garage" in listing.amenities
    assert listing.photo_urls == [
        "https://www.compass.com/m/7fffe4982e173ceee72fa477c4dc66e1d7c31f46c38be3efff335375f761cbbf/origin.jpg"
    ]
    assert listing.listing_url == (
        "https://www.compass.com/homedetails/6330-W-112th-Pl-Broomfield-CO-80020/2153779980783572393_lid/"
    )


def test_parse_collection_response_empty_page_returns_empty_list():
    assert parse_collection_response({"totalListings": 0, "currentPageListings": []}) == []


# --- multi-tab collection fetching -----------------------------------------
#
# Compass serves matches and favorites from the same collection ID, selected
# by the listingsFilter integer in the API query rather than the URL path.
# These pin down that the right filter goes out, that the never-scraped
# bucket cannot go out at all, and that a listing in both tabs survives once.

COLLECTION_URL = "https://www.compass.com/app/collection/6a27426b698343000129b139/matches"


def _listing(listing_id):
    return Listing(
        listing_id=listing_id,
        address=f"{listing_id} Test St",
        city="Testville",
        state="CO",
        zip_code="80020",
        price="$650,000",
        beds=3,
        baths=2.0,
        sqft=1800,
        lot_sqft=5000,
        parking_spaces=2,
        year_built=2000,
        description="desc",
        amenities=[],
        photo_urls=[],
        listing_url=f"https://example.com/listing/{listing_id}",
    )


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakePage:
    """Stands in for a Playwright Page, recording the listingsFilter of every
    API call and replying from a per-filter script."""

    def __init__(self, by_filter, raise_for=()):
        self.by_filter = by_filter          # filter int -> list[listing_id]
        self.raise_for = set(raise_for)     # filter ints that blow up
        self.filters_requested = []
        self.request = self

    def get(self, url):
        query = json.loads(unquote(parse_qs(urlparse(url).query)["json"][0]))
        listings_filter = query["query"]["listingsFilter"]
        self.filters_requested.append(listings_filter)
        if listings_filter in self.raise_for:
            raise RuntimeError(f"boom on filter {listings_filter}")
        ids = self.by_filter.get(listings_filter, [])
        skip = query["pagination"]["skip"]
        limit = query["pagination"]["limit"]
        page_ids = ids[skip:skip + limit]
        return FakeResponse({
            "totalListings": len(ids),
            "currentPageListings": [
                {"listingData": {"listingIdSHA": lid, "pageLink": f"/listing/{lid}/view"}}
                for lid in page_ids
            ],
        })


@pytest.fixture
def fake_parse(monkeypatch):
    """parse_listing_object is exercised thoroughly elsewhere; here the only
    thing that matters is which ids came back from which filter."""
    monkeypatch.setattr(
        "src.scraper.parse_listing_object",
        lambda data, listing_url="": _listing(data["listingIdSHA"]),
    )


def test_fetch_collection_listings_defaults_to_matches_filter(fake_parse):
    page = FakePage({0: ["a", "b"]})
    listings = fetch_collection_listings(page, COLLECTION_URL)
    assert page.filters_requested == [0]
    assert [l.listing_id for l in listings] == ["a", "b"]


def test_fetch_collection_listings_sends_the_requested_filter(fake_parse):
    page = FakePage({1: ["fav1"]})
    fetch_collection_listings(page, COLLECTION_URL, 1)
    assert page.filters_requested == [1]


@pytest.mark.parametrize("bad_filter", [2, 3, 99, -1])
def test_fetch_collection_listings_refuses_unknown_filters(bad_filter):
    """Filter 3 is notInterested -- 277 listings we must never ingest. The
    guard fires before any request goes out."""
    page = FakePage({})
    with pytest.raises(ValueError, match="refusing to fetch"):
        fetch_collection_listings(page, COLLECTION_URL, bad_filter)
    assert page.filters_requested == []


def test_fetch_collection_listings_paginates(fake_parse):
    page = FakePage({0: [str(i) for i in range(250)]})
    listings = fetch_collection_listings(page, COLLECTION_URL)
    assert len(listings) == 250
    assert page.filters_requested == [0, 0, 0]


def test_short_read_raises_rather_than_looking_like_a_delisting(fake_parse):
    page = FakePage({0: ["a", "b"]})
    original = page.get

    def truncated(url):
        response = original(url)
        payload = response.json()
        payload["currentPageListings"] = payload["currentPageListings"][:1]
        return FakeResponse(payload)

    page.get = truncated
    with pytest.raises(ValueError, match="totalListings"):
        fetch_collection_listings(page, COLLECTION_URL)


def test_fetch_collection_tabs_merges_and_dedupes_favorites_first(fake_parse):
    """A listing in both tabs is kept once. Favorites is requested first, so
    first-wins dedup keeps the favorites copy -- moving a listing into
    favorites is a deliberate act and outranks the saved-search match."""
    page = FakePage({0: ["shared", "match_only"], 1: ["fav_only", "shared"]})
    fetch = fetch_collection_tabs(page, COLLECTION_URL, ("favorites", "matches"))
    assert [l.listing_id for l in fetch.listings] == ["fav_only", "shared", "match_only"]
    assert fetch.counts == {"favorites": 2, "matches": 2}
    assert fetch.errors == {}
    assert fetch.failed_tabs == frozenset()


def test_fetch_collection_tabs_records_a_failed_tab_without_aborting(fake_parse):
    page = FakePage({0: ["m1", "m2"], 1: ["f1"]}, raise_for={1})
    fetch = fetch_collection_tabs(page, COLLECTION_URL, ("favorites", "matches"))
    assert [l.listing_id for l in fetch.listings] == ["m1", "m2"]
    assert fetch.counts == {"matches": 2}
    assert fetch.failed_tabs == frozenset({"favorites"})


def test_fetch_collection_tabs_rejects_unknown_tab_before_requesting(fake_parse):
    page = FakePage({0: ["a"]})
    with pytest.raises(ValueError, match="notInterested"):
        fetch_collection_tabs(page, COLLECTION_URL, ("matches", "notInterested"))
    assert page.filters_requested == []


def test_fetch_records_which_listings_came_from_which_tab(fake_parse):
    page = FakePage({0: ["m1"], 1: ["f1", "f2"]})
    fetch = fetch_collection_tabs(page, COLLECTION_URL, ("favorites", "matches"))
    assert fetch.tab_ids == {"favorites": frozenset({"f1", "f2"}),
                             "matches": frozenset({"m1"})}
    assert fetch.favorite_ids == frozenset({"f1", "f2"})


def test_favorite_ids_is_empty_when_the_favorites_tab_failed(fake_parse):
    """A failed tab must not quietly strip the Pending exemption and turn a
    favorite back into a deletable match. It cannot here: the run is already
    untrustworthy for delisting, and favorite_ids is empty rather than wrong."""
    page = FakePage({0: ["m1"], 1: ["f1"]}, raise_for={1})
    fetch = fetch_collection_tabs(page, COLLECTION_URL, ("favorites", "matches"))
    assert fetch.favorite_ids == frozenset()
    assert fetch.failed_tabs == frozenset({"favorites"})


def test_favorite_ids_is_empty_when_favorites_was_not_requested(fake_parse):
    page = FakePage({0: ["m1"]})
    fetch = fetch_collection_tabs(page, COLLECTION_URL, ("matches",))
    assert fetch.favorite_ids == frozenset()
