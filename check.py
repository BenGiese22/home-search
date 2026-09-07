from pathlib import Path

from src.auth import launch_authenticated_page
from src.config import load_config, load_env
from src.turso_db import stage_connection
from src.db import (
    get_price_snapshot,
    get_property_ids,
    rejected_listing_ids,
    rejected_property_ids,
    upsert_listing,
)
from src.diff import collection_fetch_is_trustworthy, compute_changes, format_report, run_delisting
from src.models import select_present_listings
from src.scraper import fetch_collection_tabs

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
LOGIN_URL = "https://www.compass.com/login/"


def main() -> None:
    config = load_config(load_env())
    if not config.collection_url:
        print("No COMPASS_COLLECTION_URL configured; nothing to check.")
        return

    db_conn = stage_connection()
    # Union of the authoritative persisted flag and a best-effort URL
    # match: a listing whose pin predates this feature (or that a schema
    # migration reset) is still protected here even before scrape.py next
    # runs to durably re-pin it.
    before = get_price_snapshot(db_conn)

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        fetch = fetch_collection_tabs(
            page, config.collection_url, config.collection_tabs
        )
        for tab, count in fetch.counts.items():
            print(f"collection/{tab} returned {count} listings")
        for tab, exc in fetch.errors.items():
            print(f"failed to fetch collection/{tab}: {exc}")
        fetched = fetch.listings
        # Without this check.py would treat every favorites-only listing as
        # absent and delist it -- it runs the same cascade scrape.py does.
        fetch_succeeded = collection_fetch_is_trustworthy(fetch, before)

    # A listing whose fresh status comes back non-Active (Expired, Sold,
    # Withdrawn, ...) is treated as absent from the collection for
    # upserting and delisting purposes alike, exactly like one that dropped
    # out of the collection API's results entirely -- see scrape.py's
    # matching logic for the full rationale. Pinned listings are exempt.
    rejected_pids = rejected_property_ids(db_conn)
    present = select_present_listings(
        fetched, fetch.favorite_ids,
        rejected_property_ids=rejected_pids,
        property_id_by_listing=get_property_ids(db_conn),
    )

    # Deliberately loop over `present`, not the raw `fetched` list: an
    # inactive listing must never be upserted here, even for the
    # first time -- compute_changes()/run_delisting() below can only remove
    # a listing that was already tracked (present in `before`) and then
    # drops out; a listing that shows up already inactive and was never
    # tracked before would otherwise get written in here and then never be
    # eligible for removal.
    for listing in present:
        # Preserve pin status: check.py never sets a pin itself (it doesn't
        # scrape LISTING_URLS), but a collection listing that's already
        upsert_listing(db_conn, listing)

    report = compute_changes(
        present, before,
        rejected_ids=rejected_listing_ids(db_conn, rejected_pids),
    )
    run_delisting(
        db_conn, PHOTOS_DIR, fetch_succeeded, report, before,
        # Without this, a delisting that happens to run through check.py
        # deletes the hosted_photos rows and strands their blobs -- the same
        # asymmetry that let 1,813 orphans accumulate when bulk_delete_listings
        # pruned hosted_photos and delete_listing did not.
        blob_token=load_env().get("BLOB_READ_WRITE_TOKEN"),
    )

    db_conn.close()

    print(f"fetched {len(fetched)} listings from the collection\n")
    print(format_report(report))


if __name__ == "__main__":
    main()
