import pytest

from src.commute import (
    CommuteResult,
    compute_commute,
    geocode,
    geocode_census,
    geocode_with_fallback,
    resolve_destination,
    route_miles_minutes,
)

DENVER = (39.7527, -105.0016)
MEDTRONIC = (39.9997, -105.0908)


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


def test_resolve_destination_uses_primary_when_it_geocodes():
    coords, used_fallback = resolve_destination("Medtronic, Lafayette, CO", lambda addr: MEDTRONIC)

    assert coords == MEDTRONIC
    assert used_fallback is False


def test_resolve_destination_falls_back_when_primary_fails():
    def fake_geocode(addr: str):
        return None if addr == "Medtronic, Lafayette, CO" else MEDTRONIC

    coords, used_fallback = resolve_destination(
        "Medtronic, Lafayette, CO", fake_geocode, fallback_address="Lafayette, CO"
    )

    assert coords == MEDTRONIC
    assert used_fallback is True


def test_resolve_destination_raises_when_both_fail():
    with pytest.raises(RuntimeError):
        resolve_destination("nowhere", lambda addr: None, fallback_address="also nowhere")


def test_resolve_destination_raises_when_primary_fails_and_no_fallback_given():
    with pytest.raises(RuntimeError):
        resolve_destination("nowhere", lambda addr: None)


def test_compute_commute_returns_geocode_failed_when_address_doesnt_geocode():
    result = compute_commute(
        "1 Nowhere Rd", DENVER, MEDTRONIC, lambda addr: None, lambda o, d: (5.0, 10.0)
    )

    assert result == CommuteResult(None, None, None, None, None, None, geocode_failed=True)


def test_compute_commute_computes_both_legs():
    origin = (39.85, -105.05)

    def fake_route(o, d):
        return (5.0, 12.0) if d == DENVER else (8.0, 22.0)

    result = compute_commute(
        "1 Real St", DENVER, MEDTRONIC, lambda addr: origin, fake_route
    )

    assert result == CommuteResult(
        lat=39.85, lon=-105.05,
        denver_miles=5.0, denver_minutes=12.0,
        medtronic_miles=8.0, medtronic_minutes=22.0,
        geocode_failed=False,
    )


def test_compute_commute_marks_failed_when_a_route_fails():
    origin = (39.85, -105.05)

    def fake_route(o, d):
        return None if d == MEDTRONIC else (5.0, 12.0)

    result = compute_commute("1 Real St", DENVER, MEDTRONIC, lambda addr: origin, fake_route)

    assert result.geocode_failed is True
    assert result.denver_miles == 5.0
    assert result.medtronic_miles is None


# --- Census fallback -------------------------------------------------------

CENSUS_HIT = {
    "result": {
        "addressMatches": [
            {"coordinates": {"x": -105.10862, "y": 39.88788}}
        ]
    }
}


def test_census_reads_lat_from_y_and_lon_from_x():
    """Census names its coordinates x/y, not lon/lat. Reading them in the
    obvious order puts every Colorado listing in Kazakhstan."""
    assert geocode_census("10538 Kipling Place", lambda _u: CENSUS_HIT) == (
        39.88788,
        -105.10862,
    )


def test_census_returns_none_for_no_match():
    assert geocode_census("nowhere", lambda _u: {"result": {"addressMatches": []}}) is None


def test_census_survives_an_unexpected_payload():
    for payload in ({}, {"result": {}}, {"result": {"addressMatches": [{}]}}):
        assert geocode_census("x", lambda _u, p=payload: p) is None


def test_fallback_is_not_called_when_the_primary_succeeds():
    """Nominatim stays primary because Census cannot resolve a POI name, and
    the destinations are POI names."""
    calls = []
    coords, used = geocode_with_fallback(
        "somewhere",
        lambda _a: (1.0, 2.0),
        lambda a: calls.append(a),
    )
    assert coords == (1.0, 2.0)
    assert used is False
    assert calls == []


def test_fallback_runs_when_the_primary_returns_nothing():
    coords, used = geocode_with_fallback(
        "9313 West 91st Place",
        lambda _a: None,
        lambda _a: (39.86278, -105.10381),
    )
    assert coords == (39.86278, -105.10381)
    assert used is True


def test_both_failing_reports_the_fallback_was_tried():
    """used_fallback is about which path ran, not whether it worked -- a run
    needs to distinguish 'Nominatim is fine' from 'both geocoders failed'."""
    coords, used = geocode_with_fallback("x", lambda _a: None, lambda _a: None)
    assert coords is None
    assert used is True
