from pathlib import Path

from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.config import load_config
from src.db import get_connection, get_price_snapshot, upsert_listing
from src.diff import compute_changes, format_report
from src.scraper import fetch_collection_listings

DATA_DIR = Path("data")
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
    db_conn.close()

    print(f"fetched {len(fetched)} listings from the collection\n")
    print(format_report(compute_changes(fetched, before)))


if __name__ == "__main__":
    main()
