"""The commutes stage across several runs: the selector and run() together.

The stage's exit code is a judgment about one run, but the selector decides
what that run attempts, and it re-picks every failed row on every run with
no cap. Whether "nothing routed" means an outage therefore depends on what
earlier runs left behind -- which only shows up when the two are exercised
together against a real database, run after run.
"""

import sqlite3

import pytest

import compute_commutes
from compute_commutes import EXIT_NOTHING_ROUTED, measure
from src.db import get_commute, init_db
from src.routing_mapbox import RoutingError

ARRIVE = "2026-09-09T08:15"
BAD = "9233 North Lamar Street"
GOOD = "8221 West 93rd Way"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


def add(conn, lid, address):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, property_type, localized_status
           ) VALUES (?, ?, 'Westminster', 'CO', '80021', '$625,000', 3, 2.5,
                     2140, 7000, 2, 1990, 'd', 'https://x/l', 'Single Family', 'Active')""",
        (lid, address),
    )
    conn.commit()


def coordinate(address):
    return (39.0 + len(address) / 1000, -105.0)


def geocoder(bad_addresses=()):
    """Every address gets its own coordinate, so a route fake can pick one
    out; the ones in `bad_addresses` do not geocode at all."""

    def geocode_fn(parts):
        if parts.address in bad_addresses:
            return None
        return (*coordinate(parts.address), "rooftop")

    return geocode_fn


def good_route(origin, destination):
    return (9.0, 22.0)


def run_once(conn, *, geocode_fn=None, route_fn=good_route):
    return measure(
        conn,
        geocode_fn=geocode_fn or geocoder(),
        route_fn=route_fn,
        arrive_by=ARRIVE,
        sleep=lambda seconds: None,
    )


def runs(conn, n, **kwargs):
    return [run_once(conn, **kwargs) for _ in range(n)]


@pytest.fixture
def routed_before(conn):
    """A corpus with one listing that has routed -- the canary."""
    add(conn, "good", GOOD)
    assert run_once(conn) == 0
    assert get_commute(conn, "good")["medtronic_minutes"] == 22.0
    return conn


def test_the_single_bad_address_incident_is_not_a_failed_run(routed_before):
    add(routed_before, "lamar", BAD)
    assert runs(routed_before, 3, geocode_fn=geocoder({BAD})) == [0, 0, 0]
    assert get_commute(routed_before, "lamar")["geocode_failed"] == 1


def test_permanently_bad_addresses_never_fail_a_quiet_run(routed_before):
    """The bug #106 moved rather than fixed: once FLOOR un-geocodable
    addresses exist, every run with nothing new attempts exactly those,
    routes 0, and -- under a plain attempted-count floor -- fails the
    pipeline four times a day, forever."""
    floor = compute_commutes.NOTHING_ROUTED_FLOOR
    bad = {f"{i} {BAD}" for i in range(floor)}
    for i, address in enumerate(sorted(bad)):
        add(routed_before, f"bad{i}", address)
        assert run_once(routed_before, geocode_fn=geocoder(bad)) == 0

    # A string of runs with nothing new: the selector re-picks all FLOOR.
    assert runs(routed_before, 4, geocode_fn=geocoder(bad)) == [0, 0, 0, 0]


def test_a_deterministic_422_on_one_listing_is_not_a_failed_run(routed_before):
    """An odd coordinate Mapbox rejects with 422 raises the same exception on
    every attempt. It is the only listing a quiet run attempts, and the
    canary, routing fine, says it is that coordinate and not the service."""
    add(routed_before, "odd", "1 Odd Coordinate Lane")
    odd_origin = coordinate("1 Odd Coordinate Lane")

    def route_fn(origin, destination):
        if origin == odd_origin:
            raise RoutingError("422 Client Error: Unprocessable Entity")
        return (9.0, 22.0)

    assert runs(routed_before, 3, route_fn=route_fn) == [0, 0, 0]
    assert "422" in get_commute(routed_before, "odd")["route_error"]


def test_a_geocode_regression_fails_every_run_not_just_the_first(routed_before):
    """A parse regression answers "no match" for every address. The failing
    run writes geocode_failed=1 for all five before it returns; any rule
    that trusts those rows next run goes green on run two."""
    for i in range(5):
        add(routed_before, f"new{i}", f"{i} New Street")

    everything = lambda parts: None  # noqa: E731
    assert runs(routed_before, 3, geocode_fn=everything) == [
        EXIT_NOTHING_ROUTED
    ] * 3


def test_a_persistent_503_fails_every_run_and_recovers(routed_before):
    add(routed_before, "a", "12 Outage Avenue")

    def route_fn(origin, destination):
        raise RoutingError("503 Server Error: Service Unavailable")

    assert runs(routed_before, 3, route_fn=route_fn) == [EXIT_NOTHING_ROUTED] * 3
    assert "503" in get_commute(routed_before, "a")["route_error"]

    assert run_once(routed_before) == 0
    assert get_commute(routed_before, "a")["medtronic_minutes"] == 22.0


def test_the_canary_row_is_never_rewritten(routed_before):
    before = dict(get_commute(routed_before, "good"))
    add(routed_before, "lamar", BAD)
    assert run_once(routed_before, geocode_fn=geocoder({BAD})) == 0

    def route_fn(origin, destination):
        raise RoutingError("503 Server Error: Service Unavailable")

    assert run_once(routed_before, route_fn=route_fn) == EXIT_NOTHING_ROUTED
    assert dict(get_commute(routed_before, "good")) == before


def test_a_bad_address_that_gets_fixed_is_measured(routed_before):
    """Failed rows are still retried every run: the listing agent may
    correct the address, and the next run should pick that up."""
    add(routed_before, "a", BAD)
    assert run_once(routed_before, geocode_fn=geocoder({BAD})) == 0
    assert run_once(routed_before) == 0
    assert get_commute(routed_before, "a")["medtronic_minutes"] == 22.0


def test_a_stale_row_is_not_a_canary(conn):
    """A row measured a different way is outstanding work, not evidence the
    current measurement works."""
    add(conn, "old", GOOD)
    assert run_once(conn) == 0
    conn.execute("UPDATE commute SET commute_source = 'something-older'")
    conn.commit()
    assert compute_commutes.pick_canary(conn) is None


def test_a_fresh_corpus_that_has_never_routed_falls_back_to_the_floor(conn):
    floor = compute_commutes.NOTHING_ROUTED_FLOOR
    bad = {f"{i} {BAD}" for i in range(floor)}
    ordered = sorted(bad)
    for i in range(floor - 1):
        add(conn, f"bad{i}", ordered[i])
    assert run_once(conn, geocode_fn=geocoder(bad)) == 0

    add(conn, f"bad{floor - 1}", ordered[floor - 1])
    assert run_once(conn, geocode_fn=geocoder(bad)) == EXIT_NOTHING_ROUTED
