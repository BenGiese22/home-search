from pathlib import Path

from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.config import load_config
from src.db import get_connection, get_pinned_listing_ids, get_price_snapshot, upsert_listing
from src.diff import apply_delisting, compute_changes, format_report, should_apply_delisting
from src.scraper import fetch_collection_listings

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
    pinned_ids = get_pinned_listing_ids(db_conn)
    before = get_price_snapshot(db_conn)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        try:
            fetched = fetch_collection_listings(page, config.collection_url)
            fetch_succeeded = True
        except Exception as exc:
            print(f"failed to fetch collection {config.collection_url}: {exc}")
            fetched = []
            fetch_succeeded = False

    for listing in fetched:
        # Preserve pin status: check.py never sets a pin itself (it doesn't
        # scrape LISTING_URLS), but a collection listing that's already
        # pinned from a prior scrape.py run must not be un-pinned here.
        upsert_listing(db_conn, listing, is_pinned=listing.listing_id in pinned_ids)

    report = compute_changes(fetched, before, pinned_ids=pinned_ids)
    if should_apply_delisting(fetch_succeeded, report, before, pinned_ids):
        apply_delisting(db_conn, PHOTOS_DIR, STORE_DIR, report.delisted_ids)
    elif report.delisted_ids:
        print(
            f"skipping delisted-listing cleanup ({len(report.delisted_ids)} would be "
            "affected) — collection fetch failed or looks anomalous"
        )

    db_conn.close()

    print(f"fetched {len(fetched)} listings from the collection\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
