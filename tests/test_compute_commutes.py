"""The commutes stage: pacing, retries, and what a failure is allowed to do.

The stage is the only layer that can tell a 401 (the token is dead — stop,
nothing here will work) from a 429 (wait and try again) from a route that
simply does not exist (record it and move on). Everything below it returns
None or raises, so every one of those decisions is made here and is worth a
test each.
"""

import pytest

import compute_commutes
from compute_commutes import RATE_LIMIT_MAX_SLEEP, RetryableStatus, StopTheRun, run
from src.commute import COMMUTE_SOURCE

ARRIVE = "2026-09-09T08:15"


class FakeConn:
    """Records upserts instead of writing. Nothing here opens a database."""

    def __init__(self):
        self.rows = {}


def listing(listing_id, address="8221 West 93rd Way"):
    return {
        "listing_id": listing_id,
        "address": address,
        "city": "Westminster",
        "state": "CO",
        "zip_code": "80021",
    }


def run_stage(
    listings,
    *,
    geocode_fn=None,
    route_fn=None,
    sleeps=None,
    upserts=None,
    canaries=(),
):
    conn = FakeConn()
    recorded = [] if upserts is None else upserts
    slept = [] if sleeps is None else sleeps
    return (
        run(
            conn,
            listings,
            geocode_fn=geocode_fn or (lambda parts: (39.86, -105.08, "rooftop")),
            route_fn=route_fn or (lambda o, d: (9.0, 22.0)),
            arrive_by=ARRIVE,
            sleep=slept.append,
            upsert_fn=lambda c, lid, result: recorded.append((lid, result)),
            canaries=canaries,
        ),
        recorded,
        slept,
    )


# --- the happy path -----------------------------------------------------


def test_every_listing_ends_in_a_row():
    code, upserts, _ = run_stage([listing("a"), listing("b")])
    assert code == 0
    assert [lid for lid, _ in upserts] == ["a", "b"]
    assert all(r.commute_source == COMMUTE_SOURCE for _, r in upserts)
    assert all(r.arrive_by == ARRIVE for _, r in upserts)


def test_the_address_reaches_the_geocoder_as_structured_parts():
    seen = []
    run_stage([listing("a")], geocode_fn=lambda parts: seen.append(parts) or (1.0, 2.0, "rooftop"))
    parts = seen[0]
    assert (parts.address, parts.city, parts.state, parts.zip_code) == (
        "8221 West 93rd Way",
        "Westminster",
        "CO",
        "80021",
    )


def test_requests_are_paced():
    """Not politeness for its own sake: a burst from a datacenter IP is what
    a rate limiter is built to notice, and the limit measured on this token
    is 300/minute against three requests per listing."""
    _, _, slept = run_stage([listing("a"), listing("b")])
    assert slept
    assert all(s > 0 for s in slept)


# --- failure that is not the stage's fault ------------------------------


def test_a_geocode_miss_still_writes_a_row():
    """Skipping the write is what made failures permanent before: a listing
    with no row is selected again next run, which is right, but a listing
    that genuinely cannot be geocoded is then retried forever at a request
    each. The row is the record that we asked."""
    misses = [listing("a"), listing("b")]

    def geocode_fn(parts):
        return None if parts.address == misses[0]["address"] else (1.0, 2.0, "rooftop")

    misses[1]["address"] = "somewhere else"
    code, upserts, _ = run_stage(misses, geocode_fn=geocode_fn)
    assert code == 0
    assert upserts[0][1].geocode_failed is True
    assert upserts[1][1].geocode_failed is False


# A listing that has routed before, standing by as the canary. Its address is
# its own so the fakes below can fail everything *except* it.
CANARY_ADDRESS = "4012 Canary Court"


def canary():
    return listing("canary", address=CANARY_ADDRESS)


def geocode_all_but_canary_fails(parts):
    return (39.9, -105.1, "rooftop") if parts.address == CANARY_ADDRESS else None


# --- a run that routes nothing, with a canary ---------------------------


def test_one_listing_that_will_not_geocode_is_not_a_failed_run(capsys):
    """2026-09-19: a run needed to measure exactly one listing, whose address
    (9233 North Lamar Street) would not geocode. 0/1 routed tripped
    EXIT_NOTHING_ROUTED and failed the entire pipeline -- indistinguishable
    from a real Mapbox outage. The canary tells them apart: it routes, so
    the service works and the failure is a fact about that address."""
    code, upserts, _ = run_stage(
        [listing("a", address="9233 North Lamar Street")],
        geocode_fn=geocode_all_but_canary_fails,
        canaries=[canary()],
    )
    assert code == 0
    assert len(upserts) == 1
    assert upserts[0][1].geocode_failed is True
    lines = capsys.readouterr().out.splitlines()
    assert "a (9233 North Lamar Street, Westminster): no coordinates" in lines
    assert (
        "commutes: nothing routed, but canary canary routed fine; not systemic "
        "-- the failed row(s) are recorded and will be retried next run: a"
    ) in lines


