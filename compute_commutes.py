"""Measure every outstanding listing's drive to Megan's office and to Denver.

    venv/bin/python compute_commutes.py [--only-new] [--force]

One geocode and two routes per listing, against Mapbox, asking for a typical
Wednesday-08:15 arrival rather than an empty road. What "outstanding" means
lives in `get_listing_ids_missing_commute`: no row, an unusable row, or a row
measured a different way. That last clause is what makes a change to how a
commute is computed migrate the corpus by itself on the next ordinary run.

**Every listing this stage attempts ends in a row**, including the ones that
fail. A listing with no row is selected again next run, which is right for a
blip and wrong for an address that will never geocode -- it would be retried
forever, a request at a time, with nothing in the data saying why. The row is
the record that we asked and what came back.

Three failure classes, and the stage is the only layer that can tell them
apart:

  401/403   the token is dead. Stop. Continuing spends a request per listing
            to overwrite the corpus with empty commutes.
  429       we are going too fast. Wait for the reset and retry.
            (502/503/504 also retry, once, after a short wait.)
  anything  record it in route_error and carry on; one bad address should
            else      not cost the other hundred their numbers.

The `anything else` class has one more rule, for a run that routes nothing
at all. The listings a quiet run attempts are the failed rows the selector
re-picks every run, so "nothing routed" alone cannot tell a handful of
permanently bad addresses from a Mapbox outage. The canary can: one listing
that has routed before under the current COMMUTE_SOURCE, measured again
through the same path and never written. It routes -- the service works,
the failures are facts about those addresses, exit 0. It fails too, in any
way -- the run fails. A corpus where nothing has ever routed has no canary,
and falls back to `NOTHING_ROUTED_FLOOR`.
"""

import sys
import time
from pathlib import Path

import requests

from src.commute import COMMUTE_SOURCE, CommuteResult, compute_commute, next_arrival
from src.config import load_env
from src.db import get_listing_ids_missing_commute, query_listings, upsert_commute
from src.routing_mapbox import AddressParts, RoutingError, geocode_address, route
from src.turso_db import stage_connection

from datetime import datetime

# Pinned rather than geocoded at run start. Two fewer requests per run, and
# it closes a silent-degradation path that was live for a year: the old code
# geocoded the POI name "Medtronic, Lafayette, CO" and, when that missed,
# fell back to the *city centroid* of Lafayette with only a print statement
# to say so. There are Medtronic sites in Louisville and Boulder for a POI
# search to drift to as well.
#
# Both from Mapbox Geocoding v6, rooftop/exact, 2026-09-05
# (ops/spikes/mapbox_preflight.py).
#
# 250 Medtronic Dr, Lafayette, CO 80026 -- Megan's office. This is the leg
# the rubric scores. Ben has no commute of his own.
MEDTRONIC_LAFAYETTE = (39.962369, -105.08848)
# 3201 Walnut St #107, Denver, CO 80205 -- the coworking space Ben uses
# occasionally. Display only: it is stored and shown, and does not enter the
# score. The column is still called denver_* because short-list reads it and
# renaming a column in libsql is a table rebuild.
DENVER_COWORKING = (39.765313, -104.978703)

# Three requests per listing against a measured limit of 300/minute, so this
# is nowhere near it. It is here because a burst from a datacenter IP is
# exactly the shape a rate limiter is built to notice.
PACE_SECONDS = 0.2

MAX_RETRIES = 3
# A provider answering Retry-After in the thousands would otherwise hold the
# pipeline lease open long enough for the reaper to kill the sandbox.
RATE_LIMIT_MAX_SLEEP = 60.0

# 502/503/504 are retried too, but on a shorter leash than a 429. A rate
# limit says when to come back; a gateway error does not, and in a real
# outage every listing would otherwise sit through the full rate-limit
# budget. One retry after a short wait is enough to ride out a blip.
SERVER_ERROR_STATUSES = (502, 503, 504)
SERVER_ERROR_RETRIES = 1
SERVER_ERROR_WAIT = 2.0

TIMEOUT_SECONDS = 30

