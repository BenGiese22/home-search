import json
import os
from dataclasses import asdict
from pathlib import Path

from src.models import Listing


def _listing_path(store_dir: Path, listing_id: str) -> Path:
    return store_dir / f"{listing_id}.json"


def is_scraped(store_dir: Path, listing_id: str) -> bool:
    return _listing_path(store_dir, listing_id).exists()


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