def test_the_canary_is_measured_but_never_written():
    """The canary's row is a known-good measurement from an earlier run.
    Writing the probe would churn it on every quiet run -- and, the day the
    probe fails, overwrite a good commute with an empty one."""
    code, upserts, _ = run_stage(
        [listing("a", address="9233 North Lamar Street")],
        geocode_fn=geocode_all_but_canary_fails,
        canaries=[canary()],
    )
    assert code == 0
    assert [lid for lid, _ in upserts] == ["a"]


def test_a_canary_that_fails_too_fails_the_run(capsys):
    """Any failure counts, a no-match included: a geocode-parse regression
    answers "no match" for every address, the canary's among them."""
    code, upserts, _ = run_stage(
        [listing("a")], geocode_fn=lambda parts: None, canaries=[canary()]
    )
    assert code == compute_commutes.EXIT_NOTHING_ROUTED
    assert [lid for lid, _ in upserts] == ["a"]
    lines = capsys.readouterr().out.splitlines()
    assert "commutes: canary canary failed (no coordinates)" in lines
    assert (
        "commutes: nothing routed, and every canary failed too (canary) "
        "-- treating the run as failed"
    ) in lines


def test_the_canary_is_not_probed_when_something_routed():
    geocoded = []

    def geocode_fn(parts):
        geocoded.append(parts.address)
        return (39.86, -105.08, "rooftop")

    code, _, _ = run_stage([listing("a")], geocode_fn=geocode_fn, canaries=[canary()])
    assert code == 0
    assert CANARY_ADDRESS not in geocoded


def test_a_dead_token_during_the_canary_still_stops_the_run():
    def geocode_fn(parts):
        if parts.address == CANARY_ADDRESS:
            raise StopTheRun("HTTP 401")
        return None

    with pytest.raises(StopTheRun):
        run_stage([listing("a")], geocode_fn=geocode_fn, canaries=[canary()])


# --- more than one canary -----------------------------------------------

BROKEN_CANARY_ADDRESS = "1 Broken Canary Row"


def test_a_bad_first_canary_is_rescued_by_a_good_second(capsys):
    """One canary whose own address has stopped routing (a Mapbox data
    change, a single 5xx blip) must not fail every quiet run by itself.
    The next canary routes, so the service works."""
    geocoded = []

    def geocode_fn(parts):
        geocoded.append(parts.address)
        return geocode_all_but_canary_fails(parts)

    third = listing("third", address="3 Unused Canary Way")
    code, upserts, _ = run_stage(
        [listing("a", address="9233 North Lamar Street")],
        geocode_fn=geocode_fn,
        canaries=[listing("broken", address=BROKEN_CANARY_ADDRESS), canary(), third],
    )
    assert code == 0
    assert [lid for lid, _ in upserts] == ["a"]
    # Stops at the first canary that routes.
    assert "3 Unused Canary Way" not in geocoded
    lines = capsys.readouterr().out.splitlines()
    assert "commutes: canary broken failed (no coordinates)" in lines
    assert (
        "commutes: nothing routed, but canary canary routed fine; not systemic "
        "-- the failed row(s) are recorded and will be retried next run: a"
    ) in lines


def test_every_canary_failing_fails_the_run(capsys):
    canaries = [listing(f"c{i}", address=f"{i} Canary Court") for i in range(3)]
    code, upserts, _ = run_stage(
        [listing("a")], geocode_fn=lambda parts: None, canaries=canaries
    )
    assert code == compute_commutes.EXIT_NOTHING_ROUTED
    assert [lid for lid, _ in upserts] == ["a"]
    assert (
        "commutes: nothing routed, and every canary failed too (c0, c1, c2) "
        "-- treating the run as failed"
    ) in capsys.readouterr().out.splitlines()


def test_a_dead_token_on_a_later_canary_still_stops_the_run():
    def geocode_fn(parts):
        if parts.address == CANARY_ADDRESS:
            raise StopTheRun("HTTP 401")
        return None

    with pytest.raises(StopTheRun):
        run_stage(
            [listing("a")],
            geocode_fn=geocode_fn,
            canaries=[listing("broken", address=BROKEN_CANARY_ADDRESS), canary()],
        )


