import sys
from pathlib import Path

import requests
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

from src.auth import ensure_logged_in
from src.config import load_config
from src.csv_writer import write_csv
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        storage_state = str(AUTH_STATE_PATH) if AUTH_STATE_PATH.exists() else None
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        ensure_logged_in(
            context, page, LOGIN_URL,
            config.compass_email, config.compass_password, AUTH_STATE_PATH,
        )

        for url in config.listing_urls:
            precheck_id = derive_listing_id_from_url(url)
            if precheck_id and is_scraped(STORE_DIR, precheck_id):
                print(f"skip (already scraped): {url}")
                continue
            try:
                listing = scrape_listing(page, url)
                if is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                _save_listing(listing, skip_photos)
            except Exception as exc:
                print(f"skip listing (failed to process {url}): {exc}")
                continue

        if config.collection_url:
            try:
                collection_listings = fetch_collection_listings(page, config.collection_url)
                print(f"collection returned {len(collection_listings)} listings")
            except Exception as exc:
                print(f"failed to fetch collection {config.collection_url}: {exc}")
                collection_listings = []

            for listing in collection_listings:
                if is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                try:
                    _save_listing(listing, skip_photos)
                except Exception as exc:
                    print(f"skip listing (failed to process {listing.address}): {exc}")
                    continue

        browser.close()

    all_listings = load_all_listings(STORE_DIR)
    write_csv(all_listings, PHOTOS_DIR, CSV_PATH)
    write_gallery(all_listings, PHOTOS_DIR, GALLERY_PATH)
    print(f"\nWrote {len(all_listings)} listings to {CSV_PATH} and {GALLERY_PATH}")


if __name__ == "__main__":
    main()
