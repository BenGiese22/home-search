import re

from playwright.sync_api import Page

from src.json_extract import find_listing_dicts_in_html
from src.listing_parser import parse_listing_object
from src.models import Listing

_LISTING_ID_RE = re.compile(r"(\d{10,})")


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


def scrape_collection(page: Page, collection_url: str) -> list[str]:
    """Scroll a Compass collection page until no new listing links load,
    then return every unique listing detail-page URL found."""
    page.goto(collection_url)
    page.wait_for_load_state("networkidle")

    previous_count = 0
    while True:
        links = page.eval_on_selector_all(
            'a[href*="/homedetails/"]',
            "elements => elements.map(e => e.href)",
        )
        unique_links = sorted(set(links))
        if len(unique_links) == previous_count:
            break
        previous_count = len(unique_links)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1000)

    return unique_links
