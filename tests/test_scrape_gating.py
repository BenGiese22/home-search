"""The scrape stage's per-listing decisions, as pure functions.

These used to be inline conditions inside a Playwright loop, which meant the
only way to exercise `--force` against `--backfill-missing` was to run a real
scrape. They are extracted so the interactions are testable, because the
decision they make is expensive in both directions: process a listing that
did not need it and you re-download 20-50 photos from Compass; skip one that
did and it silently keeps stale data forever.
"""
import scrape
from scrape import should_process
from src.models import Listing


def _listing(n=1, photos=3):
    return Listing(
        listing_id=f"L{n:04d}", address=f"{n} Test St", city="Arvada", state="CO",
        zip_code="80003", price="$600,000", beds=3, baths=2.0, sqft=2000,
        lot_sqft=7000, parking_spaces=2, year_built=2000, description="d",
        amenities=[], photo_urls=[f"https://img/{n}/{i}.jpg" for i in range(photos)],
        listing_url=f"https://example.com/{n}")


# --- the ordinary case ---------------------------------------------------

def test_an_unscraped_listing_is_processed():
    assert should_process(force=False, scraped=False, stale=False) is True


def test_an_already_scraped_listing_is_skipped():
    """The whole point: a steady-state run must do no photo work at all."""
    assert should_process(force=False, scraped=True, stale=False) is False


# --- --force -------------------------------------------------------------

def test_force_processes_even_a_scraped_listing():
    assert should_process(force=True, scraped=True, stale=False) is True


def test_force_beats_every_other_flag():
    for scraped in (True, False):
        for stale in (True, False):
            assert should_process(force=True, scraped=scraped, stale=stale) is True


# --- --backfill-missing --------------------------------------------------

def test_a_stale_listing_is_processed_even_though_it_is_scraped():
    """--backfill-missing exists for listings scraped before a field was
    added: they are complete by the photo gate and incomplete by the schema."""
    assert should_process(force=False, scraped=True, stale=True) is True


def test_staleness_does_not_rescue_an_unscraped_listing_from_being_processed():
    assert should_process(force=False, scraped=False, stale=True) is True


# --- the photo gate itself ----------------------------------------------

def test_the_gate_reads_the_hosted_index_not_the_disk():
    """needs_photo_work replaced is_scraped(). The difference matters only
    where it counts: a sandbox has no disk, so the file check answered
    'unscraped' for everything and re-downloaded the corpus."""
    listing = _listing(1, photos=2)
    hosted = {("L0001", 1): listing.photo_urls[0], ("L0001", 2): listing.photo_urls[1]}

    assert scrape.needs_photo_work(listing, hosted) is False
    assert scrape.needs_photo_work(listing, {}) is True


def test_a_listing_whose_photos_changed_is_not_considered_scraped():
    """The relist case, end to end through the gate the scrape actually uses."""
    listing = _listing(1, photos=2)
    hosted = {("L0001", 1): "https://img/OLD/0.jpg", ("L0001", 2): listing.photo_urls[1]}

    assert scrape.needs_photo_work(listing, hosted) is True


# --- the in-memory index update -----------------------------------------

def test_recording_a_scrape_stops_the_second_loop_repeating_the_first():
    """A listing can arrive via LISTING_URLS and again via the collection.
    Without recording it in the index between the two loops, the collection
    loop re-downloads photos the pinned loop just fetched."""
    listing = _listing(1, photos=3)
    hosted = {}

    assert scrape.needs_photo_work(listing, hosted) is True
    scrape.record_hosted(hosted, listing)
    assert scrape.needs_photo_work(listing, hosted) is False


def test_recording_only_claims_the_positions_the_listing_actually_has():
    listing = _listing(1, photos=2)
    hosted = {}
    scrape.record_hosted(hosted, listing)

    assert set(hosted) == {("L0001", 1), ("L0001", 2)}


# --- the round-trip budget ----------------------------------------------

def test_the_scrapes_database_work_does_not_grow_with_the_corpus():
    """Every statement is a ~240ms round-trip, so anything proportional to
    listing count is minutes of latency. This counts the database work
    scrape.main() does outside its two network loops -- the setup that gates
    the run, and the tail that rebuilds the CSV and gallery -- and asserts it
    is the same for 5 listings as for 50.

    The bulk write is excluded because it is legitimately chunked; everything
    here should be flat.
    """
    import sqlite3

    from src.db import (
        get_amenities_by_listing,
        get_listing_ids_missing_fields,
        get_photo_urls_by_listing,
        get_pinned_listing_ids,
        get_price_snapshot,
        hosted_photo_index,
        listing_ids_with_any_hosted_or_no_urls,
        listings_from_rows,
        query_listings,
        upsert_listing,
    )
    from src.turso_db import ensure_schema

    counts = {}
    for size in (5, 50):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        for n in range(size):
            upsert_listing(conn, _listing(n))

        seen = []
        conn.set_trace_callback(seen.append)

        # --- setup, exactly as main() does it ---
        get_price_snapshot(conn)
        get_pinned_listing_ids(conn)
        hosted_photo_index(conn)
        set(get_listing_ids_missing_fields(conn, scrape.BACKFILL_FIELDS))
        listing_ids_with_any_hosted_or_no_urls(conn)
        # --- tail, exactly as main() does it ---
        rows = query_listings(conn)
        listings_from_rows(rows, get_amenities_by_listing(conn), get_photo_urls_by_listing(conn))

        conn.set_trace_callback(None)
        counts[size] = len([s for s in seen if s.strip().upper().startswith("SELECT")])

    assert counts[5] == counts[50], f"database work grew with the corpus: {counts}"
    assert counts[50] <= 10, f"{counts[50]} statements is more than the budget allows"


def test_orphan_ids_reads_the_skip_set_once(monkeypatch):
    """One statement, not one per listing.

    Every statement against hosted Turso is a ~240ms round-trip. Calling the
    skip-set query inside the comprehension would have made this O(corpus)
    round-trips -- the exact shape this project has a standing rule against.
    """
    import scrape

    calls = {"skip_set": 0}
    monkeypatch.setattr(scrape, "get_listing_ids_missing_fields", lambda *a: [])
    monkeypatch.setattr(
        scrape, "query_listings",
        lambda *a: [{"listing_id": f"L{i}"} for i in range(50)],
    )

    def counting_skip_set(_conn):
        calls["skip_set"] += 1
        return frozenset()

    monkeypatch.setattr(scrape, "listing_ids_with_any_hosted_or_no_urls", counting_skip_set)

    ids = scrape.orphan_ids(object())

    assert len(ids) == 50
    assert calls["skip_set"] == 1


def test_orphan_ids_includes_a_listing_owed_photos(monkeypatch):
    """Issue #70: a listing with photo URLs and no hosted photos is not in the
    skip set, and until now nothing iterated the difference."""
    import scrape

    monkeypatch.setattr(scrape, "get_listing_ids_missing_fields", lambda *a: [])
    monkeypatch.setattr(
        scrape, "query_listings",
        lambda *a: [{"listing_id": "owed"}, {"listing_id": "done"}],
    )
    monkeypatch.setattr(
        scrape, "listing_ids_with_any_hosted_or_no_urls", lambda *a: frozenset({"done"})
    )

    assert scrape.orphan_ids(object()) == {"owed"}
