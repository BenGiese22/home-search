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


def apply_delisting(
    conn: sqlite3.Connection, photos_dir: Path, store_dir: Path, delisted_ids: list[str]
) -> None:
    """Removes each delisted listing everywhere it's stored: the DB row and
    its children, downloaded photos, and the JSON store file. Prints one
    line per listing removed. Shared by scrape.py and check.py so the
    cascade only lives in one place."""
    for listing_id in delisted_ids:
        delete_listing(conn, listing_id)
        delete_photos(photos_dir, listing_id)
        delete_stored_listing(store_dir, listing_id)
        print(f"delisted: {listing_id}")
