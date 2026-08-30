"""One-off backfill: populate hoa_annual for listings scraped before the
field existed.

No re-scrape and no network calls -- every stored listing already has its
description on disk in data/listings/, and that prose is the only source
of HOA information Compass gives us. Re-parsing it is enough.

Idempotent: re-running re-derives the same values from the same text.
Pass --dry-run to see the breakdown without writing anything.
"""

import sys
from collections import Counter
from pathlib import Path

from src.db import get_connection, get_pinned_listing_ids, upsert_listing
from src.hoa import parse_hoa_from_description
from src.store import load_all_listings, save_listing

DATA_DIR = Path("data")
STORE_DIR = DATA_DIR / "listings"
DB_PATH = DATA_DIR / "listings.db"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    listings = load_all_listings(STORE_DIR)

    counts: Counter[str] = Counter()
    changed = []
    for listing in listings:
        hoa_annual = parse_hoa_from_description(listing.description)
        if hoa_annual is None:
            counts["unknown"] += 1
        elif hoa_annual == 0:
            counts["confirmed no HOA"] += 1
        else:
            counts["known amount"] += 1
        if hoa_annual != listing.hoa_annual:
            listing.hoa_annual = hoa_annual
            changed.append(listing)

    for label, count in sorted(counts.items()):
        print(f"  {label}: {count}")
    print(f"{len(changed)} of {len(listings)} listings changed")

    if dry_run:
        print("Dry run -- nothing written.")
        return

    conn = get_connection(DB_PATH)
    pinned_ids = get_pinned_listing_ids(conn)
    for listing in changed:
        save_listing(STORE_DIR, listing)
    for listing in listings:
        # Preserve any pin already set -- upsert_listing fully replaces the
        # row, same caveat as backfill_db.py.
        upsert_listing(conn, listing, is_pinned=listing.listing_id in pinned_ids)
    conn.close()
    print(f"Wrote {len(changed)} store files and re-upserted {len(listings)} rows into {DB_PATH}")


if __name__ == "__main__":
    main()
