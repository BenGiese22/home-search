"""src/commute.py after the traffic-aware rebuild.

What used to live here -- Nominatim geocoding, the Census fallback, OSRM
routing, destination resolution -- is gone. The provider lives in
src/routing_mapbox.py (tested separately) and this module is now two things:
the rule for which arrival time to ask about, and the per-listing assembly of
one geocode plus two routes into a row.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.commute import (
    COMMUTE_SOURCE,
    CommuteResult,
    compute_commute,
    next_arrival,
)

DENVER_TZ = ZoneInfo("America/Denver")
DENVER = (39.765313, -104.978703)
MEDTRONIC = (39.962369, -105.08848)


def at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=DENVER_TZ)


# --- which arrival do we ask about? -------------------------------------
#
# A pinned weekday, not "the next weekday". Every listing is then measured
# against the same historical profile, so one computed by a Friday-fired cron
# and one computed by a Monday-fired cron are comparable numbers. Without
# that, half the ranking would be measuring a different question.


def test_a_monday_run_asks_about_the_coming_wednesday():
    assert next_arrival(at(2026, 9, 7, 3)) == "2026-09-09T08:15"


def test_a_run_early_on_wednesday_asks_about_that_same_morning():
    assert next_arrival(at(2026, 9, 9, 3)) == "2026-09-09T08:15"


def test_a_run_on_wednesday_afternoon_asks_about_the_next_wednesday():
    assert next_arrival(at(2026, 9, 9, 15)) == "2026-09-16T08:15"


def test_a_friday_run_asks_about_the_following_wednesday():
    assert next_arrival(at(2026, 9, 11, 3)) == "2026-09-16T08:15"


def test_a_weekend_run_asks_about_the_coming_wednesday():
    assert next_arrival(at(2026, 9, 5, 12)) == "2026-09-09T08:15"
    assert next_arrival(at(2026, 9, 6, 12)) == "2026-09-09T08:15"


def test_the_arrival_must_be_at_least_two_hours_ahead():
    """A cron that fires at 06:16 on a Wednesday would otherwise ask Mapbox
    about a time two minutes after the request lands, which is a forecast
    rather than a typical morning."""
    assert next_arrival(at(2026, 9, 9, 6, 14)) == "2026-09-09T08:15"
    assert next_arrival(at(2026, 9, 9, 6, 16)) == "2026-09-16T08:15"


def test_the_stamp_carries_no_offset():
    """Mapbox reads a naive arrive_by in the origin's local time zone, which
    is the question we mean. An offset would have to be recomputed across the
    DST boundary and would be wrong for half the year if it were not."""
    stamp = next_arrival(at(2026, 9, 7, 3))
    assert "+" not in stamp and "Z" not in stamp
    assert stamp.endswith("T08:15")


def test_the_rule_holds_across_the_dst_transition():
    """US DST ends Sunday 2026-11-01. A run just before it still asks about
    08:15 local on the Wednesday after, not 07:15 or 09:15."""
    assert next_arrival(at(2026, 10, 31, 12)) == "2026-11-04T08:15"
    assert next_arrival(at(2026, 11, 1, 12)) == "2026-11-04T08:15"


def test_a_naive_now_is_read_as_denver_time():
    """The stage passes datetime.now(); a caller that forgets the zone should
    get the local answer rather than a UTC-shifted one."""
    assert next_arrival(datetime(2026, 9, 7, 3)) == "2026-09-09T08:15"


def test_a_now_in_another_zone_is_converted_before_the_rule_applies():
    """03:00 UTC on Thursday is 21:00 Wednesday in Denver -- past the
    cutoff, so the answer is the following Wednesday, not the same day."""
    from datetime import timezone

    assert next_arrival(datetime(2026, 9, 10, 3, tzinfo=timezone.utc)) == "2026-09-16T08:15"


# --- assembling one listing's row ---------------------------------------


def geocoder(result):
    return lambda parts: result


def router(results):
    """A route_fn that answers per destination, so a test can fail one leg."""
    return lambda origin, destination: results[destination]


ARRIVE = "2026-09-09T08:15"


def test_a_geocode_miss_writes_a_row_that_says_so_and_routes_nothing():
    calls = []

    def route_fn(origin, destination):
        calls.append(destination)
        return (1.0, 1.0)

    result = compute_commute(
        object(), DENVER, MEDTRONIC, ARRIVE, geocoder(None), route_fn
    )
    assert result.geocode_failed is True
    assert result.lat is None and result.medtronic_minutes is None
    assert calls == []
    # Still a row, and still stamped: a listing that cannot be geocoded is
    # not outstanding work, and leaving the source NULL would make the
    # selector retry it on every run forever.
    assert result.commute_source == COMMUTE_SOURCE
    assert result.arrive_by == ARRIVE


def test_both_legs_are_measured_and_stamped():
    result = compute_commute(
        object(),
        DENVER,
        MEDTRONIC,
        ARRIVE,
        geocoder((39.86, -105.08, "rooftop")),
        router({DENVER: (18.0, 34.0), MEDTRONIC: (9.0, 22.0)}),
    )
    assert (result.lat, result.lon) == (39.86, -105.08)
    assert (result.denver_miles, result.denver_minutes) == (18.0, 34.0)
    assert (result.medtronic_miles, result.medtronic_minutes) == (9.0, 22.0)
    assert result.geocode_failed is False
    assert result.route_error is None
    assert result.commute_source == COMMUTE_SOURCE
    assert result.arrive_by == ARRIVE


def test_a_failed_leg_is_named_rather_than_reported_as_a_geocode_failure():
    """This is issue #32. geocode_failed used to be set when a *route*
    failed, so the one field that should have said "we do not know where this
    house is" also meant "we know where it is, but could not drive there".
    Nothing could tell the two apart, and the fix for each is different."""
    result = compute_commute(
        object(),
        DENVER,
        MEDTRONIC,
        ARRIVE,
        geocoder((39.86, -105.08, "rooftop")),
        router({DENVER: (18.0, 34.0), MEDTRONIC: None}),
    )
    assert result.geocode_failed is False
    assert result.medtronic_minutes is None
    assert result.denver_minutes == 34.0
    assert "medtronic" in result.route_error


def test_a_failed_display_leg_does_not_hide_the_scored_one():
    result = compute_commute(
        object(),
        DENVER,
        MEDTRONIC,
        ARRIVE,
        geocoder((39.86, -105.08, "rooftop")),
        router({DENVER: None, MEDTRONIC: (9.0, 22.0)}),
    )
    assert result.medtronic_minutes == 22.0
    assert result.denver_minutes is None
    assert "denver" in result.route_error


def test_both_legs_failing_names_both():
    result = compute_commute(
        object(),
        DENVER,
        MEDTRONIC,
        ARRIVE,
        geocoder((39.86, -105.08, "rooftop")),
        router({DENVER: None, MEDTRONIC: None}),
    )
    assert "denver" in result.route_error and "medtronic" in result.route_error


def test_the_coordinates_are_kept_when_a_route_fails():
    """The geocode succeeded and cost a request. Throwing the coordinates
    away would make the retry pay for it again."""
    result = compute_commute(
        object(),
        DENVER,
        MEDTRONIC,
        ARRIVE,
        geocoder((39.86, -105.08, "rooftop")),
        router({DENVER: None, MEDTRONIC: None}),
    )
    assert (result.lat, result.lon) == (39.86, -105.08)


def test_an_exception_from_the_router_propagates():
    """The stage decides what a transport failure means -- a 401 aborts the
    run, a 429 is retried. This function cannot tell them apart, so it must
    not swallow either."""
    import pytest

    def route_fn(origin, destination):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        compute_commute(
            object(),
            DENVER,
            MEDTRONIC,
            ARRIVE,
            geocoder((39.86, -105.08, "rooftop")),
            route_fn,
        )


def test_commute_result_is_keyword_only():
    """Seven nullable fields of the same types, and three more added by this
    rebuild. Positional construction put denver_miles where medtronic_miles
    belonged exactly once, and nothing caught it."""
    import pytest

    with pytest.raises(TypeError):
        CommuteResult(None, None, None, None, None, None, True)


def test_the_source_string_is_specific_enough_to_invalidate_on():
    """Bumping COMMUTE_SOURCE is the migration mechanism: every row stamped
    with anything else becomes outstanding work. It therefore has to name the
    things that would change the number."""
    assert "mapbox" in COMMUTE_SOURCE
    assert "0815" in COMMUTE_SOURCE
