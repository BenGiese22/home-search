from pathlib import Path

from src.db import get_connection, get_pinned_listing_ids, upsert_listing
from src.store import load_all_listings

DATA_DIR = Path("data")
STORE_DIR = DATA_DIR / "listings"
DB_PATH = DATA_DIR / "listings.db"


def main() -> None:
    listings = load_all_listings(STORE_DIR)
    conn = get_connection(DB_PATH)
    pinned_ids = get_pinned_listing_ids(conn)
    for listing in listings:
        # Preserve any pin already set on this listing_id -- upsert_listing
        # fully replaces the row, and this script has no way of knowing
        # which listings were originally scraped via LISTING_URLS.
        upsert_listing(conn, listing, is_pinned=listing.listing_id in pinned_ids)
    conn.close()
    print(f"Backfilled {len(listings)} listings from {STORE_DIR} into {DB_PATH}")


if __name__ == "__main__":
    main()
