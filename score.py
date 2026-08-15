from pathlib import Path

from src.db import get_amenities, get_commute, get_connection, query_listings, upsert_score
from src.models import Listing
from src.scoring import compute_collection_stats, score_listing

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"


def _row_to_listing(row, amenities: list[str]) -> Listing:
    return Listing(
        listing_id=row["listing_id"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip_code"],
        price=row["price"],
        beds=row["beds"],
        baths=row["baths"],
        sqft=row["sqft"],
        lot_sqft=row["lot_sqft"],
        parking_spaces=row["parking_spaces"],
        year_built=row["year_built"],
        description=row["description"],
        amenities=amenities,
        photo_urls=[],
        listing_url=row["listing_url"],
    )


def main() -> None:
    conn = get_connection(DB_PATH)
    rows = query_listings(conn)
    listings = [_row_to_listing(row, get_amenities(conn, row["listing_id"])) for row in rows]

    commute_by_id = {listing.listing_id: get_commute(conn, listing.listing_id) for listing in listings}
    sqft_values = [listing.sqft for listing in listings if listing.sqft]
    denver_minutes_values = [
        commute["denver_minutes"]
        for commute in commute_by_id.values()
        if commute is not None and commute["denver_minutes"] is not None
    ]
    stats = compute_collection_stats(sqft_values, denver_minutes_values)

    ranked = []
    for listing in listings:
        commute = commute_by_id[listing.listing_id]
        medtronic_minutes = commute["medtronic_minutes"] if commute else None
        denver_minutes = commute["denver_minutes"] if commute else None
        result = score_listing(listing, medtronic_minutes, denver_minutes, stats)
        upsert_score(conn, listing.listing_id, result)
        ranked.append((listing, result))

    conn.close()

    ranked.sort(key=lambda pair: pair[1].composite, reverse=True)
    for listing, result in ranked:
        flag = "PASS" if result.passes_filters else "    "
        print(
            f"[{flag}] {result.composite:5.1f}  {listing.address:40s} "
            f"commute={result.commute_score:5.1f} sqft={result.sqft_score:5.1f} "
            f"condition={result.condition_score:5.1f} outdoor={result.outdoor_score:5.1f} "
            f"parking={result.parking_score:5.1f}"
        )

    print(f"\nScored {len(ranked)} listings into {DB_PATH}")


if __name__ == "__main__":
    main()
