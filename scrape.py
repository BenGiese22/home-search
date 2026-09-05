import random
import sys
import time
from pathlib import Path

from playwright.sync_api import Page

from src.auth import launch_authenticated_page
from src.config import load_config, load_env
from src.turso_db import stage_connection
from src.csv_writer import write_csv
from src.db import (
    bulk_upsert_listings,
    get_amenities_by_listing,
    get_listing_ids_missing_fields,
    get_photo_urls_by_listing,
    get_pinned_listing_ids,
    get_price_snapshot,
    hosted_photo_index,
    listing_ids_with_any_hosted_or_no_urls,
    listings_from_rows,
    needs_photo_work,
    query_listings,
    upsert_listing,
)
from src.diff import collection_fetch_is_trustworthy, compute_changes, run_delisting
from src.gallery import write_gallery
from src.models import Listing, select_present_listings
from src.photo_upload import upload_photos
from src.photos import download_photos
from src.scraper import (
    derive_listing_id_from_url,
    derive_pinned_ids_from_urls,
    fetch_collection_tabs,
    scrape_listing,
)

DATA_DIR = Path("data")
PHOTOS_DIR = DATA_DIR / "photos"
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
CSV_PATH = DATA_DIR / "listings.csv"
GALLERY_PATH = DATA_DIR / "gallery.html"
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


def should_process(*, force: bool, scraped: bool, stale: bool) -> bool:
    """Whether this listing needs work this run.

    Extracted from the Playwright loops so the flag interactions are testable
    without a browser. The decision is expensive in both directions:
    processing a listing that did not need it re-downloads 20-50 photos from
    Compass, and skipping one that did leaves it silently stale forever.

    The plan's signature also took a `backfill` flag. It is redundant once
    `stale` is per-listing -- and passing both invites the bug where a run
    with --backfill-missing processes everything because the flag was
    consulted instead of the listing's own staleness.
    """
    return force or stale or not scraped


def record_hosted(hosted: dict, listing: Listing) -> None:
    """Claim a freshly-scraped listing's positions in the in-memory index.

    A listing can arrive twice in one run: once from LISTING_URLS and again
    from the collection. Without this the second loop re-downloads photos the
    first just fetched, because the database index was read once at the top
    and does not know about work done since.
    """
    for position, url in enumerate(listing.photo_urls, start=1):
        hosted[(listing.listing_id, position)] = url


def _save_listing(listing: Listing, skip_photos: bool, page: Page) -> None:
    """Download this listing's photos, unless skipped.

    The listing row itself is already in Turso by the time this runs -- there
    is no longer a JSON store to write. Safe to call again for an
    already-scraped listing (see --force): download_photos() only fetches
    photos not already on disk, so a retry fills gaps from a prior partial
    failure rather than re-downloading everything.
    """
    if not skip_photos:
        download_photos(
            listing.photo_urls,
            PHOTOS_DIR / listing.listing_id,
            _build_fetch_bytes(page),
            sleep_fn=_photo_jitter,
        )
    print(f"scraped: {listing.address}")


def _backfill_orphans(db_conn, page: Page, skip_photos: bool) -> None:
    """Refresh stale listings that neither ingestion path can reach.

    A listing pinned in the DB but no longer in LISTING_URLS and no longer in
    the active collection is invisible to both branches above -- it would keep
    whatever fields it had when it was last scraped, forever. Its listing_url
    is still on its row, so re-scrape it from that.
    """
    # One statement for the whole corpus, not one file open per listing.
    stale_ids = set(get_listing_ids_missing_fields(db_conn, BACKFILL_FIELDS))
    stale = [row for row in query_listings(db_conn) if row["listing_id"] in stale_ids]
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


# Photos hosted per listing in Blob. 0 means no cap -- see issue #17; on Pro
# the whole corpus is roughly $0.02 one-time in operations.
MAX_PHOTOS_PER_LISTING = int(load_env().get("MAX_PHOTOS_PER_LISTING", 0))


