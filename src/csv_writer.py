import csv
from pathlib import Path

from src.models import Listing

FIELDNAMES = [
    "listing_id",
    "address",
    "city",
    "state",
    "zip_code",
    "price",
    "beds",
    "baths",
    "sqft",
    "lot_sqft",
    "parking_spaces",
    "year_built",
    "description",
    "amenities",
    "listing_url",
    "photo_dir",
]


def write_csv(listings: list[Listing], photos_root: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for listing in listings:
            writer.writerow(
                {
                    "listing_id": listing.listing_id,
                    "address": listing.address,
                    "city": listing.city,
                    "state": listing.state,
                    "zip_code": listing.zip_code,
                    "price": listing.price,
                    "beds": listing.beds,
                    "baths": listing.baths,
                    "sqft": listing.sqft,
                    "lot_sqft": listing.lot_sqft,
                    "parking_spaces": listing.parking_spaces,
                    "year_built": listing.year_built,
                    "description": listing.description,
                    "amenities": "; ".join(listing.amenities),
                    "listing_url": listing.listing_url,
                    "photo_dir": str(photos_root / listing.listing_id),
                }
            )
