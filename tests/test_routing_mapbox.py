"""The Mapbox adapter is the only module that knows a provider exists.

Everything here is pure: `http_get` is injected, so no test touches the
network. The response shapes are copied from real Mapbox responses captured
by ops/spikes/mapbox_preflight.py on 2026-09-05.
"""

import pytest

from src.routing_mapbox import (
    AddressParts,
    RoutingError,
    geocode_address,
    route,
)

TOKEN = "sk.this-is-a-secret-token"

ADDRESS = AddressParts(
    address="8221 West 93rd Way", city="Westminster", state="CO", zip_code="80021"
)

# Trimmed to the fields the adapter reads, with the nesting preserved --
# `accuracy` lives inside `coordinates`, which is the detail that made the
# first draft of the pre-flight report the whole corpus as unresolvable.
GEOCODE_OK = {
    "features": [
        {
            "properties": {
                "feature_type": "address",
                "full_address": "8221 West 93rd Way, Westminster, Colorado 80021",
                "coordinates": {
                    "longitude": -105.089505,
                    "latitude": 39.865555,
                    "accuracy": "rooftop",
                },
                "match_code": {"confidence": "exact"},
            }
        }
    ]
}

ROUTE_OK = {
    "code": "Ok",
    "routes": [{"distance": 12070.5, "duration": 852.0}],
}


def responder(payload):
    """An http_get that returns one payload and records the URL it was given."""
    calls = []

    def http_get(url):
        calls.append(url)
        return payload

    http_get.calls = calls
    return http_get


# --- geocoding ----------------------------------------------------------


def test_geocode_returns_coordinates_and_accuracy():
    http_get = responder(GEOCODE_OK)
    assert geocode_address(ADDRESS, TOKEN, http_get) == (
        39.865555,
        -105.089505,
        "rooftop",
    )


def test_geocode_sends_the_address_as_structured_parameters():
    """A single free-text query leaves Mapbox guessing which token is the
    city. Every part goes in its own parameter."""
    http_get = responder(GEOCODE_OK)
    geocode_address(ADDRESS, TOKEN, http_get)
    url = http_get.calls[0]
    assert "address_line1=8221%20West%2093rd%20Way" in url
    assert "place=Westminster" in url
    assert "region=CO" in url
    assert "postcode=80021" in url
    assert "country=US" in url


def test_geocode_rejects_a_match_that_is_not_the_building():
    """A city-centroid match would become a plausible-looking commute, which
    is worse than no commute: nothing downstream could tell it was wrong."""
    payload = {
        "features": [
            {
                "properties": {
                    "coordinates": {
                        "latitude": 39.8,
                        "longitude": -105.0,
                        "accuracy": "interpolated",
                    }
                }
            }
        ]
    }
    assert geocode_address(ADDRESS, TOKEN, responder(payload)) is None


def test_geocode_accepts_parcel_and_point_as_well_as_rooftop():
    for accuracy in ("rooftop", "parcel", "point"):
        payload = {
            "features": [
                {
                    "properties": {
                        "coordinates": {
                            "latitude": 39.8,
                            "longitude": -105.0,
                            "accuracy": accuracy,
                        }
                    }
                }
            ]
        }
        assert geocode_address(ADDRESS, TOKEN, responder(payload)) is not None


def test_geocode_returns_none_when_there_are_no_features():
    assert geocode_address(ADDRESS, TOKEN, responder({"features": []})) is None


def test_geocode_returns_none_on_a_malformed_response():
    for payload in ({}, {"features": [{}]}, {"features": [{"properties": {}}]}):
        assert geocode_address(ADDRESS, TOKEN, responder(payload)) is None


def test_geocode_returns_none_when_accuracy_is_missing():
    """No accuracy field means the adapter cannot tell a rooftop from a
    centroid, so it must not treat the result as a building."""
    payload = {
        "features": [
            {"properties": {"coordinates": {"latitude": 39.8, "longitude": -105.0}}}
        ]
    }
    assert geocode_address(ADDRESS, TOKEN, responder(payload)) is None


# --- routing ------------------------------------------------------------


def test_route_returns_miles_and_minutes():
    miles, minutes = route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, responder(ROUTE_OK))
    assert miles == pytest.approx(12070.5 / 1609.34)
    assert minutes == pytest.approx(852.0 / 60.0)


def test_route_puts_the_coordinates_in_lon_lat_order():
    """Mapbox takes lon,lat; every coordinate elsewhere in this codebase is
    lat,lon. Swapping them routes between two points in the ocean and still
    returns HTTP 200."""
    http_get = responder(ROUTE_OK)
    route((39.86, -105.08), (39.96, -105.09), "2026-09-09T08:15", TOKEN, http_get)
    assert "/-105.08,39.86;-105.09,39.96?" in http_get.calls[0]


def test_route_asks_for_the_arrival_time():
    http_get = responder(ROUTE_OK)
    route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, http_get)
    assert "arrive_by=2026-09-09T08%3A15" in http_get.calls[0]


def test_route_uses_the_driving_profile_not_driving_traffic():
    """arrive_by is only supported on `driving`. driving-traffic answers for
    right now, which for a 6-hourly cron means the number depends on when the
    run happened."""
    http_get = responder(ROUTE_OK)
    route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, http_get)
    assert "/mapbox/driving/" in http_get.calls[0]
    assert "driving-traffic" not in http_get.calls[0]


def test_route_does_not_ask_for_geometry():
    http_get = responder(ROUTE_OK)
    route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, http_get)
    assert "overview=false" in http_get.calls[0]


def test_route_returns_none_when_mapbox_reports_a_non_ok_code():
    payload = {"code": "NoRoute", "routes": []}
    assert route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, responder(payload)) is None


def test_route_returns_none_when_there_are_no_routes():
    assert route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, responder({"code": "Ok", "routes": []})) is None


def test_route_returns_none_on_a_malformed_route():
    payload = {"code": "Ok", "routes": [{"distance": "far"}]}
    assert route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, responder(payload)) is None


# --- the token ----------------------------------------------------------


def test_an_error_from_the_transport_never_carries_the_token():
    """requests puts the full URL in an HTTPError's message, and the token is
    in the URL -- so an unwrapped 401 writes the credential straight into the
    pipeline log, which is uploaded and kept."""

    def http_get(url):
        raise RuntimeError(f"401 Client Error for url: {url}")

    with pytest.raises(RoutingError) as excinfo:
        route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, http_get)
    assert TOKEN not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


def test_a_geocode_error_from_the_transport_never_carries_the_token():
    def http_get(url):
        raise RuntimeError(f"429 Too Many Requests for url: {url}")

    with pytest.raises(RoutingError) as excinfo:
        geocode_address(ADDRESS, TOKEN, http_get)
    assert TOKEN not in str(excinfo.value)


def test_the_redacted_error_keeps_the_part_that_identifies_the_problem():
    """Redaction that swallows the status code would trade one silent failure
    for another."""

    def http_get(url):
        raise RuntimeError(f"401 Client Error for url: {url}")

    with pytest.raises(RoutingError, match="401"):
        route((39.86, -105.08), (39.96, -105.08), "2026-09-09T08:15", TOKEN, http_get)


def test_the_original_exception_is_kept_as_the_cause():
    original = RuntimeError("boom")

    def http_get(url):
        raise original

    with pytest.raises(RoutingError) as excinfo:
        geocode_address(ADDRESS, TOKEN, http_get)
    assert excinfo.value.__cause__ is original
