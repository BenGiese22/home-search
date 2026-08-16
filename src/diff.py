import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.db import delete_listing, parse_price
from src.models import Listing
from src.photos import delete_photos
from src.store import delete_stored_listing


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


def apply_delisting(
    conn: sqlite3.Connection, photos_dir: Path, store_dir: Path, delisted_ids: list[str]
) -> None:
    """Removes each delisted listing everywhere it's stored: the DB row and
    its children, downloaded photos, and the JSON store file. Prints one
    line per listing removed. Shared by scrape.py and check.py so the
    cascade only lives in one place. A failure on one listing (e.g. a
    locked database) is logged and skipped rather than aborting the rest
    of the batch, matching every other per-item loop in this codebase."""
    for listing_id in delisted_ids:
        try:
            delete_listing(conn, listing_id)
            delete_photos(photos_dir, listing_id)
            delete_stored_listing(store_dir, listing_id)
        except Exception as exc:
            print(f"skip delisting (failed to remove {listing_id}): {exc}")
            continue
        print(f"delisted: {listing_id}")


def run_delisting(
    conn: sqlite3.Connection,
    photos_dir: Path,
    store_dir: Path,
    fetch_succeeded: bool,
    report: ChangeReport,
    before: dict[str, tuple[str, float | None]],
    pinned_ids: frozenset[str],
) -> None:
    """Decides whether it's safe to act on report.delisted_ids and, if so,
    removes them everywhere. Centralizes the should_apply_delisting +
    apply_delisting + skip-message flow so scrape.py and check.py share
    one implementation instead of duplicating it."""
    if should_apply_delisting(fetch_succeeded, report, before, pinned_ids):
        apply_delisting(conn, photos_dir, store_dir, report.delisted_ids)
    elif report.delisted_ids:
        print(
            f"skipping delisted-listing cleanup ({len(report.delisted_ids)} would be "
            "affected) — collection fetch failed or looks anomalous"
        )
