import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.config import load_config
from src.csv_writer import write_csv
from src.db import get_connection, get_pinned_listing_ids, get_price_snapshot, upsert_listing
from src.diff import apply_delisting, compute_changes, should_apply_delisting
from src.gallery import write_gallery
from src.models import Listing
from src.photos import download_photos
from src.scraper import derive_listing_id_from_url, fetch_collection_listings, scrape_listing
from src.store import is_scraped, load_all_listings, save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
CSV_PATH = DATA_DIR / "listings.csv"
GALLERY_PATH = DATA_DIR / "gallery.html"
DB_PATH = DATA_DIR / "listings.db"
LOGIN_URL = "https://www.compass.com/login/"


def fetch_bytes(url: str) -> bytes:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def _save_listing(listing: Listing, skip_photos: bool) -> None:
    """Download photos (unless skipped) and persist a scraped Listing."""
    if not skip_photos:
        download_photos(listing.photo_urls, PHOTOS_DIR / listing.listing_id, fetch_bytes)
    save_listing(STORE_DIR, listing)
    print(f"scraped: {listing.address}")


def main() -> None:
    skip_photos = "--skip-photos" in sys.argv
    config = load_config(dotenv_values(".env"))
    db_conn = get_connection(DB_PATH)
    # Tracks true pin status across this run: seeded from the DB (pins set
    # by past runs), then grown as the explicit-URL loop below actually
    # scrapes listings. Never derived from URL text — derive_listing_id_from_url
    # is a best-effort heuristic (documented to return None for address-only
    # URLs) that's fine for its original cheap resumability-check purpose,
    # but not reliable enough to gate a hard delete on.
    pinned_ids: set[str] = set(get_pinned_listing_ids(db_conn))

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        for url in config.listing_urls:
            precheck_id = derive_listing_id_from_url(url)
            if precheck_id and is_scraped(STORE_DIR, precheck_id):
                print(f"skip (already scraped): {url}")
                continue
            try:
                listing = scrape_listing(page, url)
                pinned_ids.add(listing.listing_id)
                upsert_listing(db_conn, listing, is_pinned=True)
                if is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                _save_listing(listing, skip_photos)
            except Exception as exc:
                print(f"skip listing (failed to process {url}): {exc}")
                continue

        if config.collection_url:
            before = get_price_snapshot(db_conn)
            try:
                collection_listings = fetch_collection_listings(page, config.collection_url)
                print(f"collection returned {len(collection_listings)} listings")
                fetch_succeeded = True
            except Exception as exc:
                print(f"failed to fetch collection {config.collection_url}: {exc}")
                collection_listings = []
                fetch_succeeded = False

            for listing in collection_listings:
                # Preserve pin status if this collection listing happens to
                # also be individually pinned (via LISTING_URLS, this run
                # or a prior one) -- upsert_listing fully replaces the row.
                upsert_listing(db_conn, listing, is_pinned=listing.listing_id in pinned_ids)
                if is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                try:
                    _save_listing(listing, skip_photos)
                except Exception as exc:
                    print(f"skip listing (failed to process {listing.address}): {exc}")
                    continue

            report = compute_changes(
                collection_listings, before, pinned_ids=frozenset(pinned_ids)
            )
            if should_apply_delisting(fetch_succeeded, report, before, frozenset(pinned_ids)):
                apply_delisting(db_conn, PHOTOS_DIR, STORE_DIR, report.delisted_ids)
            elif report.delisted_ids:
                print(
                    f"skipping delisted-listing cleanup ({len(report.delisted_ids)} would "
                    "be affected) — collection fetch failed or looks anomalous"
                )

    db_conn.close()

    all_listings = load_all_listings(STORE_DIR)
    write_csv(all_listings, PHOTOS_DIR, CSV_PATH)
    write_gallery(all_listings, PHOTOS_DIR, GALLERY_PATH)

    print(f"\nWrote {len(all_listings)} listings to {CSV_PATH}, {GALLERY_PATH}, and {DB_PATH}")


if __name__ == "__main__":
    main()
