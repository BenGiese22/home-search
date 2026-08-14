from pathlib import Path

import requests
from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.backfill import dedupe_by_listing_id
from src.config import load_config
from src.db import get_connection, upsert_listing
from src.photos import download_photos
from src.scraper import fetch_collection_listings
from src.store import is_scraped, save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
DB_PATH = DATA_DIR / "listings.db"
LOGIN_URL = "https://www.compass.com/login/"


def fetch_bytes(url: str) -> bytes:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def main() -> None:
    config = load_config(dotenv_values(".env"))
    if not config.collection_url:
        print("No COMPASS_COLLECTION_URL configured; nothing to backfill.")
        return

    db_conn = get_connection(DB_PATH)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        try:
            fetched = fetch_collection_listings(page, config.collection_url)
        except Exception as exc:
            print(f"failed to fetch collection {config.collection_url}: {exc}")
            fetched = []

    fetched = dedupe_by_listing_id(fetched)
    print(f"fetched {len(fetched)} listings from the collection\n")

    for listing in fetched:
        upsert_listing(db_conn, listing)
        try:
            download_photos(listing.photo_urls, PHOTOS_DIR / listing.listing_id, fetch_bytes)
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