# Exit codes. Distinct on purpose: the pipeline surfaces the number and
# "the token is dead" needs a different response from "nothing routed".
EXIT_NO_TOKEN = 2
EXIT_AUTH_FAILED = 3
EXIT_NOTHING_ROUTED = 4

# The fallback for a run that routes nothing when there is no canary --
# no listing has ever routed under the current COMMUTE_SOURCE, so on a fresh
# corpus or the first run after a source bump. Below this many attempts the
# run exits 0 and the rows are retried next run; at or above it, the run
# fails. Set from one incident: a run with exactly one listing to measure,
# whose address would not geocode, failed the entire pipeline over it.
#
# Once anything has routed, this is not consulted: the canary answers the
# question directly, where a count cannot. The selector re-picks failed
# rows on every run with no cap, so any count of this run's failures is a
# count of accumulated bad addresses as much as of anything systemic.
NOTHING_ROUTED_FLOOR = 3


class StopTheRun(RuntimeError):
    """Nothing else in this run will work. Raised for 401/403."""


class RetryableStatus(RuntimeError):
    """A rate limit or a gateway error. Carries how long to wait."""

    def __init__(self, status: int, retry_after: float):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


# The token, once, so a message on its way to a log or to Turso can be
# scrubbed. Module-level rather than threaded through every call site
# because the redaction has to reach places that never see the token --
# an exception raised by `requests` deep inside http_get, for instance.
_REDACTION_TOKEN: str | None = None


def set_redaction_token(token: str | None) -> None:
    global _REDACTION_TOKEN
    _REDACTION_TOKEN = token


def redact(text: str) -> str:
    """Remove the access token from a string bound for a log or the database.

    Mapbox takes the token as a query parameter -- it does not accept a
    header on these endpoints -- and `requests` puts the full URL into an
    HTTPError message. Unredacted, a single 401 writes the credential into
    the stage log, which is uploaded to Blob and kept, and into route_error,
    which is stored in Turso.
    """
    from urllib.parse import quote

    if not _REDACTION_TOKEN:
        return text
    for form in (_REDACTION_TOKEN, quote(_REDACTION_TOKEN, safe="")):
        text = text.replace(form, "<redacted>")
    return text


def mapbox_get(url: str) -> dict:
    """The transport. Turns HTTP status into the three classes above."""
    response = requests.get(url, timeout=TIMEOUT_SECONDS)
    if response.status_code in (401, 403):
        raise StopTheRun(f"HTTP {response.status_code}: the Mapbox token was rejected")
    if response.status_code == 429:
        raise RetryableStatus(429, retry_after=_retry_after(response))
    if response.status_code in SERVER_ERROR_STATUSES:
        raise RetryableStatus(response.status_code, retry_after=SERVER_ERROR_WAIT)
    response.raise_for_status()
    return response.json()


