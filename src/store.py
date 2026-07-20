import json
from dataclasses import asdict
from pathlib import Path

from src.models import Listing


def _listing_path(store_dir: Path, listing_id: str) -> Path:
    return store_dir / f"{listing_id}.json"


def is_scraped(store_dir: Path, listing_id: str) -> bool:
    return _listing_path(store_dir, listing_id).exists()


def save_listing(store_dir: Path, listing: Listing) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    _listing_path(store_dir, listing.listing_id).write_text(
        json.dumps(asdict(listing), indent=2)
    )


def load_all_listings(store_dir: Path) -> list[Listing]:
    if not store_dir.exists():
        return []
    listings = []
    for path in sorted(store_dir.glob("*.json")):
        data = json.loads(path.read_text())
        listings.append(Listing(**data))
    return listings
