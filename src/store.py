import json
import os
from dataclasses import asdict
from pathlib import Path

from src.models import Listing


def _listing_path(store_dir: Path, listing_id: str) -> Path:
    return store_dir / f"{listing_id}.json"


def is_scraped(store_dir: Path, listing_id: str) -> bool:
    return _listing_path(store_dir, listing_id).exists()


def needs_field_backfill(store_dir: Path, listing_id: str, fields: tuple[str, ...]) -> bool:
    """True when a stored listing predates one of `fields` -- i.e. the key is
    absent from its JSON entirely, or present but null.

    Lets a re-scrape target only the listings that would actually gain
    something, instead of --force redoing all of them. An unscraped listing
    returns False: it is new work, not backfill, and the normal
    is_scraped() path already picks it up.

    A null value counts as needing backfill because that is what a listing
    parsed by the older code looks like. The cost of re-fetching a listing
    whose value is legitimately null is one already-cached API row; the cost
    of skipping one is a permanently stale field.
    """
    path = _listing_path(store_dir, listing_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return True
    return any(data.get(field) is None for field in fields)


def save_listing(store_dir: Path, listing: Listing) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    final_path = _listing_path(store_dir, listing.listing_id)
    tmp_path = final_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(asdict(listing), indent=2))
    os.replace(tmp_path, final_path)


def delete_stored_listing(store_dir: Path, listing_id: str) -> None:
    """Removes a listing's JSON file from the store. Safe to call even if
    the file doesn't exist."""
    _listing_path(store_dir, listing_id).unlink(missing_ok=True)


def load_all_listings(store_dir: Path) -> list[Listing]:
    if not store_dir.exists():
        return []
    listings = []
    for path in sorted(store_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"warning: skipping corrupt listing file {path}: {exc}")
            continue
        listings.append(Listing(**data))
    return listings