# --- a run that routes nothing, on a corpus that has never routed -------


def test_with_no_canary_one_bad_address_is_not_a_failed_run():
    """No listing has ever routed, so there is nothing to probe with: fall
    back to counting this run's attempts against the floor."""
    code, upserts, _ = run_stage(
        [listing("a", address="9233 North Lamar Street")],
        geocode_fn=lambda parts: None,
    )
    assert code == 0
    assert upserts[0][1].geocode_failed is True


def test_with_no_canary_just_under_the_floor_is_still_not_a_failed_run():
    """Every row still lands -- that is what lets the selector retry each
    one next run instead of the whole corpus being read as a Mapbox outage."""
    listings = [
        listing(str(i)) for i in range(compute_commutes.NOTHING_ROUTED_FLOOR - 1)
    ]
    code, upserts, _ = run_stage(listings, geocode_fn=lambda parts: None)
    assert code == 0
    assert len(upserts) == compute_commutes.NOTHING_ROUTED_FLOOR - 1
    assert all(result.geocode_failed is True for _, result in upserts)


def test_with_no_canary_at_the_floor_is_a_failed_run():
    """FLOOR addresses all failing, with nothing succeeding and nothing
    known-good to compare against, is a fact about us -- and exiting 0 would
    hand the scorer a corpus with no commutes in it, looking like a good
    run."""
    listings = [listing(str(i)) for i in range(compute_commutes.NOTHING_ROUTED_FLOOR)]
    code, upserts, _ = run_stage(listings, geocode_fn=lambda parts: None)
    assert code == compute_commutes.EXIT_NOTHING_ROUTED
    assert len(upserts) == compute_commutes.NOTHING_ROUTED_FLOOR


def test_with_no_canary_under_the_floor_says_so_in_the_log(capsys):
    """The line a human reads in a sandbox log has to say "below the floor,
    will retry" -- not just print nothing and exit 0 -- so "guard removed"
    and "guard below floor" don't look identical from the outside."""
    code, _, _ = run_stage([listing("a")], geocode_fn=lambda parts: None)
    assert code == 0
    floor = compute_commutes.NOTHING_ROUTED_FLOOR
    assert (
        f"commutes: nothing routed, no canary to probe with, "
        f"and only 1 attempted (floor for calling that systemic is {floor}); "
        f"the row(s) are recorded and will be retried next run"
    ) in capsys.readouterr().out.splitlines()


# --- the service failing to answer --------------------------------------


def test_a_route_that_raises_still_writes_a_row_naming_the_failure():
    """A transport error mid-listing used to `continue`, leaving no row at
    all. The listing then scored on the neutral fallback with nothing in the
    data saying why.

    And at one listing it fails the run: the canary hits the same error."""

    def route_fn(origin, destination):
        raise RuntimeError("connection reset")

    code, upserts, _ = run_stage([listing("a")], route_fn=route_fn, canaries=[canary()])
    assert code == compute_commutes.EXIT_NOTHING_ROUTED
    assert len(upserts) == 1
    assert upserts[0][1].medtronic_minutes is None
    assert "connection reset" in upserts[0][1].route_error


def test_one_failure_among_many_is_not_a_failed_run():
    def route_fn(origin, destination):
        raise RuntimeError("blip")

    calls = {"n": 0}

    def flaky(origin, destination):
        calls["n"] += 1
        if calls["n"] == 1:
            return route_fn(origin, destination)
        return (9.0, 22.0)

    code, upserts, _ = run_stage([listing("a"), listing("b")], route_fn=flaky)
    assert code == 0
    assert len(upserts) == 2


def test_every_listing_failing_is_a_failed_run():
    """Exit 0 with an empty corpus is the failure mode this whole project
    keeps producing: HTTP 200, valid JSON, plausible row counts, wrong
    answer. If nothing routed, something is wrong with us, not with the
    roads."""

    def route_fn(origin, destination):
        raise RuntimeError("boom")

    listings = [listing(str(i)) for i in range(compute_commutes.NOTHING_ROUTED_FLOOR)]
    code, _, _ = run_stage(listings, route_fn=route_fn)
    assert code == 4


def test_no_listings_to_do_is_not_a_failure():
    code, upserts, _ = run_stage([])
    assert code == 0
    assert upserts == []


