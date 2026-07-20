from src.scraper import derive_listing_id_from_url


def test_derive_listing_id_from_url_homedetails_lid():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/2145067054346865465_lid/"
    assert derive_listing_id_from_url(url) == "2145067054346865465"


def test_derive_listing_id_from_url_listing_view():
    url = "https://www.compass.com/listing/2130651237632606465/view?agent_id=688995414728a40001928728"
    assert derive_listing_id_from_url(url) == "2130651237632606465"


def test_derive_listing_id_from_url_no_id_returns_none():
    url = "https://www.compass.com/homedetails/2765-Canossa-Dr-Broomfield-CO-80020/"
    assert derive_listing_id_from_url(url) is None
