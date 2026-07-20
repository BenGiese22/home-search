import json
import re
from urllib.parse import quote

from playwright.sync_api import Page

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


def fetch_collection_listings(page: Page, collection_url: str) -> list[Listing]:
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
                    "listingsFilter": 0,
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

    return all_listings