# --- rate limiting ------------------------------------------------------


def test_a_rate_limit_is_waited_out_and_retried():
    calls = {"n": 0}

    def route_fn(origin, destination):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableStatus(429, retry_after=5.0)
        return (9.0, 22.0)

    code, upserts, slept = run_stage([listing("a")], route_fn=route_fn)
    assert code == 0
    assert 5.0 in slept
    assert upserts[0][1].medtronic_minutes == 22.0


def test_the_rate_limit_wait_is_capped():
    """A provider that answers Retry-After in the thousands would otherwise
    hold the pipeline lease open until the reaper kills the sandbox."""

    def route_fn(origin, destination):
        raise RetryableStatus(429, retry_after=99999.0)

    _, _, slept = run_stage([listing("a")], route_fn=route_fn)
    assert slept
    assert max(slept) <= RATE_LIMIT_MAX_SLEEP


def test_a_rate_limit_gives_up_after_a_few_tries():
    """A quota that is still exhausted after the retries is exhausted for
    the canary too, so a one-listing run fails."""
    attempts = {"n": 0}

    def route_fn(origin, destination):
        attempts["n"] += 1
        raise RetryableStatus(429, retry_after=1.0)

    code, upserts, _ = run_stage([listing("a")], route_fn=route_fn, canaries=[canary()])
    assert code == compute_commutes.EXIT_NOTHING_ROUTED
    # The listing and then the canary, each through the full retry budget.
    assert attempts["n"] == 2 * (compute_commutes.MAX_RETRIES + 1)
    assert "429" in upserts[0][1].route_error


# --- failure that means "stop" ------------------------------------------


def test_a_dead_token_aborts_before_the_second_listing():
    """Continuing past a 401 would spend a request per listing to write a
    corpus of empty commutes, and then exit 4 -- by which point the rows are
    already overwritten."""
    seen = []

    def route_fn(origin, destination):
        seen.append(destination)
        raise StopTheRun("401 unauthorized")

    with pytest.raises(StopTheRun):
        run_stage([listing("a"), listing("b")], route_fn=route_fn)
    assert len(seen) == 1


def test_a_dead_token_during_geocoding_also_aborts():
    def geocode_fn(parts):
        raise StopTheRun("403 forbidden")

    with pytest.raises(StopTheRun):
        run_stage([listing("a"), listing("b")], geocode_fn=geocode_fn)


# --- through the real adapter -------------------------------------------
#
# The tests above hand run() fakes that raise StopTheRun / RetryableStatus
# directly. In production those come from mapbox_get *inside* the adapter,
# whose _fetch wraps every exception in a token-scrubbed RoutingError. These
# go through the adapter, because that wrapping is exactly what the fakes
# skipped.

GEOCODE_OK = {
    "features": [
        {
            "properties": {
                "coordinates": {
                    "latitude": 39.86,
                    "longitude": -105.08,
                    "accuracy": "rooftop",
                }
            }
        }
    ]
}


def real_geocoder(http_get):
    from src.routing_mapbox import geocode_address

    return lambda parts: geocode_address(parts, "sk.token", http_get)


def test_a_401_from_inside_the_adapter_still_stops_the_run():
    """Without unwrapping, a dead token arrives as RoutingError, is recorded
    as a route error on every listing, and never reaches EXIT_AUTH_FAILED."""
    seen = []

    def http_get(url):
        seen.append(url)
        raise StopTheRun("HTTP 401: the Mapbox token was rejected")

    with pytest.raises(StopTheRun):
        run_stage([listing("a"), listing("b")], geocode_fn=real_geocoder(http_get))
    assert len(seen) == 1


