import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from playwright.sync_api import Page

from src.backfill import dedupe_by_listing_id
from src.config import COLLECTION_TABS
from src.json_extract import find_listing_dicts_in_html
from src.listing_parser import parse_listing_object
from src.models import Listing

_LISTING_ID_RE = re.compile(r"(\d{10,})")
_COLLECTION_ID_RE = re.compile(r"/collection/([a-f0-9]+)")


def derive_listing_id_from_url(url: str) -> str | None:
    """Best-effort extraction of a Compass listing ID from its URL, used
    only as a cheap pre-fetch resumability check. The authoritative ID
    always comes from the scraped page's own listingIdSHA (see
    parse_listing_object); if this heuristic ever mismatches, the worst
    case is one extra page load, not a data-correctness bug."""
    match = _LISTING_ID_RE.search(url)
    return match.group(1) if match else None


def derive_pinned_ids_from_urls(listing_urls: list[str]) -> frozenset[str]:
    """Best-effort listing_ids extracted from LISTING_URLS entries. Used
    only to opportunistically reinforce pin protection for listings that a
    schema migration, or a not-yet-rerun scrape.py, may have left unpinned
    in the DB — never the sole source of truth for whether something is
    protected. See get_pinned_listing_ids() in src/db.py for the
    authoritative, persisted flag this backstops."""
    return frozenset(
        listing_id
        for url in listing_urls
        if (listing_id := derive_listing_id_from_url(url)) is not None
    )


def scrape_listing(page: Page, url: str) -> Listing:
    page.goto(url)
    page.wait_for_load_state("networkidle")
    html = page.content()
    candidates = find_listing_dicts_in_html(html)
    if not candidates:
        raise ValueError(f"No listing data found on page: {url}")
    return parse_listing_object(candidates[0], listing_url=url)


def extract_collection_id(collection_url: str) -> str:
    """Extract the collection ID from a Compass collection URL, e.g.
    https://www.compass.com/app/collection/<id>/matches?... -> <id>."""
    match = _COLLECTION_ID_RE.search(collection_url)
    if not match:
        raise ValueError(f"Could not find a collection ID in URL: {collection_url}")
    return match.group(1)


def parse_collection_response(response_json: dict) -> list[Listing]:
    """Parse one page of Compass's /api/v3/collections/listings/paginated
    response into Listings. Each item's listingData is shaped identically
    to an individual listing page's embedded JSON (verified live), so
    parse_listing_object handles it directly."""
    listings = []
    for item in response_json.get("currentPageListings", []):
        listing_data = item.get("listingData", {})
        page_link = listing_data.get("pageLink", "")
        listing_url = (
            f"https://www.compass.com{page_link}" if page_link.startswith("/") else page_link
        )
        listings.append(parse_listing_object(listing_data, listing_url=listing_url))
    return listings


def fetch_collection_listings(
    page: Page, collection_url: str, listings_filter: int = 0
) -> list[Listing]:
    """Fetch every listing in a Compass collection via its internal
    paginated API and return them as fully-parsed Listings.

    Compass's collection page (`/app/collection/<id>/matches`) is a
    client-rendered SPA — scrolling and scanning for `<a>` links (the
    original design) finds nothing there. This function instead calls the
    same API the page itself calls to load its data (confirmed live by
    intercepting the page's network traffic), which returns complete
    listing data for the whole collection in a handful of paginated
    requests instead of one live page visit per listing.

    Must be called after ensure_logged_in — it reuses the page's
    authenticated request context (cookies), which Playwright's
    `page.request` shares with the browser session automatically.
    """
    if listings_filter not in COLLECTION_TABS.values():
        # Guards the one value that must never be fetched (3 = notInterested)
        # and any typo, before a request goes out.
        raise ValueError(
            f"refusing to fetch listingsFilter {listings_filter}; "
            f"expected one of {sorted(COLLECTION_TABS.values())}"
        )

    collection_id = extract_collection_id(collection_url)

    all_listings: list[Listing] = []
    skip = 0
    limit = 120
    total = None
    while total is None or skip < total:
        query = json.dumps(
            {
                "collectionId": collection_id,
                "pagination": {"skip": skip, "limit": limit},
                "query": {
                    "listingsFilter": listings_filter,
                    "sort": {"collectionListingsSortOrder": 1},
                    "searchCriteria": {},
                    "enrichments": [0, 1, 2],
                },
            }
        )
        api_url = (
            "https://www.compass.com/api/v3/collections/listings/paginated"
            f"?json={quote(query)}"
        )
        response = page.request.get(api_url)
        data = response.json()
        total = data["totalListings"]
        all_listings.extend(parse_collection_response(data))
        skip += limit

    # A short read means pagination stopped early (a dropped page, a shape
    # change). Returning it silently would look exactly like listings having
    # left the collection, which is the input the delisting cascade acts on.
    if len(all_listings) != total:
        raise ValueError(
            f"collection returned {len(all_listings)} listings but reported "
            f"totalListings={total}"
        )

    return all_listings


@dataclass(frozen=True)
class CollectionFetch:
    """The result of fetching one or more tabs of a single collection.

    Carries per-tab outcomes rather than a single bool because the delisting
    cascade needs to know whether *every* tab was accounted for: a tab that
    failed while another succeeded looks identical, downstream, to that tab's
    listings having been removed by a human.
    """

    listings: list[Listing] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)   # tab -> raw count, successes only
    errors: dict[str, str] = field(default_factory=dict)   # tab -> error text
    tab_ids: dict[str, frozenset[str]] = field(default_factory=dict)  # tab -> listing ids

    @property
    def failed_tabs(self) -> frozenset[str]:
        return frozenset(self.errors)

    @property
    def favorite_ids(self) -> frozenset[str]:
        """Which listings are favorites, for the Pending exemption. Empty when
        the favorites tab was not fetched or failed -- so a failed tab can
        never make a favorite look like a plain match and get it deleted."""
        return self.tab_ids.get("favorites", frozenset())


def fetch_collection_tabs(
    page: Page, collection_url: str, tabs: Sequence[str]
) -> CollectionFetch:
    """Fetch each named tab of a collection and merge them into one list.

    Tabs are fetched in the order given -- config orders them by precedence --
    and deduped first-wins, so a listing sitting in both favorites and matches
    keeps its favorites copy.

    One tab failing does not abort the others: partial data is still worth
    upserting. It does mark the fetch untrustworthy for delisting purposes,
    which is what collection_fetch_is_trustworthy reads.
    """
    for tab in tabs:
        if tab not in COLLECTION_TABS:
            raise ValueError(
                f"unknown collection tab {tab!r}; expected one of {sorted(COLLECTION_TABS)}"
            )

    merged: list[Listing] = []
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    tab_ids: dict[str, frozenset[str]] = {}
    for tab in tabs:
        try:
            listings = fetch_collection_listings(page, collection_url, COLLECTION_TABS[tab])
        except Exception as exc:
            errors[tab] = str(exc)
            continue
        counts[tab] = len(listings)
        tab_ids[tab] = frozenset(listing.listing_id for listing in listings)
        merged.extend(listings)

    return CollectionFetch(
        listings=dedupe_by_listing_id(merged),
        counts=counts,
        errors=errors,
        tab_ids=tab_ids,
    )
