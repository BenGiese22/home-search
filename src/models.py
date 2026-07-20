from dataclasses import dataclass


@dataclass
class Listing:
    listing_id: str
    address: str
    city: str
    state: str
    zip_code: str
    price: str
    beds: int
    baths: float
    sqft: int
    lot_sqft: int
    parking_spaces: int
    year_built: int
    description: str
    amenities: list[str]
    photo_urls: list[str]
    listing_url: str
