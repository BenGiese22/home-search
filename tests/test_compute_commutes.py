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


def test_a_corpus_where_nothing_geocodes_is_a_failed_run():
    """One address the geocoder does not know is a fact about that address.
    Every address failing is a fact about us -- and exiting 0 would hand the
    scorer a corpus with no commutes in it, looking like a good run."""
    code, upserts, _ = run_stage(
        [listing("a"), listing("b")], geocode_fn=lambda parts: None
    )
    assert code == 4
    assert len(upserts) == 2


def test_a_route_that_raises_still_writes_a_row_naming_the_failure():
    """A transport error mid-listing used to `continue`, leaving no row at
    all. The listing then scored on the neutral fallback with nothing in the
    data saying why."""

    def route_fn(origin, destination):
        raise RuntimeError("connection reset")

    code, upserts, _ = run_stage([listing("a")], route_fn=route_fn)
    assert code == 4
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

    code, _, _ = run_stage([listing("a"), listing("b")], route_fn=route_fn)
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
    attempts = {"n": 0}

    def route_fn(origin, destination):
        attempts["n"] += 1
        raise RetryableStatus(429, retry_after=1.0)

    code, upserts, _ = run_stage([listing("a")], route_fn=route_fn)
    assert code == 4
    assert attempts["n"] <= compute_commutes.MAX_RETRIES + 1
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
