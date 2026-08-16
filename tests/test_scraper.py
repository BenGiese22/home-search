import json
from pathlib import Path

import pytest

from src.scraper import (
    derive_listing_id_from_url,
    derive_pinned_ids_from_urls,
    extract_collection_id,
    parse_collection_response,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "collection_response_sample.json"


def test_derive_listing_id_from_url_homedetails_lid():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/2145067054346865465_lid/"
    assert derive_listing_id_from_url(url) == "2145067054346865465"


def test_derive_listing_id_from_url_listing_view():
    url = "https://www.compass.com/listing/2130651237632606465/view?agent_id=688995414728a40001928728"
    assert derive_listing_id_from_url(url) == "2130651237632606465"


def test_derive_listing_id_from_url_no_id_returns_none():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/"
    assert derive_listing_id_from_url(url) is None


def test_derive_pinned_ids_from_urls_extracts_ids():
    urls = [
        "https://www.compass.com/listing/2130651237632606465/view",
        "https://www.compass.com/listing/2120506603298373729/view",
    ]

    assert derive_pinned_ids_from_urls(urls) == frozenset(
        {"2130651237632606465", "2120506603298373729"}
    )


def test_derive_pinned_ids_from_urls_skips_urls_with_no_id():
    urls = [
        "https://www.compass.com/listing/2130651237632606465/view",
        "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/",
    ]

    assert derive_pinned_ids_from_urls(urls) == frozenset({"2130651237632606465"})


def test_derive_pinned_ids_from_urls_empty_list_returns_empty_frozenset():
    assert derive_pinned_ids_from_urls([]) == frozenset()


def test_extract_collection_id_from_app_collection_url():
    url = (
        "https://www.compass.com/app/collection/6a27426b698343000129b139/matches"
        "?source=deals&page=1&pageSize=120&savedSearchId=984690897574691433&sort=time_added"
    )
    assert extract_collection_id(url) == "6a27426b698343000129b139"


def test_extract_collection_id_no_match_raises():
    with pytest.raises(ValueError, match="collection ID"):
        extract_collection_id("https://www.compass.com/overview/")


def test_parse_collection_response_from_real_fixture():
    data = json.loads(FIXTURE_PATH.read_text())
    listings = parse_collection_response(data)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.listing_id == "2153779980783572393"
    assert listing.address == "6330 West 112th Place"
    assert listing.city == "Broomfield"
    assert listing.price == "$525,000"
    assert listing.beds == 3
    assert listing.baths == 1.75  # 1 full + 0 half + 1 three-quarter*0.75
    assert listing.sqft == 1779
    assert listing.parking_spaces == 2
    assert listing.year_built == 1979
    assert "Attached Garage" in listing.amenities
    assert listing.photo_urls == [
        "https://www.compass.com/m/7fffe4982e173ceee72fa477c4dc66e1d7c31f46c38be3efff335375f761cbbf/origin.jpg"
    ]
    assert listing.listing_url == (
        "https://www.compass.com/homedetails/6330-W-112th-Pl-Broomfield-CO-80020/2153779980783572393_lid/"
    )


def test_parse_collection_response_empty_page_returns_empty_list():
    assert parse_collection_response({"totalListings": 0, "currentPageListings": []}) == []
