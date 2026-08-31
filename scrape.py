import random
import sys
import time
from pathlib import Path

from playwright.sync_api import Page

from src.auth import launch_authenticated_page
from src.config import load_config, load_env
from src.csv_writer import write_csv
from src.db import (
    bulk_upsert_listings,
    get_connection,
    get_pinned_listing_ids,
    get_price_snapshot,
    query_listings,
    upsert_listing,
)
from src.diff import compute_changes, run_delisting
from src.gallery import write_gallery
from src.models import Listing, is_active_status
from src.photos import download_photos
from src.scraper import (
    derive_listing_id_from_url,
    derive_pinned_ids_from_urls,
    fetch_collection_listings,
    scrape_listing,
)
from src.store import is_scraped, load_all_listings, needs_field_backfill, save_listing

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
STORE_DIR = DATA_DIR / "listings"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
CSV_PATH = DATA_DIR / "listings.csv"
GALLERY_PATH = DATA_DIR / "gallery.html"
DB_PATH = DATA_DIR / "listings.db"
LOGIN_URL = "https://www.compass.com/login/"

# Randomized pause between photo downloads so a listing's ~20-35 photos
# don't fire back-to-back with no pacing -- see docs/journal/decisions.md,
# 2026-08-16, for why this and routing photo fetches through the
# authenticated page's request context (below) both matter.
PHOTO_JITTER_MIN_SECONDS = 0.15
PHOTO_JITTER_MAX_SECONDS = 0.5


def _photo_jitter() -> None:
    time.sleep(random.uniform(PHOTO_JITTER_MIN_SECONDS, PHOTO_JITTER_MAX_SECONDS))


def _build_fetch_bytes(page: Page):
    """Fetches photo bytes through the already-authenticated Playwright
    page's request context instead of a standalone requests.get() call --
    this reuses the real browser session's cookies and HTTP/TLS
    fingerprint, so a photo download looks like a normal in-page image
    load rather than a separate unauthenticated script hitting the CDN."""

    def fetch_bytes(url: str) -> bytes:
        response = page.request.get(url)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} fetching {url}")
        return response.body()

    return fetch_bytes


# Fields introduced after listings were first scraped; --backfill-missing
# uses these to decide which stored listings are stale. Add to this tuple
# whenever a new extracted field lands, so one flag keeps working.
BACKFILL_FIELDS = (
    "hoa_annual",
    "tax_annual",
    "sqft_above_grade",
    "outdoor_spaces",
)


def _save_listing(listing: Listing, skip_photos: bool, page: Page) -> None:
    """Download photos (unless skipped) and persist a scraped Listing.
    Safe to call again for an already-scraped listing (see --force): the
    JSON store overwrite is idempotent, and download_photos() only fetches
    photos that aren't already on disk, so a retry fills in gaps from a
    prior partial failure rather than re-downloading everything."""
    if not skip_photos:
        download_photos(
            listing.photo_urls,
            PHOTOS_DIR / listing.listing_id,
            _build_fetch_bytes(page),
            sleep_fn=_photo_jitter,
        )
    save_listing(STORE_DIR, listing)
    print(f"scraped: {listing.address}")


def _backfill_orphans(db_conn, page: Page, skip_photos: bool) -> None:
    """Refresh stale listings that neither ingestion path can reach.

    A listing pinned in the DB but no longer in LISTING_URLS and no longer in
    the active collection is invisible to both branches above -- it would keep
    whatever fields it had when it was last scraped, forever. Its listing_url
    is still on its row, so re-scrape it from that.
    """
    stale = [
        row for row in query_listings(db_conn)
        if needs_field_backfill(STORE_DIR, row["listing_id"], BACKFILL_FIELDS)
    ]
    if not stale:
        return
    print(f"--backfill-missing: {len(stale)} unreachable listing(s) to refresh by stored URL")
    pinned_ids = get_pinned_listing_ids(db_conn)
    for row in stale:
        try:
            listing = scrape_listing(page, row["listing_url"])
            upsert_listing(db_conn, listing, is_pinned=row["listing_id"] in pinned_ids)
            _save_listing(listing, skip_photos, page)
        except Exception as exc:
            print(f"skip listing (failed to refresh {row['address']}): {exc}")


