import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.blob_upload import delete_blobs
from src.db import bulk_delete_listings, delete_listing, parse_price
from src.models import Listing
from src.photos import delete_photos


@dataclass
class PriceChange:
    listing: Listing
    old_price: str
    new_price: str


@dataclass
class ChangeReport:
    new_listings: list[Listing]
    price_changes: list[PriceChange]
    delisted_ids: list[str]


def compute_changes(
    fetched: list[Listing],
    before: dict[str, tuple[str, float | None]],
    pinned_ids: frozenset[str] = frozenset(),
) -> ChangeReport:
    """Compares freshly-fetched listings against a price snapshot taken
    before this run's upserts. A listing_id absent from `before` is new;
    one present with a different price_numeric is a price change; a
    listing_id present in `before` but absent from `fetched` has dropped
    out of the live collection — delisted. `pinned_ids` (listings tracked
    individually via LISTING_URLS, not returned by any collection fetch)
    are never treated as delisted — their absence from `fetched` doesn't
    mean anything, since they were never expected to appear there."""
    new_listings = []
    price_changes = []
    seen_ids: set[str] = set()
    for listing in fetched:
        if listing.listing_id in seen_ids:
            continue
        seen_ids.add(listing.listing_id)
        prior = before.get(listing.listing_id)
        if prior is None:
            new_listings.append(listing)
            continue
        old_price, old_price_numeric = prior
        if parse_price(listing.price) != old_price_numeric:
            price_changes.append(PriceChange(listing, old_price, listing.price))
    delisted_ids = sorted(set(before.keys()) - seen_ids - pinned_ids)
    return ChangeReport(
        new_listings=new_listings, price_changes=price_changes, delisted_ids=delisted_ids
    )


def format_report(report: ChangeReport) -> str:
    lines = [f"{len(report.new_listings)} new listing(s)"]
    for listing in report.new_listings:
        lines.append(f"  NEW  {listing.address} — {listing.price} — {listing.listing_url}")

    lines.append(f"{len(report.price_changes)} price change(s)")
    for change in report.price_changes:
        lines.append(
            f"  {change.listing.address}: {change.old_price} -> {change.new_price}"
        )

    lines.append(f"{len(report.delisted_ids)} delisted")
    for listing_id in report.delisted_ids:
        lines.append(f"  DELISTED  {listing_id}")

    return "\n".join(lines)


def collection_fetch_is_trustworthy(fetch, before) -> bool:
    """Whether a multi-tab collection fetch is complete enough to delist from.

    This computes the `fetch_succeeded` flag that should_apply_delisting
    already gates on; the fraction check below is unchanged and still applies
    on top of it.

    The rule is `all tabs succeeded`, and the asymmetry matters. Suppose
    favorites (~26) fails while matches (~149) succeeds: the merged fetch is
    just the matches, so every favorites-only listing looks delisted -- but
    that is only ~6% of everything tracked, which sails under
    MAX_DELISTED_FRACTION and would wipe the entire favorites bucket. The next
    run would re-add them as new listings, re-downloading photos and losing
    their scores. The reverse case (matches fails) only survives today by the
    accident that 139/159 happens to trip the fraction.

    A tab returning zero listings gets the same treatment for the same reason:
    an empty 200 OK -- the exact failure the fraction guard was built for --
    is invisible to a global fraction when the empty tab is the small one. Not
    delisting for a run is cheap; deleting a bucket's photos and scores is not.
    """
    if fetch.errors:
        return False
    # Bootstrap: nothing tracked yet, so there is nothing to wrongly delete.
    if before and any(count == 0 for count in fetch.counts.values()):
        return False
    return True


MAX_DELISTED_FRACTION = 0.5


def should_apply_delisting(
    fetch_succeeded: bool,
    report: ChangeReport,
    before: dict[str, tuple[str, float | None]],
    pinned_ids: frozenset[str],
) -> bool:
    """Guards the delisting cascade against two ways a bad collection fetch
    can masquerade as a real mass delisting: an exception during the fetch
    (caller passes fetch_succeeded=False), or a fetch that "succeeds" but
    returns empty or anomalously few results (e.g. a transient API glitch
    responding 200 OK with zero matches). If more than MAX_DELISTED_FRACTION
    of every eligible (non-pinned) listing this project already knows about
    would be wiped in one run, that's treated as more consistent with a bad
    fetch than a real mass delisting -- refuse and let a human investigate
    rather than deleting silently. Deliberately no small-count exemption:
    100% of a tiny tracked collection is exactly as suspicious as 100% of a
    large one, and a genuinely legitimate full delisting can always be
    confirmed by a human who sees the "skipped" message, rather than
    happening automatically and silently."""
    if not fetch_succeeded:
        return False
    eligible = len(set(before.keys()) - pinned_ids)
    if eligible == 0:
        return True
    return len(report.delisted_ids) / eligible <= MAX_DELISTED_FRACTION


def _hosted_blob_urls(
    conn: sqlite3.Connection, listing_ids: list[str]
) -> dict[str, list[str]]:
    """The blob URLs of the doomed listings' hosted photos, keyed by listing.

    ONE statement for the whole batch, taken BEFORE the prune -- once the rows
    are gone there is no record anywhere of what those blobs were. Returns an
    empty mapping when the table does not exist, which is the local schema
    (hosted_photos lives only in the hosted one), so a local run is not broken
    by a table it never had.
    """
    if not listing_ids:
        return {}
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "hosted_photos" not in existing:
        return {}
    placeholders = ", ".join("?" for _ in listing_ids)
    by_listing: dict[str, list[str]] = {}
    for row in conn.execute(
        f"SELECT listing_id, blob_url FROM hosted_photos "
        f"WHERE listing_id IN ({placeholders})",
        tuple(listing_ids),
    ):
        if row[1]:
            by_listing.setdefault(row[0], []).append(row[1])
    return by_listing