def _upload_photos_for(db_conn) -> None:
    """Uploads any not-yet-hosted photo for every tracked listing.

    Runs over all listings rather than only the ones processed this run: the
    skip set makes that nearly free (one query), and it means a photo whose
    upload failed or was interrupted on an earlier run gets picked up rather
    than being stranded forever.

    hosted_photos lives in the same database the stage already holds open, so
    this reuses that connection rather than opening a second one. An upload
    problem must not fail the scrape -- the listings and photos are already
    safely written, and the next run retries.

    The stage passes the current photo URLs, not just the listing ids: a
    photo's identity is its source URL, and without the map the upload would
    fall back to "position N is already hosted" -- the key that let 6085 West
    82nd Drive keep 44 photos from its previous listing. One extra statement
    for the whole corpus.
    """
    env = load_env()
    if not env.get("BLOB_READ_WRITE_TOKEN"):
        print("skipping photo upload: BLOB_READ_WRITE_TOKEN is not set")
        return
    photo_urls = get_photo_urls_by_listing(db_conn)
    if not photo_urls:
        return
    uploaded, failed = upload_photos(
        db_conn, PHOTOS_DIR, photo_urls,
        env["BLOB_READ_WRITE_TOKEN"],
        max_per_listing=MAX_PHOTOS_PER_LISTING,
    )
    if uploaded or failed:
        summary = f"uploaded {uploaded} new photo(s)"
        if failed:
            summary += f" ({failed} failed -- a rerun retries them)"
        print(summary)


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
    # Fail before touching Compass rather than after. Without a token the run
    # downloads every photo and hosts none, so the viewer sees nothing new
    # and the next run downloads them all again -- a lot of traffic against
    # the one site whose bot detection is still an open question.
    if not skip_photos and not load_env().get("BLOB_READ_WRITE_TOKEN"):
        sys.exit(
            "scrape.py: BLOB_READ_WRITE_TOKEN is not set, so downloaded photos "
            "could not be hosted. Set it, or pass --skip-photos."
        )
    db_conn = stage_connection()
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
    # One read of the whole hosted index, mutated in memory as listings are
    # scraped. This is what replaced is_scraped()'s per-listing file check:
    # a question the database can answer, so it works with no disk at all.
    hosted = hosted_photo_index(db_conn)
    # Only computed when --backfill-missing asks for it; otherwise nothing is
    # stale and the set stays empty rather than costing a statement.
    stale_ids = (
        set(get_listing_ids_missing_fields(db_conn, BACKFILL_FIELDS))
        if backfill_missing else set()
    )
    # Only needed by the explicit-URL loop, which decides before it knows a
    # listing's photo URLs. Skipped entirely when LISTING_URLS is unset.
    prefetch_settled = (
        listing_ids_with_any_hosted_or_no_urls(db_conn) if config.listing_urls else frozenset()
    )

    with launch_authenticated_page(config, LOGIN_URL, AUTH_STATE_PATH) as page:
        for url in config.listing_urls:
            precheck_id = derive_listing_id_from_url(url)
            # --backfill-missing must reach pinned listings too. They take the
            # detail-page branch rather than the collection one, so filtering
            # only the collection batch left every pinned listing stale.
            stale = (
                backfill_missing and precheck_id is not None and precheck_id in stale_ids
            )
            # A weak gate, and deliberately so: this runs BEFORE the detail
            # page is fetched, so the listing's current photo URLs are not
            # known yet. Membership means "nothing suggests work is needed";
            # the real decision is made below, once the URLs are in hand.
            if (
                not force and not stale and precheck_id
                and precheck_id in prefetch_settled
            ):
                print(f"skip (already scraped): {url}")
                continue
            try:
                listing = scrape_listing(page, url)
                pinned_ids.add(listing.listing_id)
                upsert_listing(db_conn, listing, is_pinned=True)
                stale = stale or (backfill_missing and listing.listing_id in stale_ids)
                if not should_process(
                    force=force, scraped=not needs_photo_work(listing, hosted), stale=stale
                ):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                _save_listing(listing, skip_photos, page)
                record_hosted(hosted, listing)
            except Exception as exc:
                print(f"skip listing (failed to process {url}): {exc}")
                continue

        if config.collection_url:
            fetch = fetch_collection_tabs(
                page, config.collection_url, config.collection_tabs
            )
            for tab, count in fetch.counts.items():
                print(f"collection/{tab} returned {count} listings")
            for tab, exc in fetch.errors.items():
                print(f"failed to fetch collection/{tab}: {exc}")
            # Already deduped across tabs, favorites copy winning.
            collection_listings = fetch.listings
            fetch_succeeded = collection_fetch_is_trustworthy(fetch, before)

            # A listing whose fresh status comes back non-Active (Expired,
            # Sold, Withdrawn, ...) is treated as absent from the
            # collection for every purpose below -- upserting, photo/JSON
            # saving, and delisting -- exactly like one that dropped out of
            # every fetched tab entirely, going through the same
            # reviewed, circuit-breaker-protected removal path rather than
            # new logic. Pinned listings are exempt, same as delisting
            # already exempts them: an explicit pin means Ben wants it
            # tracked regardless of what the MLS status says. A favorite
            # that has gone Pending is exempt too -- see
            # select_present_listings and issue #50.
            present_listings = select_present_listings(
                collection_listings, pinned_ids, fetch.favorite_ids
            )
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
                    if needs_photo_work(listing, hosted)
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
                    if listing.listing_id in stale_ids
                ]
                print(
                    f"--backfill-missing: {len(stale)} of {len(to_process)} listings "
                    f"are missing one of {', '.join(BACKFILL_FIELDS)}"
                )
                to_process = stale

            for listing in to_process:
                if not should_process(
                    force=force,
                    scraped=not needs_photo_work(listing, hosted),
                    stale=backfill_missing,
                ):
                    print(f"skip (already scraped): {listing.address}")
                    continue
                try:
                    _save_listing(listing, skip_photos, page)
                    record_hosted(hosted, listing)
                except Exception as exc:
                    print(f"skip listing (failed to process {listing.address}): {exc}")
                    continue

            if backfill_missing:
                _backfill_orphans(db_conn, page, skip_photos)

            report = compute_changes(
                present_listings, before, pinned_ids=frozenset(pinned_ids)
            )
            # The blob token is passed so a delisted listing's hosted photos
            # are reclaimed rather than stranded -- hosted_photos.blob_url is
            # the only record of them, and pruning the rows without it is how
            # 1,813 orphans (~371 MB) accumulated. Absent (no token
            # configured), the URLs are printed instead of lost.
            run_delisting(
                db_conn, PHOTOS_DIR, fetch_succeeded, report, before,
                frozenset(pinned_ids),
                blob_token=load_env().get("BLOB_READ_WRITE_TOKEN"),
            )

    _upload_photos_for(db_conn)

    # Rebuilt from Turso, not from a directory of JSON files. The old form
    # produced a CSV and gallery containing only whatever the local store
    # happened to hold -- a sandbox with no disk wrote a 2-listing gallery
    # from a 100-listing corpus.
    rows = query_listings(db_conn)
    all_listings = listings_from_rows(
        rows, get_amenities_by_listing(db_conn), get_photo_urls_by_listing(db_conn)
    )
    db_conn.close()
    write_csv(all_listings, PHOTOS_DIR, CSV_PATH)
    write_gallery(all_listings, PHOTOS_DIR, GALLERY_PATH)

    print(f"\nWrote {len(all_listings)} listings to {CSV_PATH}, {GALLERY_PATH}, and Turso")


if __name__ == "__main__":
    main()
