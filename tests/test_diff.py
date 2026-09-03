from pathlib import Path

import pytest

from src.db import get_connection, query_listings, upsert_listing
from src.turso_db import ensure_schema
from src.diff import (
    collection_fetch_is_trustworthy,
    apply_delisting,
    compute_changes,
    format_report,
    run_delisting,
    should_apply_delisting,
)
from src.models import Listing

SAMPLE = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$650,000",
    beds=4,
    baths=2.5,
    sqft=2200,
    lot_sqft=5000,
    parking_spaces=2,
    year_built=2005,
    description="desc",
    amenities=["Garage", "Central AC"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_listing_not_in_snapshot_is_new():
    report = compute_changes([SAMPLE], before={})

    assert report.new_listings == [SAMPLE]
    assert report.price_changes == []


def test_listing_with_same_price_is_unchanged():
    before = {"abc123": ("$650,000", 650000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert report.price_changes == []


def test_listing_with_different_price_is_a_price_change():
    before = {"abc123": ("$600,000", 600000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert len(report.price_changes) == 1
    change = report.price_changes[0]
    assert change.listing == SAMPLE
    assert change.old_price == "$600,000"
    assert change.new_price == "$650,000"


def test_format_report_lists_new_listing_address_and_url():
    report = compute_changes([SAMPLE], before={})

    text = format_report(report)

    assert "1 new listing" in text
    assert "1 Test St" in text
    assert "https://example.com/listing/abc123" in text


def test_format_report_lists_price_change_old_and_new():
    before = {"abc123": ("$600,000", 600000.0)}
    report = compute_changes([SAMPLE], before)

    text = format_report(report)

    assert "1 price change" in text
    assert "$600,000" in text
    assert "$650,000" in text


def test_duplicate_listing_id_in_fetched_is_reported_once():
    report = compute_changes([SAMPLE, SAMPLE], before={})

    assert report.new_listings == [SAMPLE]


def test_format_report_no_changes_reads_clean():
    report = compute_changes([SAMPLE], before={"abc123": ("$650,000", 650000.0)})

    text = format_report(report)

    assert "0 new listing" in text
    assert "0 price change" in text


def test_listing_in_snapshot_but_not_fetched_is_delisted():
    before = {"abc123": ("$650,000", 650000.0), "gone456": ("$500,000", 500000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == ["gone456"]


def test_no_delisted_ids_when_all_snapshot_listings_still_fetched():
    before = {"abc123": ("$650,000", 650000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == []


def test_delisted_ids_sorted_for_deterministic_output():
    before = {
        "abc123": ("$650,000", 650000.0),
        "zzz999": ("$1", 1.0),
        "aaa111": ("$1", 1.0),
    }

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == ["aaa111", "zzz999"]


def test_format_report_lists_delisted_count_and_ids():
    before = {"abc123": ("$650,000", 650000.0), "gone456": ("$500,000", 500000.0)}
    report = compute_changes([SAMPLE], before)

    text = format_report(report)

    assert "1 delisted" in text
    assert "gone456" in text


def test_format_report_no_delisted_reads_clean():
    report = compute_changes([SAMPLE], before={"abc123": ("$650,000", 650000.0)})

    text = format_report(report)

    assert "0 delisted" in text


def test_pinned_id_absent_from_fetched_is_not_delisted():
    before = {"abc123": ("$650,000", 650000.0), "pinned789": ("$1", 1.0)}

    report = compute_changes([SAMPLE], before, pinned_ids=frozenset({"pinned789"}))

    assert report.delisted_ids == []


def test_pinned_id_does_not_suppress_other_delisted_ids():
    before = {
        "abc123": ("$650,000", 650000.0),
        "pinned789": ("$1", 1.0),
        "gone456": ("$500,000", 500000.0),
    }

    report = compute_changes([SAMPLE], before, pinned_ids=frozenset({"pinned789"}))

    assert report.delisted_ids == ["gone456"]


def test_apply_delisting_removes_db_row_photos_and_json(tmp_path: Path):
    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    photos_dir = tmp_path / "photos"
    listing_photo_dir = photos_dir / "abc123"
    listing_photo_dir.mkdir(parents=True)
    (listing_photo_dir / "01.jpg").write_bytes(b"x")
    store_dir = tmp_path / "listings"
    store_dir.mkdir()
    (store_dir / "abc123.json").write_text("{}")

    apply_delisting(conn, photos_dir, store_dir, ["abc123"])

    assert query_listings(conn) == []
    assert not listing_photo_dir.exists()
    assert not (store_dir / "abc123.json").exists()


def test_apply_delisting_handles_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "listings.db")

    apply_delisting(conn, tmp_path / "photos", tmp_path / "listings", [])  # should not raise


def test_apply_delisting_continues_past_a_failure(tmp_path: Path, monkeypatch, capsys):
    from src import diff as diff_module

    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)

    # Delisting now takes a batched fast path; this exercises the per-listing
    # fallback that preserves "one bad listing must not strand the rest".
    def failing_bulk(conn, listing_ids):
        raise Exception("database is locked")

    monkeypatch.setattr(diff_module, "bulk_delete_listings", failing_bulk)

    real_delete_listing = diff_module.delete_listing
    calls = []

    def flaky_delete_listing(conn, listing_id):
        calls.append(listing_id)
        if listing_id == "abc123":
            raise Exception("database is locked")
        real_delete_listing(conn, listing_id)

    monkeypatch.setattr(diff_module, "delete_listing", flaky_delete_listing)

    apply_delisting(conn, tmp_path / "photos", tmp_path / "listings", ["abc123", "other456"])

    assert calls == ["abc123", "other456"]  # both attempted, one failing doesn't stop the other
    assert [row["listing_id"] for row in query_listings(conn)] == ["abc123"]  # other456 removed
    assert "abc123" in capsys.readouterr().out


def test_should_apply_delisting_false_when_fetch_failed():
    before = {"abc123": ("$1", 1.0), "gone456": ("$1", 1.0)}
    report = compute_changes([], before)  # would look like both are delisted

    assert should_apply_delisting(False, report, before, frozenset()) is False


def test_should_apply_delisting_true_for_a_normal_small_delist():
    # 1 of 5 known listings dropped out -- an ordinary, plausible delisting
    before = {f"id{i}": ("$1", 1.0) for i in range(5)}
    fetched_ids = {f"id{i}" for i in range(4)}
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": lid}) for lid in fetched_ids]
    report = compute_changes(fetched, before)

    assert should_apply_delisting(True, report, before, frozenset()) is True


def test_should_apply_delisting_false_when_almost_everything_looks_delisted():
    # A "successful" fetch that returned nothing -- the exact shape of the
    # bug where an empty-but-200-OK collection response would otherwise
    # wipe every known listing.
    before = {f"id{i}": ("$1", 1.0) for i in range(5)}
    report = compute_changes([], before)

    assert should_apply_delisting(True, report, before, frozenset()) is False


def test_should_apply_delisting_true_when_no_listings_known_yet():
    assert should_apply_delisting(True, compute_changes([], {}), {}, frozenset()) is True


def test_should_apply_delisting_excludes_pinned_ids_from_the_denominator():
    # 1 of 2 *eligible* (non-pinned) listings delisted -- the pinned
    # listing must not count toward "everything known" or appear in
    # delisted_ids itself.
    before = {
        "pinned1": ("$1", 1.0),
        "stays123": ("$1", 1.0),
        "gone456": ("$1", 1.0),
    }
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": "stays123"})]
    report = compute_changes(fetched, before, pinned_ids=frozenset({"pinned1"}))

    assert report.delisted_ids == ["gone456"]
    # eligible = {stays123, gone456} = 2; delisted = 1 -> 0.5, at the
    # threshold (<=), so this is still considered safe.
    assert should_apply_delisting(True, report, before, frozenset({"pinned1"})) is True


def test_should_apply_delisting_false_for_total_wipeout_even_with_few_listings():
    # Regression guard: a small total collection (here, 3 tracked
    # listings) with a "successful" but empty/anomalous fetch must not be
    # waved through just because the absolute delisted count is small --
    # 100% of a tiny collection is exactly as suspicious as 100% of a
    # large one.
    before = {"a": ("$1", 1.0), "b": ("$1", 1.0), "c": ("$1", 1.0)}
    report = compute_changes([], before)

    assert should_apply_delisting(True, report, before, frozenset()) is False


def test_run_delisting_removes_listings_when_safe(tmp_path: Path):
    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)
    before = {"abc123": ("$1", 1.0), "other456": ("$1", 1.0)}
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": "other456"})]  # abc123 delisted
    report = compute_changes(fetched, before)

    run_delisting(conn, tmp_path / "photos", tmp_path / "listings", True, report, before, frozenset())

    assert [row["listing_id"] for row in query_listings(conn)] == ["other456"]


def test_run_delisting_skips_and_prints_when_unsafe(tmp_path: Path, capsys):
    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    before = {"abc123": ("$1", 1.0)}
    report = compute_changes([], before)

    run_delisting(conn, tmp_path / "photos", tmp_path / "listings", False, report, before, frozenset())

    assert [row["listing_id"] for row in query_listings(conn)] == ["abc123"]
    assert "1" in capsys.readouterr().out


def test_run_delisting_prints_nothing_when_nothing_delisted(tmp_path: Path, capsys):
    conn = get_connection(tmp_path / "listings.db")
    report = compute_changes([], {})

    run_delisting(conn, tmp_path / "photos", tmp_path / "listings", True, report, {}, frozenset())

    assert capsys.readouterr().out == ""


def test_files_are_not_removed_for_a_listing_whose_db_delete_failed(tmp_path, monkeypatch):
    """A listing whose row survives must keep its photos. Deleting them while
    the listing is still tracked leaves it image-less with nothing to signal
    why -- and the JSON store is what a re-scrape checks to decide whether the
    listing needs fetching at all."""
    from src import diff as diff_module

    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)

    photos_dir = tmp_path / "photos" / SAMPLE.listing_id
    photos_dir.mkdir(parents=True)
    (photos_dir / "01.jpg").write_bytes(b"x")

    def failing_bulk(conn, listing_ids):
        raise Exception("database is locked")

    def failing_single(conn, listing_id):
        raise Exception("database is locked")

    monkeypatch.setattr(diff_module, "bulk_delete_listings", failing_bulk)
    monkeypatch.setattr(diff_module, "delete_listing", failing_single)

    apply_delisting(conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id])

    assert (photos_dir / "01.jpg").exists(), "photos removed for a listing still in the db"
    assert [row["listing_id"] for row in query_listings(conn)] == [SAMPLE.listing_id]


# --- multi-tab fetch trust -------------------------------------------------
#
# The delisting cascade acts on "listings that were tracked and are no longer
# in the fetch". With two tabs, a tab that merely failed produces exactly that
# input for its whole bucket, so the trust gate below is what stands between a
# flaky request and deleting every favorite's rows, photos and scores.

class StubFetch:
    def __init__(self, counts, errors=None):
        self.counts = counts
        self.errors = errors or {}


MATCH_IDS = [f"m{i}" for i in range(149)]
FAV_ONLY_IDS = [f"f{i}" for i in range(10)]


def _snapshot(ids):
    return {lid: ("$650,000", 650000.0) for lid in ids}


def test_every_tab_ok_is_trustworthy():
    fetch = StubFetch(counts={"favorites": 26, "matches": 149})
    assert collection_fetch_is_trustworthy(fetch, _snapshot(MATCH_IDS)) is True


def test_any_failed_tab_is_not_trustworthy():
    fetch = StubFetch(counts={"matches": 149}, errors={"favorites": "boom"})
    assert collection_fetch_is_trustworthy(fetch, _snapshot(MATCH_IDS)) is False


def test_empty_tab_with_listings_already_tracked_is_not_trustworthy():
    """A 200 OK returning zero listings is the failure the fraction guard was
    built for, but an empty favorites tab is only ~6% of everything tracked --
    invisible to a global fraction."""
    fetch = StubFetch(counts={"favorites": 0, "matches": 149})
    assert collection_fetch_is_trustworthy(fetch, _snapshot(MATCH_IDS)) is False


def test_empty_tab_on_a_cold_start_is_trustworthy():
    fetch = StubFetch(counts={"favorites": 0, "matches": 0})
    assert collection_fetch_is_trustworthy(fetch, before={}) is True


def test_failed_favorites_tab_cannot_delist_the_favorites_bucket():
    """The regression guard. Favorites fails, matches succeeds: all 10
    favorites-only listings look delisted, and 10/159 = 6% sails under
    MAX_DELISTED_FRACTION. Only the trust gate stops the wipe."""
    before = _snapshot(MATCH_IDS + FAV_ONLY_IDS)
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": lid}) for lid in MATCH_IDS]
    report = compute_changes(fetched, before)

    assert set(report.delisted_ids) == set(FAV_ONLY_IDS)
    # The fraction check alone would happily allow it ...
    assert should_apply_delisting(True, report, before, frozenset()) is True
    # ... so the trust gate is what has to say no.
    fetch = StubFetch(counts={"matches": 149}, errors={"favorites": "boom"})
    trustworthy = collection_fetch_is_trustworthy(fetch, before)
    assert should_apply_delisting(trustworthy, report, before, frozenset()) is False


def test_failed_matches_tab_cannot_delist_either():
    """The mirror case survives today only by the accident that 149/159 trips
    the fraction. Do not rely on that proportion holding."""
    before = _snapshot(MATCH_IDS + FAV_ONLY_IDS)
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": lid}) for lid in FAV_ONLY_IDS]
    report = compute_changes(fetched, before)
    fetch = StubFetch(counts={"favorites": 10}, errors={"matches": "boom"})
    trustworthy = collection_fetch_is_trustworthy(fetch, before)
    assert should_apply_delisting(trustworthy, report, before, frozenset()) is False


def test_first_run_with_favorites_adds_without_delisting():
    """Rolling this out is purely additive: favorites only widen the fetched
    set, so nothing looks absent and the breaker never engages."""
    before = _snapshot(MATCH_IDS)
    fetched = [
        Listing(**{**SAMPLE.__dict__, "listing_id": lid})
        for lid in FAV_ONLY_IDS + MATCH_IDS
    ]
    report = compute_changes(fetched, before)

    assert {l.listing_id for l in report.new_listings} == set(FAV_ONLY_IDS)
    assert report.delisted_ids == []
    fetch = StubFetch(counts={"favorites": 26, "matches": 149})
    assert should_apply_delisting(
        collection_fetch_is_trustworthy(fetch, before), report, before, frozenset()
    ) is True


# --- delisting reclaims the blobs ------------------------------------------
#
# hosted_photos.blob_url is the only record of what exists in the Vercel Blob
# store, so pruning the rows without deleting the blobs strands them forever.
# 1,813 orphans (~371 MB) accumulated that way. The rule the tests below pin:
# never delete a hosted_photos row without first deleting or printing its blob.

def _hosted_conn(tmp_path: Path):
    """A connection carrying the hosted schema, so hosted_photos exists --
    the local schema has no such table."""
    conn = get_connection(tmp_path / "listings.db")
    ensure_schema(conn)
    return conn


def _host(conn, listing_id: str, position: int, blob_url: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hosted_photos "
        "(listing_id, position, blob_url, source_url) VALUES (?, ?, ?, ?)",
        (listing_id, position, blob_url, f"https://cdn/{listing_id}/{position}.jpg"),
    )
    conn.commit()


def test_delisting_deletes_the_listings_blobs(tmp_path: Path):
    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")
    _host(conn, SAMPLE.listing_id, 2, "https://blob/b.jpg")
    deleted = []

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token="tok",
        delete_fn=lambda urls, token: deleted.extend(urls),
    )

    assert sorted(deleted) == ["https://blob/a.jpg", "https://blob/b.jpg"]
    assert conn.execute("SELECT * FROM hosted_photos").fetchall() == []


def test_the_blob_urls_are_read_in_one_statement(tmp_path: Path):
    """Every Turso statement is a ~240ms round-trip; a per-listing read here
    would put the delisting cascade back where the batched delete rescued it
    from."""
    conn = _hosted_conn(tmp_path)
    ids = []
    for n in range(20):
        listing_id = f"L{n:04d}"
        ids.append(listing_id)
        upsert_listing(conn, Listing(**{**SAMPLE.__dict__, "listing_id": listing_id}))
        _host(conn, listing_id, 1, f"https://blob/{n}.jpg")

    statements = []
    conn.set_trace_callback(statements.append)
    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", ids,
        blob_token="tok", delete_fn=lambda urls, token: None,
    )
    conn.set_trace_callback(None)

    reads = [s for s in statements if s.strip().upper().startswith("SELECT")
             and "hosted_photos" in s]
    assert len(reads) == 1, reads


def test_the_rows_go_before_the_blobs(tmp_path: Path):
    """Deliberate ordering. A row pointing at a blob that no longer exists is
    a broken image in the viewer; a blob with no row is just wasted bytes."""
    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")
    rows_at_delete_time = []

    def delete_fn(urls, token):
        rows_at_delete_time.extend(conn.execute("SELECT * FROM hosted_photos"))

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token="tok", delete_fn=delete_fn,
    )

    assert rows_at_delete_time == []


def test_a_failed_blob_delete_prints_the_urls_and_does_not_raise(tmp_path, capsys):
    """A stranded blob is cheap; a failed pipeline run is not. But the URLs
    are the only handle left on those blobs, so they get printed."""
    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")

    def boom(urls, token):
        raise RuntimeError("HTTP 500")

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token="tok", delete_fn=boom,
    )

    out = capsys.readouterr().out
    assert "https://blob/a.jpg" in out
    assert query_listings(conn) == []


