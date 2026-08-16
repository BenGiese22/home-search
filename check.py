from pathlib import Path

from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.config import load_config
from src.db import delete_listing, get_connection, get_price_snapshot, upsert_listing
from src.diff import compute_changes, format_report
from src.photos import delete_photos
from src.scraper import fetch_collection_listings
from src.store import delete_stored_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
DB_PATH = DATA_DIR / "listings.db"
LOGIN_URL = "https://www.compass.com/login/"


def main() -> None:
    config = load_config(dotenv_values(".env"))
    if not config.collection_url:
        print("No COMPASS_COLLECTION_URL configured; nothing to check.")
        return

    db_conn = get_connection(DB_PATH)
    before = get_price_snapshot(db_conn)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        try:
            fetched = fetch_collection_listings(page, config.collection_url)
        except Exception as exc:
            print(f"failed to fetch collection {config.collection_url}: {exc}")
            fetched = []

    for listing in fetched:
        upsert_listing(db_conn, listing)

    report = compute_changes(fetched, before)
    for listing_id in report.delisted_ids:
        delete_listing(db_conn, listing_id)
        delete_photos(PHOTOS_DIR, listing_id)
        delete_stored_listing(STORE_DIR, listing_id)
        print(f"delisted: {listing_id}")

    db_conn.close()

    print(f"fetched {len(fetched)} listings from the collection\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
