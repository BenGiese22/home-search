"""Pushing a rejection back to Compass, and believing it only when proven.

The write is one PUT that answers `200 {}`. Nothing in that response says the
listing moved, and this project has produced "HTTP 200, valid data, plausible
counts, wrong answer" often enough that a 200 is not allowed to be the
evidence. The evidence is the collection fetch the run makes immediately
afterwards -- marking a listing notInterested moves it into filter 3, and
filters 0 and 1 are the only ones scrape fetches, so a rejected listing that
has left the fetch is one Compass acted on.

That ordering is the design: sync first, fetch second, and the fetch the run
was making anyway is the read-back.
"""

import sqlite3

import pytest

import scrape
from src.db import reject_property, upsert_property_id
from src.scraper import CollectionFetch
from src.turso_db import ensure_schema

COLLECTION_URL = "https://www.compass.com/app/collection/6a27426b698343000129b139/matches"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


class FakePage:
    """Stands in for the authenticated Playwright page.

    Records PUTs, and answers the notInterested read with whatever the test
    says Compass holds -- which is the whole point of the read-back, so it
    has to be settable per test.
    """

    def __init__(self, status=200, raises=None, pile=(), pile_raises=None):
        self.request = self
        self.puts = []
        self._status = status
        self._raises = raises
        self.pile = list(pile)
        self._pile_raises = pile_raises

    def put(self, url, data=None, headers=None):
        self.puts.append({"url": url, "data": data, "headers": headers})
        if self._raises:
            raise self._raises
        return type("Response", (), {"status": self._status})()

    def get(self, url):
        if self._pile_raises:
            raise self._pile_raises
        items = [{"listingData": {"listingIdSHA": lid}} for lid in self.pile]
        return type(
            "Response", (), {
                "json": lambda _self: {
                    "totalListings": len(items), "currentPageListings": items,
                }
            },
        )()


def listing(lid):
    return type("L", (), {"listing_id": lid})()


def fetch_of(*lids, errors=None):
    return CollectionFetch(
        listings=[listing(lid) for lid in lids],
        counts={"matches": len(lids)},
        errors=errors or {},
        tab_ids={"matches": frozenset(lids)},
    )


def rejected(conn, pid, lid):
    upsert_property_id(conn, lid, pid)
    reject_property(conn, pid, listing_ref=lid, address=f"{pid} St")


def synced(conn, pid):
    row = conn.execute(
        "SELECT compass_synced_at FROM rejections WHERE property_id = ?", (pid,)
    ).fetchone()
    return row["compass_synced_at"] is not None


# --- the write ----------------------------------------------------------


def test_a_pending_rejection_is_sent_in_the_shape_compass_uses(conn):
    rejected(conn, "131FZM", "L1")
    page = FakePage()

    sent = scrape._sync_rejections_to_compass(conn, page, COLLECTION_URL)

    assert sent == {"131FZM": "L1"}
    assert len(page.puts) == 1
    assert page.puts[0]["url"].endswith("/not_interested")
    assert page.puts[0]["data"] == {
        "collectionIdToListingIds": {"6a27426b698343000129b139": ["L1"]}
    }


def test_nothing_pending_makes_no_request(conn):
    page = FakePage()
    assert scrape._sync_rejections_to_compass(conn, page, COLLECTION_URL) == {}
    assert page.puts == []


def test_an_already_synced_rejection_is_not_sent_again(conn):
    rejected(conn, "131FZM", "L1")
    page = FakePage(pile=["L1"])
    scrape._sync_rejections_to_compass(conn, page, COLLECTION_URL)
    scrape._confirm_rejection_sync(
        conn, page, COLLECTION_URL, {"131FZM": "L1"}, fetch_of("L2")
    )

    page = FakePage()
    assert scrape._sync_rejections_to_compass(conn, page, COLLECTION_URL) == {}
    assert page.puts == []


def test_no_collection_url_means_no_write(conn):
    """There would be no collection to name, and no fetch to verify against."""
    rejected(conn, "131FZM", "L1")
    page = FakePage()

    assert scrape._sync_rejections_to_compass(conn, page, None) == {}
    assert page.puts == []


def test_a_refused_write_does_not_stop_the_scrape(conn):
    """The rejection is already recorded on our side, which is the half that
    governs what Ben sees. Compass being out of step is an inconvenience, and
    it stays pending for the next run."""
    rejected(conn, "131FZM", "L1")

    sent = scrape._sync_rejections_to_compass(
        conn, FakePage(status=403), COLLECTION_URL
    )

    assert sent == {}
    assert not synced(conn, "131FZM")


def test_a_transport_failure_does_not_stop_the_scrape(conn):
    rejected(conn, "131FZM", "L1")

    sent = scrape._sync_rejections_to_compass(
        conn, FakePage(raises=RuntimeError("connection reset")), COLLECTION_URL
    )

    assert sent == {}
    assert not synced(conn, "131FZM")


# --- the read-back ------------------------------------------------------


def sync_and_confirm(conn, page, fetch):
    sent = scrape._sync_rejections_to_compass(conn, page, COLLECTION_URL)
    scrape._confirm_rejection_sync(conn, page, COLLECTION_URL, sent, fetch)


def note(conn, pid):
    row = conn.execute(
        "SELECT compass_sync_note FROM rejections WHERE property_id = ?", (pid,)
    ).fetchone()
    return row["compass_sync_note"]


def test_a_listing_found_in_the_pile_is_recorded_as_confirmed(conn):
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(conn, FakePage(pile=["L1"]), fetch_of("L2", "L3"))

    assert synced(conn, "131FZM")
    assert note(conn, "131FZM") == "confirmed"


def test_a_listing_still_in_the_next_fetch_stays_pending(conn):
    """A 200 with an empty body means accepted, not moved. Recording this as
    synced would lose the rejection permanently; leaving it pending costs one
    request next run."""
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(conn, FakePage(pile=[]), fetch_of("L1", "L2"))

    assert not synced(conn, "131FZM")


def test_a_listing_compass_does_not_hold_is_recorded_as_absent(conn):
    """The 2026-09-07 regression. Gone from the fetched tabs AND not in the
    pile is not a confirmation -- re-sending will never do anything, so the
    retry stops, but it must not read as "Compass moved it"."""
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(conn, FakePage(pile=["L9"]), fetch_of("L2"))

    assert synced(conn, "131FZM")
    assert note(conn, "131FZM") == "absent"


def test_a_failed_tab_confirms_nothing(conn):
    """A tab that errored is missing every listing in it, which reads exactly
    like every rejection succeeding at once. This is the same trap that
    collection_fetch_is_trustworthy exists for on the delisting side."""
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(
        conn, FakePage(pile=["L1"]), fetch_of("L2", errors={"favorites": "timeout"})
    )

    assert not synced(conn, "131FZM")


def test_an_empty_fetch_confirms_nothing(conn):
    """Zero listings back is a broken fetch, not a collection Compass emptied
    on our behalf."""
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(
        conn, FakePage(pile=["L1"]),
        CollectionFetch(listings=[], counts={}, errors={}, tab_ids={}),
    )

    assert not synced(conn, "131FZM")


def test_an_unreadable_pile_confirms_nothing(conn):
    """Without the pile there is only absence, and absence is what got this
    wrong the first time. Retrying costs one request; a false confirmation
    costs the rejection."""
    rejected(conn, "131FZM", "L1")

    sync_and_confirm(
        conn, FakePage(pile_raises=RuntimeError("HTTP 500")), fetch_of("L2")
    )

    assert not synced(conn, "131FZM")