def _parse_limit(argv: list[str]) -> int | None:
    for arg in argv:
        if arg.startswith("--limit="):
            return int(arg.split("=", 1)[1])
    return None


def main() -> None:
    skip_photos = "--skip-photos" in sys.argv
    limit = _parse_limit(sys.argv)
    new_listing_only = "--new-listing" in sys.argv
    force = "--force" in sys.argv
    # Fields added after the initial scrape. --backfill-missing re-processes
    # only the listings whose stored JSON predates them, which is the cheap
    # alternative to --force: same one collection API pass, but it skips the
    # photo download and store rewrite for listings that already have the data.
    backfill_missing = "--backfill-missing" in sys.argv
    config = load_config(load_env())
    db_conn = get_connection(DB_PATH)
    # Snapshot BEFORE any upserts this run touch the DB -- including the
    # explicit-URL loop below -- so a genuine price change on a listing
    # that's both pinned and collection-fetched is still detected. Taking
    # this after that loop would read back the price it just wrote.
    before = get_price_snapshot(db_conn)
    # Tracks pin status across this run: starts as the union of the
    # authoritative persisted flag and a best-effort URL match (protects a
    # pin a schema migration reset, or one that predates a re-run of this
    # script), then grows further as the explicit-URL loop below actually
    # confirms real listing_ids via scraping -- the authoritative source
    # for anything not already in the DB or config.
    pinned_ids: set[str] = set(
        get_pinned_listing_ids(db_conn) | derive_pinned_ids_from_urls(config.listing_urls)
    )

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        for url in config.listing_urls:
            precheck_id = derive_listing_id_from_url(url)
            # --backfill-missing must reach pinned listings too. They take the
            # detail-page branch rather than the collection one, so filtering
            # only the collection batch left every pinned listing stale.
            stale = (
                backfill_missing
                and precheck_id is not None
                and needs_field_backfill(STORE_DIR, precheck_id, BACKFILL_FIELDS)
            )
            if not force and not stale and precheck_id and is_scraped(STORE_DIR, precheck_id):
                print(f"skip (already scraped): {url}")
                continue
            try:
                listing = scrape_listing(page, url)
                pinned_ids.add(listing.listing_id)
                upsert_listing(db_conn, listing, is_pinned=True)
                stale = stale or (
                    backfill_missing
                    and needs_field_backfill(STORE_DIR, listing.listing_id, BACKFILL_FIELDS)
                )
                if not force and not stale and is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                _save_listing(listing, skip_photos, page)
            except Exception as exc:
                print(f"skip listing (failed to process {url}): {exc}")
                continue

        if config.collection_url:
            try:
                collection_listings = fetch_collection_listings(page, config.collection_url)
                print(f"collection returned {len(collection_listings)} listings")
                fetch_succeeded = True
            except Exception as exc:
                print(f"failed to fetch collection {config.collection_url}: {exc}")
                collection_listings = []
                fetch_succeeded = False

            # A listing whose fresh status comes back non-Active (Expired,
            # Sold, Withdrawn, ...) is treated as absent from the
            # collection for every purpose below -- upserting, photo/JSON
            # saving, and delisting -- exactly like one that dropped out of
            # the collection API's results entirely, going through the same
            # reviewed, circuit-breaker-protected removal path rather than
            # new logic. Pinned listings are exempt, same as delisting
            # already exempts them: an explicit pin means Ben wants it
            # tracked regardless of what the MLS status says.
            present_listings = [
                listing for listing in collection_listings
                if listing.listing_id in pinned_ids or is_active_status(listing.localized_status)
            ]
            inactive_count = len(collection_listings) - len(present_listings)
            if inactive_count:
                print(f"{inactive_count} listing(s) no longer active (expired/sold/withdrawn), excluding")

            # Refresh every present (active/pinned) listing's DB row
            # regardless of --limit/--new-listing -- cheap (no network), and
            # it's what keeps price-change detection correct even during a
            # deliberately limited/staged run that skips most photos.
            # Deliberately NOT looping over the raw collection_listings: an
            # inactive, non-pinned listing must never be upserted here, even
            # for the first time -- compute_changes()/run_delisting() below
            # can only remove a listing that was already tracked (present in
            # `before`) and then drops out; a listing that shows up already
            # inactive and was never tracked before would otherwise get
            # written in here and then never be eligible for removal.
            # One batched write for the whole collection rather than one
            # per listing. Against local SQLite the difference is invisible;
            # against Turso, upsert_listing costs ~65 round-trips per listing
            # (one per amenity and one per photo URL, because
            # turso_serverless's executemany loops), which is ~8,385
            # round-trips -- about 33 minutes -- across this corpus.
            # pinned_ids carries the CURRENT pin status of every listing, for
            # the same reason upsert_listing takes is_pinned: this is a full
            # row replace, so a listing missing from it is actively un-pinned.
            bulk_upsert_listings(db_conn, present_listings, pinned_ids=pinned_ids)

            # --limit caps how many listings this run downloads photos for
            # and saves to the JSON store -- e.g. for a first smoke test of
            # the photo-download path against the live site. --new-listing
            # filters to not-yet-scraped listings *before* that cap is
            # applied: without it, --limit alone re-slices the same
            # fixed-order prefix of present_listings every run, and once
            # that prefix is already scraped, a repeated --limit run does
            # zero new work -- --new-listing is what makes a chunked,
            # staged backfill (several --limit runs over time) actually
            # make progress each time. compute_changes below runs against
            # present_listings too (not the raw collection_listings), so an
            # excluded inactive listing is treated as a delisting candidate,
            # and a small --limit batch is never mistaken for mass delisting.
            candidates = present_listings
            if new_listing_only:
                candidates = [
                    listing for listing in candidates
                    if not is_scraped(STORE_DIR, listing.listing_id)
                ]
            to_process = candidates[:limit] if limit is not None else candidates
            if limit is not None:
                flags = f"--limit={limit}" + (" --new-listing" if new_listing_only else "")
                print(
                    f"{flags}: processing {len(to_process)} of "
                    f"{len(present_listings)} active/pinned listings this run"
                )

            if backfill_missing:
                stale = [
                    listing for listing in to_process
                    if needs_field_backfill(STORE_DIR, listing.listing_id, BACKFILL_FIELDS)
                ]
                print(
                    f"--backfill-missing: {len(stale)} of {len(to_process)} listings "
                    f"are missing one of {', '.join(BACKFILL_FIELDS)}"
                )
                to_process = stale

            for listing in to_process:
                if not force and not backfill_missing and is_scraped(STORE_DIR, listing.listing_id):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                try:
                    _save_listing(listing, skip_photos, page)
                except Exception as exc:
                    print(f"skip listing (failed to process {listing.address}): {exc}")
                    continue

            if backfill_missing:
                _backfill_orphans(db_conn, page, skip_photos)

            report = compute_changes(
                present_listings, before, pinned_ids=frozenset(pinned_ids)
            )
            run_delisting(
                db_conn, PHOTOS_DIR, STORE_DIR, fetch_succeeded, report, before,
                frozenset(pinned_ids),
            )

    db_conn.close()

    all_listings = load_all_listings(STORE_DIR)
    write_csv(all_listings, PHOTOS_DIR, CSV_PATH)
    write_gallery(all_listings, PHOTOS_DIR, GALLERY_PATH)

    print(f"\nWrote {len(all_listings)} listings to {CSV_PATH}, {GALLERY_PATH}, and {DB_PATH}")


if __name__ == "__main__":
    main()
