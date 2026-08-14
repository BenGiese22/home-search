from dataclasses import dataclass

from src.db import parse_price
from src.models import Listing


@dataclass
class PriceChange:
    listing: Listing
    old_price: str
    new_price: str


@dataclass
class ChangeReport:
    new_listings: list[Listing]
    price_changes: list[PriceChange]


def compute_changes(
    fetched: list[Listing], before: dict[str, tuple[str, float | None]]
) -> ChangeReport:
    """Compares freshly-fetched listings against a price snapshot taken
    before this run's upserts. A listing_id absent from `before` is new;
    one present with a different price_numeric is a price change."""
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
    return ChangeReport(new_listings=new_listings, price_changes=price_changes)


def format_report(report: ChangeReport) -> str:
    lines = [f"{len(report.new_listings)} new listing(s)"]
    for listing in report.new_listings:
        lines.append(f"  NEW  {listing.address} — {listing.price} — {listing.listing_url}")

    lines.append(f"{len(report.price_changes)} price change(s)")
    for change in report.price_changes:
        lines.append(
            f"  {change.listing.address}: {change.old_price} -> {change.new_price}"
        )

    return "\n".join(lines)
