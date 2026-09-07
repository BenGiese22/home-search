"""What the digest says, and when it says anything at all."""

from datetime import datetime, timedelta, timezone

from src.db import KIND_DELISTED, KIND_NEW, KIND_PRICE
from src.digest import STALE_AFTER, compose, should_send

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def event(kind, listing_ref, detail=None, detected=NOW, event_id=None):
    return {
        "event_id": event_id or f"{kind}:{listing_ref}",
        "kind": kind,
        "listing_ref": listing_ref,
        "detail": detail,
        "detected_at": detected.isoformat(),
        "notified_at": None,
    }


def listing(address="8221 West 93rd Way", city="Westminster", price="$625,000"):
    return {
        "address": address, "city": city, "price": price,
        "beds": 3, "baths": 2.5, "sqft": 2140,
    }


# --- whether to send ------------------------------------------------------


def test_nothing_changed_sends_nothing():
    assert should_send([], NOW) is False


def test_a_new_listing_always_sends():
    assert should_send([event(KIND_NEW, "L1")], NOW) is True


def test_a_price_change_alone_waits_to_be_carried():
    """Ben's call: a new listing interrupts, a price change does not. An
    email nobody asked for is how the one they did ask for stops being read."""
    assert should_send([event(KIND_PRICE, "L2", "$599k -> $579k")], NOW) is False


def test_a_delisting_alone_waits_too():
    assert should_send([event(KIND_DELISTED, "L3")], NOW) is False


def test_a_price_change_rides_along_with_a_new_listing():
    events = [event(KIND_NEW, "L1"), event(KIND_PRICE, "L2")]
    assert should_send(events, NOW) is True


def test_changes_that_have_waited_a_week_send_on_their_own():
    """Otherwise a price drop on a house being watched waits indefinitely for
    an unrelated house to appear."""
    old = event(KIND_PRICE, "L2", detected=NOW - STALE_AFTER - timedelta(hours=1))
    assert should_send([old], NOW) is True


def test_changes_just_short_of_a_week_still_wait():
    recent = event(KIND_PRICE, "L2", detected=NOW - STALE_AFTER + timedelta(hours=1))
    assert should_send([recent], NOW) is False


def test_an_unreadable_timestamp_errs_toward_sending():
    """One extra email is the cheap direction; suppressing forever is not."""
    broken = event(KIND_PRICE, "L2")
    broken["detected_at"] = "not a date"
    assert should_send([broken], NOW) is True


# --- what it says ---------------------------------------------------------


def test_the_subject_counts_each_kind():
    events = [event(KIND_NEW, "L1"), event(KIND_NEW, "L4"), event(KIND_PRICE, "L2")]
    digest = compose(events, {}, {}, total=101)
    assert digest.subject == "2 new listings, 1 price change"


def test_a_single_change_is_not_pluralised():
    digest = compose([event(KIND_NEW, "L1")], {}, {}, total=101)
    assert digest.subject == "1 new listing"


def test_a_new_listing_carries_its_rank_and_the_numbers():
    """An address and a price are not enough to decide whether to look. The
    rubric has already ranked this house against the other hundred, which is
    the whole reason the digest runs after scoring."""
    digest = compose(
        [event(KIND_NEW, "L1")],
        {"L1": listing()},
        {"L1": (4, 71.2)},
        total=101,
        site_url="https://short-list.bgiese.tech",
    )
    assert "ranked #4 of 101" in digest.body
    assert "8221 West 93rd Way, Westminster" in digest.body
    assert "$625,000 · 3bd 2.5ba · 2,140 sqft" in digest.body
    assert "composite 71.2" in digest.body
    assert "https://short-list.bgiese.tech/listing/L1" in digest.body


def test_a_price_change_shows_the_movement():
    digest = compose(
        [event(KIND_PRICE, "L2", "$599,000 -> $579,000")],
        {"L2": listing(address="6545 Dover Street", city="Arvada")},
        {"L2": (12, 58.2)},
        total=101,
    )
    assert "$599,000 -> $579,000" in digest.body
    assert "ranked #12" in digest.body


def test_a_delisting_names_the_house_from_the_event_itself():
    """Its listing row is gone by the time this runs, so the address has to
    have been captured when the change was detected."""
    digest = compose(
        [event(KIND_DELISTED, "L3", detail="12651 James Circle")],
        {},
        {},
        total=101,
    )
    assert "12651 James Circle" in digest.body
    assert "gone from the collection" in digest.body


def test_an_unscored_new_listing_still_appears():
    """A listing that arrived this run may not be scored yet. Dropping the
    line would hide exactly the thing the email exists for."""
    digest = compose([event(KIND_NEW, "L1")], {"L1": listing()}, {}, total=101)
    assert "not scored yet" in digest.body
    assert "8221 West 93rd Way" in digest.body


def test_every_event_is_claimed_so_none_is_reported_twice():
    events = [event(KIND_NEW, "L1"), event(KIND_PRICE, "L2")]
    digest = compose(events, {}, {}, total=101)
    assert digest.event_ids == [e["event_id"] for e in events]


def test_the_body_links_back_to_the_full_ranking():
    digest = compose([event(KIND_NEW, "L1")], {"L1": listing()}, {}, total=101,
                     site_url="https://short-list.bgiese.tech")
    assert "The full ranking: https://short-list.bgiese.tech" in digest.body


def test_no_site_url_produces_no_broken_links():
    digest = compose([event(KIND_NEW, "L1")], {"L1": listing()}, {}, total=101)
    assert "http" not in digest.body
