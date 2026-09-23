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
at all. It splits "anything else" in two:

  answered no     the service worked and said no: a geocode miss, or "no
                  route" for a coordinate. A fact about the address.
  did not answer  an exception after the retries: 5xx, timeout, a 429 that
                  never cleared, a bug of ours. A fact about us.

One "did not answer" with nothing routed fails the run. "Answered no" only
does at `NOTHING_ROUTED_FLOOR` of them, and a listing whose previous row
was *also* answered-no is known-bad and not counted at all: the selector
re-picks it every run, forever, and it would otherwise trip the floor on
its own the day there are enough of them.
"""

import sys
import time
from collections.abc import Collection
from pathlib import Path

import requests

from src.commute import COMMUTE_SOURCE, compute_commute, next_arrival
from src.config import load_env
from src.db import (
    get_commutes_by_listing,
    get_listing_ids_missing_commute,
    query_listings,
    upsert_commute,
)
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

# How many *new* answered-no failures (a geocode miss, or "no route" for a
# coordinate) it takes, with nothing routed, before the run is read as
# systemic rather than as N bad addresses. Set from one incident: a run
# with exactly one listing to measure, whose address would not geocode,
# failed the entire pipeline over it.
#
# This floor does not accumulate across runs, and is not meant to. Only new
# answered-no failures count toward it -- a listing that answered no last
# run too is known-bad and excluded, because the selector re-picks failed
# rows on every run with no cap and three permanent bad addresses would
# otherwise fail every quiet run forever. So the floor is crossed only by
# FLOOR fresh no-answers arriving in the same run. Outages are not what it
# is for: those surface as exceptions, and one of those is enough.
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


def run(
    conn,
    listings,
    *,
    geocode_fn,
    route_fn,
    arrive_by: str,
    sleep=time.sleep,
    upsert_fn=upsert_commute,
    known_bad: Collection[str] = frozenset(),
) -> int:
    """Measure each listing and write its row. Returns the exit code.

    Split out of main() so the loop can be tested with fakes: main() opens
    Turso and reads the environment, neither of which belongs in a test of
    "what happens when the second of three listings rate-limits".

    `known_bad` is the listing_ids whose previous row was already an
    answered-no failure (see `known_bad_ids`).
    """
    attempted = routed = fresh_no = old_no = service_errors = 0

    for row in listings:
        attempted += 1
        parts = AddressParts(
            address=row["address"],
            city=row["city"],
            state=row["state"],
            zip_code=row["zip_code"],
        )
        label = f"{row['listing_id']} ({parts.address}, {parts.city})"
        raised = False

        try:
            result = compute_commute(
                parts,
                DENVER_COWORKING,
                MEDTRONIC_LAFAYETTE,
                arrive_by,
                lambda p: _with_retries(lambda: geocode_fn(p), sleep),
                lambda o, d: _with_retries(lambda: route_fn(o, d), sleep),
            )
        except StopTheRun:
            # Deliberately before the upsert: the row still holds the
            # previous measurement, and overwriting it with nothing would
            # destroy data to record a failure of ours.
            raise
        except Exception as exc:  # noqa: BLE001
            from src.commute import CommuteResult

            raised = True
            reason = redact(f"{type(exc).__name__}: {exc}")[:200]
            result = CommuteResult(
                lat=None,
                lon=None,
                denver_miles=None,
                denver_minutes=None,
                medtronic_miles=None,
                medtronic_minutes=None,
                geocode_failed=False,
                arrive_by=arrive_by,
                route_error=reason,
            )

        if result.route_error:
            result.route_error = redact(result.route_error)[:200]

        upsert_fn(conn, row["listing_id"], result)

        if result.medtronic_minutes is not None:
            routed += 1
            print(f"{label}: {result.medtronic_minutes:.1f} min", flush=True)
        else:
            what = (
                "no coordinates"
                if result.geocode_failed
                else redact(str(result.route_error))
            )
            if raised:
                service_errors += 1
            elif row["listing_id"] in known_bad:
                old_no += 1
                what += " (known-bad: failed last run too; not counted as systemic)"
            else:
                fresh_no += 1
            print(f"{label}: {what}", flush=True)

        sleep(PACE_SECONDS)

    print(f"commutes: {routed}/{attempted} routed, arrive_by={arrive_by}")

    if routed or not attempted:
        # Some routed and some did not: the service works, and every failed
        # row is re-picked next run. Not EXIT_PARTIAL -- a blip that cleared
        # on the next listing is not worth an alert, and a listing that
        # fails the same way every run would alert on every run.
        return 0
    if service_errors or fresh_no >= NOTHING_ROUTED_FLOOR:
        # Exiting 0 here is how a corpus of empty commutes reaches the
        # scorer looking like a successful run.
        print(
            f"commutes: nothing routed at all -- {service_errors} service "
            f"error(s), {fresh_no} new no-match answer(s), {old_no} known-bad; "
            f"treating the run as failed"
        )
        return EXIT_NOTHING_ROUTED
    print(
        f"commutes: nothing routed, but only {fresh_no} new no-match answer(s) "
        f"(floor for calling that systemic is {NOTHING_ROUTED_FLOOR}) and no "
        f"service errors; the row(s) are recorded and will be retried next run"
    )
    return 0


def answered_no(row) -> bool:
    """Whether a stored commute row records the service answering no -- a
    geocode miss or "no route" -- as opposed to a success or an exception."""
    if row["geocode_failed"]:
        return True
    return row["medtronic_minutes"] is None and str(row["route_error"] or "").startswith(
        "no route"
    )


def known_bad_ids(conn, listing_ids: Collection[str]) -> frozenset[str]:
    """The listings, among those about to be attempted, whose last row was
    already an answered-no failure. One read of the whole commute table
    rather than one per listing: against Turso a statement is a round-trip."""
    previous = get_commutes_by_listing(conn)
    return frozenset(
        lid for lid in listing_ids if lid in previous and answered_no(previous[lid])
    )


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
    known_bad = known_bad_ids(conn, missing_ids)

    print(f"commutes: {len(listings)} listing(s), arrive_by={arrive_by}")
    return run(
        conn,
        listings,
        geocode_fn=geocode_fn,
        route_fn=route_fn,
        arrive_by=arrive_by,
        sleep=sleep,
        known_bad=known_bad,
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