def apply_delisting(
    conn: sqlite3.Connection,
    photos_dir: Path,
    delisted_ids: list[str],
    blob_token: str | None = None,
    delete_fn: Callable[[list[str], str], None] = delete_blobs,
) -> None:
    """Removes each delisted listing everywhere it's stored: the DB row and
    its children, downloaded photos, the JSON store file, and now the blobs
    its hosted_photos rows pointed at. Prints one line per listing removed.
    Shared by scrape.py and check.py so the cascade only lives in one place.
    A failure on one listing (e.g. a locked database) is logged and skipped
    rather than aborting the rest of the batch, matching every other per-item
    loop in this codebase.

    The blob URLs are read first, in one statement, because pruning the rows
    destroys the only record of them -- that is how 1,813 blobs (~371 MB)
    were stranded. The DELETE still happens before the blob delete, though:
    a row pointing at a blob that no longer exists is a broken image in the
    viewer, while a blob with no row is only wasted bytes. Blob failures are
    printed, never raised; without a blob_token (a --skip-photos dev run) the
    URLs are printed instead of deleted, so they are never lost silently.
    """
    if not delisted_ids:
        return
    blob_urls = _hosted_blob_urls(conn, delisted_ids)
    # Fast path: one statement per table, child-first, rather than a
    # delete_listing() per listing -- against Turso the per-listing form is
    # ~7 round-trips each.
    try:
        bulk_delete_listings(conn, delisted_ids)
        removed = list(delisted_ids)
    except Exception as exc:
        # Falling back per listing preserves the property the batched form
        # gives up: one undeletable listing (a locked database, a constraint
        # violation) must not strand the rest of the batch.
        print(f"batched delisting failed ({exc}); retrying one at a time")
        removed = []
        for listing_id in delisted_ids:
            try:
                delete_listing(conn, listing_id)
            except Exception as inner:
                print(f"skip delisting (failed to remove {listing_id}): {inner}")
                continue
            removed.append(listing_id)

    # Only clean up files for listings actually gone from the database.
    # Deleting a listing's photos while its row survives would leave a
    # tracked listing with no images and no way to notice.
    for listing_id in removed:
        try:
            delete_photos(photos_dir, listing_id)
        except Exception as exc:
            print(f"delisted {listing_id} from the database, but its local "
                  f"files could not be removed: {exc}")
            continue
        print(f"delisted: {listing_id}")

    # Only for listings whose rows actually went. A listing still in the
    # database must keep its images, exactly as it keeps its photos on disk.
    doomed_blobs = [url for listing_id in removed for url in blob_urls.get(listing_id, [])]
    if not doomed_blobs:
        return
    if blob_token is None:
        print(
            f"no blob token: {len(doomed_blobs)} blob(s) left in the store for "
            "delisted listings. Their rows are gone, so these URLs are the "
            "only remaining handle on them:"
        )
        for url in doomed_blobs:
            print(f"  {url}")
        return
    try:
        delete_fn(doomed_blobs, blob_token)
    except Exception as exc:
        print(f"failed to delete {len(doomed_blobs)} blob(s) ({exc}); "
              "their rows are already gone, so these URLs are the only "
              "remaining handle on them:")
        for url in doomed_blobs:
            print(f"  {url}")


def supersede_relisted(
    conn,
    relists: list[tuple[str, str, str]],
    photos_dir,
    blob_token: str | None = None,
    delete_fn: Callable[[list[str], str], None] = delete_blobs,
) -> list[str]:
    """Remove the stale predecessor of each relisted property.

    A relist arrives as a new listing_id for a house already in the corpus,
    so the delisting cascade never sees it: the old row is not "absent from
    the collection" in any way the cascade recognises, and pinned rows are
    exempt from it regardless. The two rows then coexist -- one property
    scored twice, ranked twice, and paid for twice at the vision API.

    Reuses run_delisting's blob handling rather than calling delete_listing
    directly, and that is not incidental. hosted_photos.blob_url is the ONLY
    record of an uploaded image, so the URLs have to be read before the rows
    go. Deleting first is how 1,813 orphans (~371 MB) accumulated once
    already.
    """
    if not relists:
        return []
    for address, keep, drop in relists:
        print(f"relist: {address} -- {drop} superseded by {keep}")
    doomed = [drop for _address, _keep, drop in relists]
    apply_delisting(
        conn, photos_dir, doomed, blob_token=blob_token, delete_fn=delete_fn
    )
    return doomed


def run_delisting(
    conn: sqlite3.Connection,
    photos_dir: Path,
    fetch_succeeded: bool,
    report: ChangeReport,
    before: dict[str, tuple[str, float | None]],
    pinned_ids: frozenset[str],
    blob_token: str | None = None,
    delete_fn: Callable[[list[str], str], None] = delete_blobs,
) -> None:
    """Decides whether it's safe to act on report.delisted_ids and, if so,
    removes them everywhere. Centralizes the should_apply_delisting +
    apply_delisting + skip-message flow so scrape.py and check.py share
    one implementation instead of duplicating it.

    blob_token is optional so a caller with no Blob credentials (a
    --skip-photos run) still delists -- it just prints the stranded URLs
    rather than reclaiming them."""
    if should_apply_delisting(fetch_succeeded, report, before, pinned_ids):
        apply_delisting(
            conn, photos_dir, report.delisted_ids,
            blob_token=blob_token, delete_fn=delete_fn,
        )
    elif report.delisted_ids:
        print(
            f"skipping delisted-listing cleanup ({len(report.delisted_ids)} would be "
            "affected) — collection fetch failed or looks anomalous"
        )
