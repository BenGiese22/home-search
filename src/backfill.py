from src.models import Listing


def dedupe_by_listing_id(listings: list[Listing]) -> list[Listing]:
    """Keep the first occurrence of each listing_id. fetch_collection_listings
    can return duplicates across paginated requests; downloading and saving
    the same listing twice wastes a full photo-download pass."""
    seen: set[str] = set()
    deduped = []
    for listing in listings:
        if listing.listing_id in seen:
            continue
        seen.add(listing.listing_id)
        deduped.append(listing)
    return deduped
