from pathlib import Path

from dotenv import dotenv_values

from src.auth import launch_authenticated_page
from src.config import load_config
from src.db import get_connection, get_pinned_listing_ids, get_price_snapshot, upsert_listing
from src.diff import compute_changes, format_report, run_delisting
from src.models import is_active_status
from src.scraper import derive_pinned_ids_from_urls, fetch_collection_listings

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
    # Union of the authoritative persisted flag and a best-effort URL
    # match: a listing whose pin predates this feature (or that a schema
    # migration reset) is still protected here even before scrape.py next
    # runs to durably re-pin it.
    pinned_ids = get_pinned_listing_ids(db_conn) | derive_pinned_ids_from_urls(
        config.listing_urls
    )
    before = get_price_snapshot(db_conn)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        try:
            fetched = fetch_collection_listings(page, config.collection_url)
            fetch_succeeded = True
        except Exception as exc:
            print(f"failed to fetch collection {config.collection_url}: {exc}")
            fetched = []
            fetch_succeeded = False

    # A listing whose fresh status comes back non-Active (Expired, Sold,
    # Withdrawn, ...) is treated as absent from the collection for
    # upserting and delisting purposes alike, exactly like one that dropped
    # out of the collection API's results entirely -- see scrape.py's
    # matching logic for the full rationale. Pinned listings are exempt.
    present = [
        listing for listing in fetched
        if listing.listing_id in pinned_ids or is_active_status(listing.localized_status)
    ]

    # Deliberately loop over `present`, not the raw `fetched` list: an
    # inactive, non-pinned listing must never be upserted here, even for the
    # first time -- compute_changes()/run_delisting() below can only remove
    # a listing that was already tracked (present in `before`) and then
    # drops out; a listing that shows up already inactive and was never
    # tracked before would otherwise get written in here and then never be
    # eligible for removal.
    for listing in present:
        # Preserve pin status: check.py never sets a pin itself (it doesn't
        # scrape LISTING_URLS), but a collection listing that's already
        # pinned from a prior scrape.py run must not be un-pinned here.
        upsert_listing(db_conn, listing, is_pinned=listing.listing_id in pinned_ids)

    report = compute_changes(present, before, pinned_ids=pinned_ids)
    run_delisting(db_conn, PHOTOS_DIR, STORE_DIR, fetch_succeeded, report, before, pinned_ids)

    db_conn.close()

    print(f"fetched {len(fetched)} listings from the collection\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