def test_a_429_from_inside_the_adapter_is_waited_out_and_retried():
    calls = {"n": 0}

    def http_get(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableStatus(429, retry_after=5.0)
        return GEOCODE_OK

    code, upserts, slept = run_stage([listing("a")], geocode_fn=real_geocoder(http_get))
    assert code == 0
    assert 5.0 in slept
    assert upserts[0][1].medtronic_minutes == 22.0


# --- server errors ------------------------------------------------------


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        raise AssertionError("mapbox_get should have classified this status")

    def json(self):
        return {}


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_error_is_classified_as_retryable(monkeypatch, status):
    monkeypatch.setattr(
        compute_commutes.requests, "get", lambda url, timeout: FakeResponse(status)
    )
    with pytest.raises(RetryableStatus) as excinfo:
        compute_commutes.mapbox_get("https://api.mapbox.com/x")
    assert excinfo.value.status == status
    assert excinfo.value.retry_after == compute_commutes.SERVER_ERROR_WAIT


def test_a_single_503_is_retried_rather_than_failing_the_listing():
    """One transient 503 on a one-listing run used to fail the listing
    outright -- and, with nothing else routed, the run."""
    calls = {"n": 0}

    def route_fn(origin, destination):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableStatus(503, retry_after=compute_commutes.SERVER_ERROR_WAIT)
        return (9.0, 22.0)

    code, upserts, slept = run_stage([listing("a")], route_fn=route_fn)
    assert code == 0
    assert compute_commutes.SERVER_ERROR_WAIT in slept
    assert upserts[0][1].medtronic_minutes == 22.0


def test_a_persistent_503_gives_up_sooner_than_a_rate_limit():
    """A 429 says when to come back; a 503 does not, and in a real outage
    every listing would sit through the full rate-limit budget."""
    attempts = {"n": 0}

    def route_fn(origin, destination):
        attempts["n"] += 1
        raise RetryableStatus(503, retry_after=compute_commutes.SERVER_ERROR_WAIT)

    _, upserts, _ = run_stage([listing("a")], route_fn=route_fn)
    assert attempts["n"] == compute_commutes.SERVER_ERROR_RETRIES + 1
    assert "503" in upserts[0][1].route_error


# --- the token ----------------------------------------------------------


def test_the_stage_never_prints_the_token(capsys):
    """The stage log is captured to a file and uploaded to Blob. Anything
    printed here is stored."""
    token = "sk.a-very-secret-value"
    compute_commutes.set_redaction_token(token)
    try:

        def route_fn(origin, destination):
            raise RuntimeError(f"boom for url ...access_token={token}")

        run_stage([listing("a")], route_fn=route_fn)
        captured = capsys.readouterr()
        assert token not in captured.out
        assert token not in captured.err
    finally:
        compute_commutes.set_redaction_token(None)


def test_the_adapter_redacts_even_when_the_stage_has_not_been_configured():
    """The stage's redact() needs to be told the token, so it is only as good
    as main() remembering to call set_redaction_token. The adapter does not
    depend on that: it builds the URL, so it always knows. Belt and braces,
    because a token in a log is not recoverable once the log is uploaded."""
    from src.routing_mapbox import AddressParts, RoutingError, geocode_address

    token = "sk.a-very-secret-value"
    compute_commutes.set_redaction_token(None)

    def http_get(url):
        raise RuntimeError(f"401 Client Error for url: {url}")

    with pytest.raises(RoutingError) as excinfo:
        geocode_address(
            AddressParts(address="a", city="b", state="CO", zip_code="80021"),
            token,
            http_get,
        )
    assert token not in str(excinfo.value)


def test_main_configures_redaction_before_it_can_fail(monkeypatch):
    """set_redaction_token has to happen before the Turso connection, not
    after: stage_connection() can raise, and its message would otherwise be
    the first unredacted thing printed."""
    order = []
    token = "sk.a-very-secret-value"

    monkeypatch.setattr(compute_commutes, "load_env", lambda: {"MAPBOX_ACCESS_TOKEN": token})
    monkeypatch.setattr(
        compute_commutes,
        "set_redaction_token",
        lambda value: order.append(("redact", value)),
    )

    def boom():
        order.append(("connect", None))
        raise RuntimeError("turso is down")

    monkeypatch.setattr(compute_commutes, "stage_connection", boom)

    with pytest.raises(RuntimeError, match="turso is down"):
        compute_commutes.main()
    assert order == [("redact", token), ("connect", None)]


def test_main_exits_naming_the_variable_when_the_token_is_absent(capsys):
    import compute_commutes as module

    original = module.load_env
    module.load_env = lambda: {}
    try:
        assert module.main() == module.EXIT_NO_TOKEN
    finally:
        module.load_env = original
    assert "MAPBOX_ACCESS_TOKEN" in capsys.readouterr().err


def test_a_leaked_token_is_scrubbed_from_the_stored_route_error():
    """route_error is written to Turso and read back by verify. A raw
    exception message would put the credential in the database."""
    token = "sk.a-very-secret-value"
    compute_commutes.set_redaction_token(token)
    try:

        def route_fn(origin, destination):
            raise RuntimeError(f"boom access_token={token}")

        _, upserts, _ = run_stage([listing("a")], route_fn=route_fn)
        assert token not in upserts[0][1].route_error
    finally:
        compute_commutes.set_redaction_token(None)
