from src.commute import geocode, route_miles_minutes


def test_geocode_returns_lat_lon_from_first_result():
    def fake_http_get(url: str) -> list[dict]:
        return [{"lat": "39.7527", "lon": "-105.0016"}]

    result = geocode("Denver Union Station, Denver, CO", fake_http_get)

    assert result == (39.7527, -105.0016)


def test_geocode_returns_none_when_no_results():
    result = geocode("nowhere at all", lambda url: [])

    assert result is None


def test_geocode_returns_none_on_malformed_result():
    result = geocode("bad data", lambda url: [{"unexpected": "shape"}])

    assert result is None


def test_route_miles_minutes_converts_meters_and_seconds():
    def fake_http_get(url: str) -> dict:
        return {"routes": [{"distance": 16093.4, "duration": 1200.0}]}

    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), fake_http_get)

    assert result == (10.0, 20.0)


def test_route_miles_minutes_returns_none_when_no_routes():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": []})

    assert result is None


def test_route_miles_minutes_returns_none_on_malformed_response():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": [{}]})

    assert result is None


def test_route_miles_minutes_returns_none_on_non_numeric_distance():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": [{"distance": "bad", "duration": 1200.0}]})

    assert result is None


def test_route_miles_minutes_returns_none_on_non_numeric_duration():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": [{"distance": 16093.4, "duration": "bad"}]})

    assert result is None
