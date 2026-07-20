from src.models import Listing


def parse_listing_object(obj: dict, listing_url: str) -> Listing:
    location = obj.get("location", {})
    size = obj.get("size", {})
    price = obj.get("price", {})
    building = obj.get("buildingInfo", {})
    detailed = obj.get("detailedInfo", {})
    media = obj.get("media", [])

    full_baths = size.get("fullBathrooms", 0) or 0
    half_baths = size.get("halfBathrooms", 0) or 0
    three_quarter_baths = size.get("threeQuarterBathrooms", 0) or 0
    baths = full_baths + half_baths * 0.5 + three_quarter_baths * 0.75

    return Listing(
        listing_id=obj.get("listingIdSHA") or obj.get("feedListingId", ""),
        address=location.get("prettyAddress", ""),
        city=location.get("city", ""),
        state=location.get("state", ""),
        zip_code=location.get("zipCode", ""),
        price=price.get("formatted", ""),
        beds=size.get("bedrooms", 0) or 0,
        baths=baths,
        sqft=size.get("squareFeet", 0) or 0,
        lot_sqft=size.get("lotSizeInSquareFeet", 0) or 0,
        year_built=building.get("buildingYearOpened", 0) or 0,
        description=obj.get("description", ""),
        amenities=list(detailed.get("amenities", [])),
        photo_urls=[m["originalUrl"] for m in media if "originalUrl" in m],
        listing_url=listing_url,
    )
