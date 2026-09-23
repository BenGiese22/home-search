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

ARRIVE = "2026-09-09T08:15"
BAD = "9233 North Lamar Street"


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


def geocoder(bad_addresses):
    def geocode_fn(parts):
        return None if parts.address in bad_addresses else (39.86, -105.08, "rooftop")

    return geocode_fn


def good_route(origin, destination):
    return (9.0, 22.0)


def run_once(conn, *, geocode_fn, route_fn=good_route):
    return measure(
        conn,
        geocode_fn=geocode_fn,
        route_fn=route_fn,
        arrive_by=ARRIVE,
        sleep=lambda seconds: None,
    )


def test_permanently_bad_addresses_never_fail_a_quiet_run(conn):
    """The bug PR #106 moved rather than fixed: once FLOOR un-geocodable
    addresses exist, every run with nothing new attempts exactly those,
    routes 0, and -- under a plain attempted-count floor -- fails the
    pipeline four times a day, forever."""
    floor = compute_commutes.NOTHING_ROUTED_FLOOR
    bad = {f"{i} {BAD}" for i in range(floor)}
    add(conn, "good", "8221 West 93rd Way")
    assert run_once(conn, geocode_fn=geocoder(bad)) == 0

    # They arrive one per run, the way listings actually do.
    for i, address in enumerate(sorted(bad)):
        add(conn, f"bad{i}", address)
        assert run_once(conn, geocode_fn=geocoder(bad)) == 0

    # Now a string of runs with nothing new: the selector re-picks all FLOOR.
    for _ in range(4):
        assert run_once(conn, geocode_fn=geocoder(bad)) == 0
    assert all(get_commute(conn, f"bad{i}")["geocode_failed"] == 1 for i in range(floor))


def test_a_known_bad_address_that_gets_fixed_is_measured(conn):
    """Known-bad is still retried, not skipped: the listing agent may correct
    the address, and the next run should pick that up."""
    add(conn, "a", BAD)
    assert run_once(conn, geocode_fn=geocoder({BAD})) == 0
    assert run_once(conn, geocode_fn=geocoder(set())) == 0
    assert get_commute(conn, "a")["medtronic_minutes"] == 22.0


def test_a_persistent_routing_outage_fails_every_run_not_just_the_first(conn):
    """One new listing, and routing answers 5xx on every attempt. The first
    run must fail -- and so must every run after it, when that listing is
    the only thing re-picked. A rule that let a listing's own failure
    history excuse a service error would go green on run two."""
    add(conn, "a", "8221 West 93rd Way")

    def route_fn(origin, destination):
        raise RuntimeError("HTTP 503 Service Unavailable")

    for _ in range(3):
        assert run_once(conn, geocode_fn=geocoder(set()), route_fn=route_fn) == EXIT_NOTHING_ROUTED
    assert "503" in get_commute(conn, "a")["route_error"]

    # And when the service comes back, so does the run.
    assert run_once(conn, geocode_fn=geocoder(set())) == 0
    assert get_commute(conn, "a")["medtronic_minutes"] == 22.0


def test_a_geocoder_outage_fails_even_when_only_known_bad_rows_are_retried(conn):
    """No new listings, only known-bad ones re-picked, and the geocoder is
    down. It never answered, so this is not "the same bad addresses again"."""
    add(conn, "a", BAD)
    assert run_once(conn, geocode_fn=geocoder({BAD})) == 0

    def geocode_fn(parts):
        raise RuntimeError("HTTP 500")

    assert run_once(conn, geocode_fn=geocode_fn) == EXIT_NOTHING_ROUTED
