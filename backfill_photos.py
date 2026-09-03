import random
import time
from pathlib import Path

from playwright.sync_api import Page

from src.auth import launch_authenticated_page
from src.config import load_config, load_env
from src.turso_db import stage_connection
from src.db import get_pinned_listing_ids, upsert_listing
from src.photos import download_photos
from src.scraper import fetch_collection_tabs
from src.store import is_scraped, save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
LOGIN_URL = "https://www.compass.com/login/"

# See scrape.py's matching constants/helper -- same rationale, kept in sync.
PHOTO_JITTER_MIN_SECONDS = 0.15
PHOTO_JITTER_MAX_SECONDS = 0.5


def _photo_jitter() -> None:
    time.sleep(random.uniform(PHOTO_JITTER_MIN_SECONDS, PHOTO_JITTER_MAX_SECONDS))


def _build_fetch_bytes(page: Page):
    """Fetches photo bytes through the authenticated page's request context
    rather than a standalone requests.get() call -- see scrape.py's
    matching helper for why."""

    def fetch_bytes(url: str) -> bytes:
        response = page.request.get(url)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} fetching {url}")
        return response.body()

    return fetch_bytes


def main() -> None:
    config = load_config(load_env())
    if not config.collection_url:
        print("No COMPASS_COLLECTION_URL configured; nothing to backfill.")
        return

    db_conn = stage_connection()
    pinned_ids = get_pinned_listing_ids(db_conn)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        fetch = fetch_collection_tabs(
            page, config.collection_url, config.collection_tabs
        )
        for tab, exc in fetch.errors.items():
            print(f"failed to fetch collection/{tab}: {exc}")
        fetched = fetch.listings
        print(f"fetched {len(fetched)} listings from the collection\n")

        fetch_bytes = _build_fetch_bytes(page)
        for listing in fetched:
            # Preserve pin status: this script only touches collection
            # listings, but one of them may also be individually pinned.
            upsert_listing(db_conn, listing, is_pinned=listing.listing_id in pinned_ids)
            try:
                download_photos(
                    listing.photo_urls,
                    PHOTOS_DIR / listing.listing_id,
                    fetch_bytes,
                    sleep_fn=_photo_jitter,
                )
            except Exception as exc:
                print(f"skip listing (failed to download photos for {listing.address}): {exc}")
                continue
            if not is_scraped(STORE_DIR, listing.listing_id):
                save_listing(STORE_DIR, listing)
            print(f"backfilled photos: {listing.address}")

    db_conn.close()
    print(f"\nBackfilled photos for {len(fetched)} listings")


if __name__ == "__main__":
    main()
