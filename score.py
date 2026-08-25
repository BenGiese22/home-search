import sys
from pathlib import Path

from src.db import get_amenities, get_commute, get_connection, get_visual_score, query_listings, upsert_score
from src.models import Listing
from src.scoring import compute_collection_stats, score_listing

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"

# Points of composite score per $100k of price -- lets a $500k listing
# scoring 8 be compared against a $650k listing scoring 6.5 on a like-for-like
# basis. Deliberately not blended into the composite itself (see
# docs/journal/decisions.md, 2026-08-16): it's a separate lens for weighing
# score against price, not a scoring factor of its own.
PRICE_UNIT = 100_000.0


def value_score(composite: float, price_numeric: float | None) -> float | None:
    """None when price_numeric is missing (e.g. "Contact agent" listings) --
    there's nothing to divide by, so the field reads as unavailable rather
    than a misleading 0."""
    if not price_numeric:
        return None
    return composite / (price_numeric / PRICE_UNIT)


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
    sort_by_value = "--sort-by-value" in sys.argv

    conn = get_connection(DB_PATH)
    rows = query_listings(conn)
    listings = [_row_to_listing(row, get_amenities(conn, row["listing_id"])) for row in rows]
    price_numeric_by_id = {row["listing_id"]: row["price_numeric"] for row in rows}

    commute_by_id = {listing.listing_id: get_commute(conn, listing.listing_id) for listing in listings}
    sqft_values = [listing.sqft for listing in listings if listing.sqft]
    denver_minutes_values = [
        commute["denver_minutes"]
        for commute in commute_by_id.values()
        if commute is not None and commute["denver_minutes"] is not None
    ]
    room_count_values = [
        listing.beds + listing.baths for listing in listings if listing.beds
    ]
    stats = compute_collection_stats(sqft_values, denver_minutes_values, room_count_values)

    ranked = []
    for listing in listings:
        commute = commute_by_id[listing.listing_id]
        medtronic_minutes = commute["medtronic_minutes"] if commute else None
        denver_minutes = commute["denver_minutes"] if commute else None

        visual_row = get_visual_score(conn, listing.listing_id)
        visual_condition_score = None
        visual_outdoor_score = None
        if visual_row is not None and not visual_row["photo_score_unavailable"]:
            visual_condition_score = visual_row["condition_photo_score"]
            visual_outdoor_score = visual_row["outdoor_photo_score"]

        result = score_listing(
            listing, medtronic_minutes, denver_minutes, stats,
            visual_condition_score=visual_condition_score,
            visual_outdoor_score=visual_outdoor_score,
        )
        upsert_score(conn, listing.listing_id, result)
        value = value_score(result.composite, price_numeric_by_id[listing.listing_id])
        ranked.append((listing, result, value))

    conn.close()

    if sort_by_value:
        # Listings with no numeric price (value is None) sort last regardless
        # of composite, rather than defaulting to 0 and outranking real values.
        ranked.sort(key=lambda triple: (triple[2] is not None, triple[2]), reverse=True)
    else:
        ranked.sort(key=lambda triple: triple[1].composite, reverse=True)

    for listing, result, value in ranked:
        flag = "PASS" if result.passes_filters else "    "
        incomplete = " [incomplete data]" if result.has_incomplete_data else ""
        value_str = f"{value:5.2f}" if value is not None else "  n/a"
        print(
            f"[{flag}] {result.composite:5.1f}  {listing.address:40s} "
            f"commute={result.commute_score:5.1f} sqft={result.sqft_score:5.1f} "
            f"condition={result.condition_score:5.1f} outdoor={result.outdoor_score:5.1f} "
            f"rooms={result.room_count_score:5.1f} parking={result.parking_score:5.1f} "
            f"value={value_str}pts/$100k{incomplete}"
        )

    print(f"\nScored {len(ranked)} listings into {DB_PATH}")
    if not sort_by_value:
        print("(sorted by composite; rerun with --sort-by-value to sort by score per $100k)")


if __name__ == "__main__":
    main()