def test_without_a_blob_token_the_urls_are_printed_not_dropped(tmp_path, capsys):
    """A --skip-photos dev run has no RW token. The rows still go, so the
    URLs have to survive somewhere a human can see them."""
    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token=None,
        delete_fn=lambda urls, token: pytest.fail("must not delete without a token"),
    )

    assert "https://blob/a.jpg" in capsys.readouterr().out


def test_blobs_are_kept_for_a_listing_whose_row_survived(tmp_path, monkeypatch):
    """Same rule as the photos on disk: if the listing is still tracked, its
    images must stay. Deleting them would leave a live listing broken."""
    from src import diff as diff_module

    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")

    def failing(conn, *args):
        raise Exception("database is locked")

    monkeypatch.setattr(diff_module, "bulk_delete_listings", failing)
    monkeypatch.setattr(diff_module, "delete_listing", failing)
    deleted = []

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token="tok", delete_fn=lambda urls, token: deleted.extend(urls),
    )

    assert deleted == []


def test_delisting_without_a_hosted_photos_table_still_works(tmp_path: Path):
    """The local schema has no hosted_photos; the blob read must not be the
    thing that breaks a local run."""
    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)

    apply_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", [SAMPLE.listing_id],
        blob_token="tok",
        delete_fn=lambda urls, token: pytest.fail("nothing hosted to delete"),
    )

    assert query_listings(conn) == []


def test_run_delisting_passes_the_blob_token_through(tmp_path: Path):
    conn = _hosted_conn(tmp_path)
    upsert_listing(conn, SAMPLE)
    _host(conn, SAMPLE.listing_id, 1, "https://blob/a.jpg")
    before = {SAMPLE.listing_id: (SAMPLE.price, 650000.0), "kept": ("$1", 1.0)}
    report = compute_changes(
        [Listing(**{**SAMPLE.__dict__, "listing_id": "kept"})], before
    )
    seen = []

    run_delisting(
        conn, tmp_path / "photos", tmp_path / "listings", True, report, before,
        frozenset(), blob_token="tok",
        delete_fn=lambda urls, token: seen.append((urls, token)),
    )

    assert seen == [(["https://blob/a.jpg"], "tok")]
