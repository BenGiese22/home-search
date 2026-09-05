import csv
import sys
from pathlib import Path

from src.turso_db import stage_connection
from src.db import (
    get_amenities_by_listing,
    get_commutes_by_listing,
    get_visual_scores_by_listing,
    listings_from_rows,
    query_listings,
    upsert_scores,
)
from src.commute import COMMUTE_SOURCE
from src.scoring import compute_collection_stats, finished_sqft, score_listing

DATA_DIR = Path("data")
RANKED_CSV_PATH = DATA_DIR / "ranked_report.csv"

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


def write_ranked_csv(ranked: list[tuple], csv_path: Path) -> None:
    """Same ranked order as the terminal report, with listing_url included so
    each row can be clicked straight through to the real listing."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rank", "passes_filters", "composite", "address", "price",
            "commute_score", "sqft_score", "condition_score", "outdoor_score",
            "room_count_score", "parking_score", "hoa_score", "value_per_100k",
            "has_incomplete_data", "listing_url",
        ])
        for rank, (listing, result, value) in enumerate(ranked, start=1):
            writer.writerow([
                rank,
                result.passes_filters,
                f"{result.composite:.1f}",
                listing.address,
                listing.price,
                f"{result.commute_score:.1f}",
                f"{result.sqft_score:.1f}",
                f"{result.condition_score:.1f}",
                f"{result.outdoor_score:.1f}",
                f"{result.room_count_score:.1f}",
                f"{result.parking_score:.1f}",
                f"{result.hoa_score:.1f}",
                f"{value:.2f}" if value is not None else "",
                result.has_incomplete_data,
                listing.listing_url,
            ])


def main() -> None:
    sort_by_value = "--sort-by-value" in sys.argv

    conn = stage_connection()
    rows = query_listings(conn)
    # Three set-at-a-time reads rather than three per listing. Against local
    # SQLite the difference is invisible; against Turso each statement is a
    # ~240ms HTTP round-trip, so the per-listing shape cost ~93 seconds per
    # run. tests/test_score_batching.py pins the statement count flat.
    amenities_by_id = get_amenities_by_listing(conn)
    commute_by_id = get_commutes_by_listing(conn)
    visual_by_id = get_visual_scores_by_listing(conn)

    listings = listings_from_rows(rows, amenities_by_id, {})
    price_numeric_by_id = {row["listing_id"]: row["price_numeric"] for row in rows}

    # Normalize against finished area, matching what score_sqft now scores --
    # mixing a total-footprint min/max with finished-area inputs would skew
    # every listing's percentile.
    sqft_values = [finished_sqft(listing) for listing in listings if finished_sqft(listing)]
    room_count_values = [
        listing.beds + listing.baths for listing in listings if listing.beds
    ]
    stats = compute_collection_stats(
        sqft_values=sqft_values, room_count_values=room_count_values
    )

    ranked = []
    score_rows = []
    for listing in listings:
        commute = commute_by_id.get(listing.listing_id)
        # A row measured a different way is not scored. It is not a number
        # for the same question -- a free-flow duration against a curve drawn
        # for rush hour scores every listing 100 -- so scoring it would rank
        # the un-recomputed half of the corpus above the recomputed half and
        # look entirely normal doing it. Treating it as absent puts the
        # listing on the neutral fallback AND raises has_incomplete_data,
        # which is loud: if the commutes stage ever lands after the scorer,
        # the whole corpus is flagged rather than quietly mis-ranked.
        medtronic_minutes = (
            commute["medtronic_minutes"]
            if commute is not None and commute["commute_source"] == COMMUTE_SOURCE
            else None
        )

        visual_row = visual_by_id.get(listing.listing_id)
        visual_condition_score = None
        visual_outdoor_score = None
        if visual_row is not None and not visual_row["photo_score_unavailable"]:
            visual_condition_score = visual_row["condition_photo_score"]
            visual_outdoor_score = visual_row["outdoor_photo_score"]

        result = score_listing(
            listing,
            medtronic_minutes=medtronic_minutes,
            stats=stats,
            visual_condition_score=visual_condition_score,
            visual_outdoor_score=visual_outdoor_score,
        )
        score_rows.append((listing.listing_id, result))
        value = value_score(result.composite, price_numeric_by_id[listing.listing_id])
        ranked.append((listing, result, value))

    # One batched write rather than one INSERT per listing, for the same
    # round-trip reason as the reads above.
    upsert_scores(conn, score_rows)
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
            f"hoa={result.hoa_score:5.1f} "
            f"value={value_str}pts/$100k{incomplete}"
        )

    write_ranked_csv(ranked, RANKED_CSV_PATH)

    print(f"\nScored {len(ranked)} listings into Turso")
    print(f"Wrote ranked report to {RANKED_CSV_PATH}")
    if not sort_by_value:
        print("(sorted by composite; rerun with --sort-by-value to sort by score per $100k)")


if __name__ == "__main__":
    main()