def _retry_after(response) -> float:
    """How long to wait, from whichever header the provider actually sent.

    X-Rate-Limit-Reset is an absolute epoch second; Retry-After is a delta.
    Falling back to a fixed wait rather than zero: a retry that fires
    immediately after a 429 is just a second 429.
    """
    reset = response.headers.get("X-Rate-Limit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except (TypeError, ValueError):
            pass
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return 5.0


def _unwrapped(call):
    """Run `call`, re-raising StopTheRun / RetryableStatus as themselves.

    mapbox_get raises those *inside* the adapter, whose _fetch wraps every
    exception in a token-scrubbed RoutingError. Left wrapped, a dead token
    reads as one more route error per listing and never reaches
    EXIT_AUTH_FAILED, and a 429 is never retried. Unwrapping is safe for
    the token: neither class's message contains the URL.
    """
    try:
        return call()
    except RoutingError as exc:
        if isinstance(exc.__cause__, (StopTheRun, RetryableStatus)):
            raise exc.__cause__ from None
        raise


def _with_retries(call, sleep):
    """Run `call`, waiting out rate limits and gateway errors. StopTheRun is
    never retried."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return _unwrapped(call)
        except RetryableStatus as exc:
            limit = MAX_RETRIES if exc.status == 429 else SERVER_ERROR_RETRIES
            if attempt >= limit:
                raise
            wait = min(max(exc.retry_after, 0.0), RATE_LIMIT_MAX_SLEEP)
            print(f"  HTTP {exc.status}, waiting {wait:.0f}s", flush=True)
            sleep(wait)
    raise AssertionError("unreachable")


def _parts(row) -> AddressParts:
    return AddressParts(
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip_code"],
    )


def _label(row) -> str:
    return f"{row['listing_id']} ({row['address']}, {row['city']})"


def _measure_one(row, *, geocode_fn, route_fn, arrive_by, sleep) -> CommuteResult:
    """One listing, through the retries, as a row. Never raises but for
    StopTheRun: any other failure is recorded in route_error."""
    try:
        result = compute_commute(
            _parts(row),
            DENVER_COWORKING,
            MEDTRONIC_LAFAYETTE,
            arrive_by,
            lambda p: _with_retries(lambda: geocode_fn(p), sleep),
            lambda o, d: _with_retries(lambda: route_fn(o, d), sleep),
        )
    except StopTheRun:
        # Deliberately before any upsert: the row still holds the previous
        # measurement, and overwriting it with nothing would destroy data
        # to record a failure of ours.
        raise
    except Exception as exc:  # noqa: BLE001
        result = CommuteResult(
            lat=None,
            lon=None,
            denver_miles=None,
            denver_minutes=None,
            medtronic_miles=None,
            medtronic_minutes=None,
            geocode_failed=False,
            arrive_by=arrive_by,
            route_error=f"{type(exc).__name__}: {exc}",
        )
    if result.route_error:
        result.route_error = redact(result.route_error)[:200]
    return result


def _failure(result: CommuteResult) -> str:
    return "no coordinates" if result.geocode_failed else str(result.route_error)


def run(
    conn,
    listings,
    *,
    geocode_fn,
    route_fn,
    arrive_by: str,
    sleep=time.sleep,
    upsert_fn=upsert_commute,
    canary=None,
) -> int:
    """Measure each listing and write its row. Returns the exit code.

    Split out of main() so the loop can be tested with fakes: main() opens
    Turso and reads the environment, neither of which belongs in a test of
    "what happens when the second of three listings rate-limits".

    `canary` is a listing row that has routed before (see `pick_canary`),
    or None if nothing has. It is only measured if this run routes nothing,
    and its result is never written.
    """
    measure_kwargs = dict(
        geocode_fn=geocode_fn, route_fn=route_fn, arrive_by=arrive_by, sleep=sleep
    )
    attempted = routed = 0
    failed = []

    for row in listings:
        attempted += 1
        result = _measure_one(row, **measure_kwargs)
        upsert_fn(conn, row["listing_id"], result)

        if result.medtronic_minutes is not None:
            routed += 1
            print(f"{_label(row)}: {result.medtronic_minutes:.1f} min", flush=True)
        else:
            failed.append(row["listing_id"])
            print(f"{_label(row)}: {_failure(result)}", flush=True)

        sleep(PACE_SECONDS)

    print(f"commutes: {routed}/{attempted} routed, arrive_by={arrive_by}")

    if routed or not attempted:
        # Some routed and some did not: the service works, and every failed
        # row is re-picked next run. Not EXIT_PARTIAL -- a blip that cleared
        # on the next listing is not worth an alert, and a listing that
        # fails the same way every run would alert on every run.
        return 0

    if canary is None:
        if attempted >= NOTHING_ROUTED_FLOOR:
            # Exiting 0 here is how a corpus of empty commutes reaches the
            # scorer looking like a successful run.
            print(
                f"commutes: nothing routed, no listing has ever routed to probe "
                f"with, and {attempted} attempted (floor is "
                f"{NOTHING_ROUTED_FLOOR}) -- treating the run as failed"
            )
            return EXIT_NOTHING_ROUTED
        print(
            f"commutes: nothing routed, no listing has ever routed to probe "
            f"with, and only {attempted} attempted (floor for calling that "
            f"systemic is {NOTHING_ROUTED_FLOOR}); the row(s) are recorded and "
            f"will be retried next run"
        )
        return 0

    # Not written: the canary's row is a good measurement from an earlier
    # run, and a failed probe must not replace it with an empty one.
    probe = _measure_one(canary, **measure_kwargs)
    if probe.medtronic_minutes is not None:
        print(
            f"commutes: nothing routed, but canary {canary['listing_id']} routed "
            f"fine; not systemic -- the failed row(s) are recorded and will be "
            f"retried next run: {', '.join(failed)}"
        )
        return 0
    print(
        f"commutes: nothing routed, and canary {canary['listing_id']} failed too "
        f"({_failure(probe)}) -- treating the run as failed"
    )
    return EXIT_NOTHING_ROUTED


def pick_canary(conn):
    """A listing that has routed under the current COMMUTE_SOURCE, or None.

    Lowest listing_id, so the same one is probed run after run and a flaky
    canary shows up as one flaky listing rather than as noise. Must be
    picked *before* the run writes anything, or a run that overwrites the
    corpus (--force) could pick from rows it just emptied.
    """
    return conn.execute(
        """
        SELECT l.* FROM listings l
        JOIN commute c ON c.listing_id = l.listing_id
        WHERE c.medtronic_minutes IS NOT NULL
          AND c.commute_source = ?
        ORDER BY l.listing_id
        LIMIT 1
        """,
        (COMMUTE_SOURCE,),
    ).fetchone()


def measure(
    conn,
    *,
    geocode_fn,
    route_fn,
    arrive_by: str,
    retry_failed: bool = True,
    force: bool = False,
    sleep=time.sleep,
) -> int:
    """Select the outstanding listings, then run() them. Returns the exit code.

    Everything main() does after it has a connection and a token, so that
    several runs can be exercised back to back against one database.
    """
    missing_ids = get_listing_ids_missing_commute(
        conn, retry_failed=retry_failed, force=force
    )
    if force:
        print(f"--force: recomputing all {len(missing_ids)} listing(s)")
    if not missing_ids:
        print(f"commute table already covers every listing ({COMMUTE_SOURCE})")
        return 0

    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    listings = [listings_by_id[lid] for lid in missing_ids if lid in listings_by_id]
    canary = pick_canary(conn)

    print(f"commutes: {len(listings)} listing(s), arrive_by={arrive_by}")
    return run(
        conn,
        listings,
        geocode_fn=geocode_fn,
        route_fn=route_fn,
        arrive_by=arrive_by,
        sleep=sleep,
        canary=canary,
    )


def main() -> int:
    env = load_env()
    token = env.get("MAPBOX_ACCESS_TOKEN")
    if not token:
        # Name the variable, never the value.
        print("compute_commutes: MAPBOX_ACCESS_TOKEN is not set", file=sys.stderr)
        return EXIT_NO_TOKEN
    set_redaction_token(token)

    conn = stage_connection()
    # --only-new skips listings whose previous attempt failed; the default
    # retries them, since a failure is usually transient and leaving it
    # unretried permanently neutralizes the commute factor. Neither flag
    # affects staleness: a row measured a different way is always selected.
    retry_failed = "--only-new" not in sys.argv
    # --force recomputes every listing regardless. Rarely needed now that
    # COMMUTE_SOURCE makes a measurement change self-invalidating; it stays
    # for the case where the provider's own answer has changed under a
    # source string that did not.
    force = "--force" in sys.argv

    arrive_by = next_arrival(datetime.now())

    try:
        code = measure(
            conn,
            geocode_fn=lambda parts: geocode_address(parts, token, mapbox_get),
            route_fn=lambda origin, dest: route(
                origin, dest, arrive_by, token, mapbox_get
            ),
            arrive_by=arrive_by,
            retry_failed=retry_failed,
            force=force,
        )
    except StopTheRun as exc:
        print(f"compute_commutes: {redact(str(exc))}", file=sys.stderr)
        return EXIT_AUTH_FAILED
    finally:
        conn.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
