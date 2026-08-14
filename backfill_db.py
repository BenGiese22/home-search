from pathlib import Path

from src.db import get_connection, upsert_listing
from src.store import load_all_listings

DATA_DIR = Path("data")
STORE_DIR = DATA_DIR / "listings"
DB_PATH = DATA_DIR / "listings.db"


def main() -> None:
    listings = load_all_listings(STORE_DIR)
    conn = get_connection(DB_PATH)
    for listing in listings:
        upsert_listing(conn, listing)
    conn.close()
    print(f"Backfilled {len(listings)} listings from {STORE_DIR} into {DB_PATH}")


if __name__ == "__main__":
    main()
