"""Turning one listing's address into the two drive times the rubric reads.

Provider-free on purpose: the geocoder and the router arrive as injected
functions, and the only module that names a vendor is src/routing_mapbox.py.
What is left here is the part that is ours -- which arrival time every
listing is measured against, and how a partial failure is recorded.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

Coordinates = tuple[float, float]

# Stamped onto every row this module produces. The selector treats a row
# carrying anything else as outstanding work, so **bumping this string is how
# a re-measurement is triggered** -- the next pipeline run recomputes the
# corpus by itself, with no flag and no separate job.
#
# It therefore has to name everything that would change the number: the
# provider, the profile, and the arrival time. Change any of those and change
# this.
COMMUTE_SOURCE = "mapbox-arrive-0815/v1"

# Every commute in the corpus is measured against the same historical
# weekday profile so the numbers are comparable across listings. Wednesday is
# the conventional representative weekday; 08:15 is Megan's arrival.
ARRIVAL_WEEKDAY = 2  # Monday is 0
ARRIVAL_HOUR = 8
ARRIVAL_MINUTE = 15

# How far ahead the requested arrival must be. A cron firing at 06:16 on a
# Wednesday would otherwise ask about a time two minutes after the request
# lands, which is a live forecast rather than a typical morning.
MINIMUM_LEAD = timedelta(hours=2)

DENVER_TZ = ZoneInfo("America/Denver")


def next_arrival(now: datetime) -> str:
    """The arrival time to ask about, as `YYYY-MM-DDThh:mm` local, no offset.

    Mapbox reads a naive `arrive_by` in the origin's own time zone, which is
    exactly the question meant here: what does this drive cost at a quarter
    past eight in the morning where the house is. Emitting an offset would
    mean recomputing it either side of the DST boundary, and being wrong for
    half the year if that were ever missed.

    A naive `now` is read as Denver time; an aware one is converted first, so
    a caller passing `datetime.now(timezone.utc)` still gets the local
    answer rather than one shifted six hours.
    """
    local = now.replace(tzinfo=DENVER_TZ) if now.tzinfo is None else now.astimezone(DENVER_TZ)

    candidate = local.replace(
        hour=ARRIVAL_HOUR, minute=ARRIVAL_MINUTE, second=0, microsecond=0
    )
    # Walk forward a day at a time rather than doing weekday arithmetic: the
    # replace() above is applied before the day changes, so each candidate is
    # a real local 08:15 and the zone handles its own DST offset.
    while candidate.weekday() != ARRIVAL_WEEKDAY or candidate - local < MINIMUM_LEAD:
        candidate = (candidate + timedelta(days=1)).replace(
            hour=ARRIVAL_HOUR, minute=ARRIVAL_MINUTE, second=0, microsecond=0
        )
    return candidate.strftime("%Y-%m-%dT%H:%M")


@dataclass(kw_only=True)
class CommuteResult:
    """One row of the `commute` table.

    Keyword-only. Ten fields, most of them nullable floats of the same type,
    so a positional constructor is a silent mis-assignment waiting to happen
    -- and did happen once, putting a Denver duration in a Medtronic column.
    """

    lat: float | None
    lon: float | None
    denver_miles: float | None
    denver_minutes: float | None
    medtronic_miles: float | None
    medtronic_minutes: float | None
    # Means only what it says: we do not know where this house is. It used to
    # also be set when a *route* failed, which conflated "no coordinates"
    # with "coordinates but no road" -- two states whose fixes are different
    # and which nothing downstream could tell apart (#32).
    geocode_failed: bool
    commute_source: str = COMMUTE_SOURCE
    arrive_by: str | None = None
    # Short reason a leg did not route, naming the leg. The honest half of
    # what geocode_failed used to be asked to carry.
    route_error: str | None = None


def compute_commute(
    address_parts,
    denver_coords: Coordinates,
    medtronic_coords: Coordinates,
    arrive_by: str,
    geocode_fn: Callable[[object], tuple[float, float, str] | None],
    route_fn: Callable[[Coordinates, Coordinates], tuple[float, float] | None],
) -> CommuteResult:
    """One geocode and two routes, assembled into a row.

    Always returns a row, including for a geocode miss -- a listing we cannot
    place is a settled answer, not outstanding work, and leaving it unstamped
    would make the selector retry it on every run for as long as it is
    listed.

    An exception from `route_fn` propagates. The stage is the only layer that
    can tell a 401 (abort the run) from a 429 (wait and retry) from a blip,
    so swallowing one here would turn an expired token into a corpus of empty
    commutes.
    """
    origin = geocode_fn(address_parts)
    if origin is None:
        return CommuteResult(
            lat=None,
            lon=None,
            denver_miles=None,
            denver_minutes=None,
            medtronic_miles=None,
            medtronic_minutes=None,
            geocode_failed=True,
            arrive_by=arrive_by,
        )

    coordinates = (origin[0], origin[1])
    denver = route_fn(coordinates, denver_coords)
    medtronic = route_fn(coordinates, medtronic_coords)

    failed_legs = [
        name
        for name, leg in (("denver", denver), ("medtronic", medtronic))
        if leg is None
    ]
    return CommuteResult(
        lat=coordinates[0],
        lon=coordinates[1],
        denver_miles=denver[0] if denver else None,
        denver_minutes=denver[1] if denver else None,
        medtronic_miles=medtronic[0] if medtronic else None,
        medtronic_minutes=medtronic[1] if medtronic else None,
        geocode_failed=False,
        arrive_by=arrive_by,
        route_error=(
            f"no route: {', '.join(failed_legs)}" if failed_legs else None
        ),
    )
