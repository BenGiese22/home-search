"""Tests for publish.py's parallel photo upload.

publish.py is otherwise deliberately untested -- it is thin orchestration
plus real network calls, matching score_photos.py/compute_commutes.py. The
upload path is the exception: it runs work across threads while sharing one
Turso connection, and it carries an idempotency invariant (a failed upload
must record no hosted_photos row, so a rerun retries it). That is real
logic, and it is testable without touching the network.
"""
import sqlite3
from pathlib import Path

import pytest

import publish
from src.turso_sync import ensure_schema


def _conn() -> sqlite3.Connection:
    # check_same_thread=False mirrors the shared-connection shape; the test
    # asserts we never actually use it off the main thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


@pytest.fixture
def photos(tmp_path, monkeypatch):
    """Two listings, three photos, laid out the way the pipeline writes them."""
    for listing_id, count in (("aaa", 2), ("bbb", 1)):
        d = tmp_path / listing_id
        d.mkdir()
        for i in range(1, count + 1):
            (d / f"{i:02d}.jpg").write_bytes(b"jpeg")
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)
    return tmp_path


def test_uploads_every_pending_photo_and_records_each_url(photos, monkeypatch):
    monkeypatch.setattr(
        publish, "upload_photo",
        lambda path, lid, pos, token: f"https://x.public.blob.vercel-storage.com/photos/{lid}/{pos:02d}.jpg",
    )
    conn = _conn()

    uploaded, failed = publish._upload_new_photos(conn, ["aaa", "bbb"], "tok")

    assert (uploaded, failed) == (3, 0)
    rows = conn.execute("SELECT listing_id, position, blob_url FROM hosted_photos ORDER BY listing_id, position").fetchall()
    assert [(r["listing_id"], r["position"]) for r in rows] == [("aaa", 1), ("aaa", 2), ("bbb", 1)]
    assert rows[0]["blob_url"].endswith("/photos/aaa/01.jpg")


def test_a_failed_upload_records_no_row_so_a_rerun_retries_it(photos, monkeypatch):
    def flaky(path, lid, pos, token):
        if lid == "aaa" and pos == 2:
            raise RuntimeError("vercel blob put failed: 500")
        return f"https://x.public.blob.vercel-storage.com/photos/{lid}/{pos:02d}.jpg"

    monkeypatch.setattr(publish, "upload_photo", flaky)
    conn = _conn()

    uploaded, failed = publish._upload_new_photos(conn, ["aaa", "bbb"], "tok")

    assert (uploaded, failed) == (2, 1)
    recorded = {(r["listing_id"], r["position"]) for r in conn.execute("SELECT * FROM hosted_photos")}
    assert ("aaa", 2) not in recorded, "a failed upload must leave no row behind"
    assert recorded == {("aaa", 1), ("bbb", 1)}


def test_already_uploaded_photos_are_skipped(photos, monkeypatch):
    conn = _conn()
    conn.execute("INSERT INTO hosted_photos VALUES ('aaa', 1, 'https://old')")
    conn.commit()
    calls = []

    def track(path, lid, pos, token):
        calls.append((lid, pos))
        return "https://x.public.blob.vercel-storage.com/new.jpg"

    monkeypatch.setattr(publish, "upload_photo", track)

    uploaded, failed = publish._upload_new_photos(conn, ["aaa", "bbb"], "tok")

    assert (uploaded, failed) == (2, 0)
    assert ("aaa", 1) not in calls, "an already-hosted photo must not be re-uploaded"
    assert conn.execute("SELECT blob_url FROM hosted_photos WHERE listing_id='aaa' AND position=1").fetchone()[0] == "https://old"


def test_the_shared_connection_is_never_used_from_a_worker_thread(photos, monkeypatch):
    """The whole reason for the three-phase split. If an upload ever touched
    the Turso connection directly, this would catch it."""
    import threading

    main_thread = threading.current_thread().ident

    class ThreadGuardedConn:
        """Wraps the real connection and asserts every use is on the main thread."""

        def __init__(self, inner):
            self._inner = inner

        def _check(self):
            assert threading.current_thread().ident == main_thread, (
                "the shared Turso connection was used from a worker thread"
            )

        def execute(self, *args, **kwargs):
            self._check()
            return self._inner.execute(*args, **kwargs)

        def commit(self, *args, **kwargs):
            self._check()
            return self._inner.commit(*args, **kwargs)

    seen_threads = []

    def record_thread(path, lid, pos, token):
        seen_threads.append(threading.current_thread().ident)
        return f"https://x.public.blob.vercel-storage.com/photos/{lid}/{pos:02d}.jpg"

    monkeypatch.setattr(publish, "upload_photo", record_thread)
    guarded = ThreadGuardedConn(_conn())

    uploaded, failed = publish._upload_new_photos(guarded, ["aaa", "bbb"], "tok")

    assert (uploaded, failed) == (3, 0)
    assert seen_threads, "uploads should have run"
    assert any(t != main_thread for t in seen_threads), (
        "uploads should run on worker threads, otherwise this is still serial"
    )


def test_no_pending_photos_is_a_clean_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(publish, "upload_photo", lambda *a: pytest.fail("should not upload"))

    assert publish._upload_new_photos(_conn(), ["missing"], "tok") == (0, 0)


def test_pending_list_costs_one_query_not_one_per_photo(photos):
    """The reason _collect_pending_photos exists in this shape. Against
    hosted Turso each query is a ~240ms HTTP round-trip, so a per-photo
    already_uploaded() check cost ~12 minutes on ~3000 photos before the
    first upload could start. This asserts we read hosted_photos once."""
    conn = _conn()
    queries = []
    inner_execute = conn.execute

    class CountingConn:
        def execute(self, sql, *args, **kwargs):
            queries.append(sql)
            return inner_execute(sql, *args, **kwargs)

        def commit(self, *a, **k):
            return conn.commit(*a, **k)

    pending = publish._collect_pending_photos(CountingConn(), ["aaa", "bbb"])

    assert len(pending) == 3
    hosted_reads = [q for q in queries if "hosted_photos" in q]
    assert len(hosted_reads) == 1, f"expected 1 hosted_photos read, got {len(hosted_reads)}"


def test_a_photo_named_oddly_is_skipped_not_fatal(tmp_path, monkeypatch):
    d = tmp_path / "aaa"; d.mkdir()
    (d / "01.jpg").write_bytes(b"x")
    (d / "cover.jpg").write_bytes(b"x")
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)

    pending = publish._collect_pending_photos(_conn(), ["aaa"])

    assert [(lid, pos) for lid, pos, _ in pending] == [("aaa", 1)]


def test_photo_count_is_capped_per_listing(tmp_path, monkeypatch):
    """Vercel bills each upload as an Advanced Operation, and the Hobby tier
    includes 2,000/month. Backfilling every local photo blew that budget on
    the first real sync, so only the first N per listing get hosted."""
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 21):
        (d / f"{i:02d}.jpg").write_bytes(b"x")
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(publish, "MAX_PHOTOS_PER_LISTING", 8)

    pending = publish._collect_pending_photos(_conn(), ["aaa"])

    assert [pos for _, pos, _ in pending] == list(range(1, 9))


def test_a_cap_of_zero_means_no_cap(tmp_path, monkeypatch):
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 13):
        (d / f"{i:02d}.jpg").write_bytes(b"x")
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(publish, "MAX_PHOTOS_PER_LISTING", 0)

    assert len(publish._collect_pending_photos(_conn(), ["aaa"])) == 12


def test_prunable_tables_deletes_children_before_listings():
    """Turso enforces foreign keys that local SQLite does not. Pruning the
    parent row first aborts the whole publish, so `listings` must be last."""
    from publish import PRUNABLE_TABLES

    assert PRUNABLE_TABLES[-1] == "listings"
    for child in ("commute", "scores", "visual_scores", "amenities", "photo_urls"):
        assert PRUNABLE_TABLES.index(child) < PRUNABLE_TABLES.index("listings")


def test_prune_deleted_listings_succeeds_with_foreign_keys_enforced():
    """Regression: reproduces the live failure by turning FK enforcement on,
    which is how Turso behaves and how local sqlite3 does not."""
    import sqlite3

    from publish import _prune_deleted_listings
    from src.db import _SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS hosted_photos (
               listing_id TEXT NOT NULL, position INTEGER NOT NULL,
               blob_url TEXT NOT NULL, PRIMARY KEY (listing_id, position));"""
    )
    conn.execute("PRAGMA foreign_keys = ON")
    for lid in ("keep", "drop"):
        conn.execute(
            "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
            "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
            "listing_url) VALUES (?, 'a', 'b', 'CO', '80020', '$1', 1, 1, 1, 1, 1, 1, 'd', 'u')",
            (lid,),
        )
        conn.execute("INSERT INTO amenities (listing_id, amenity) VALUES (?, 'Pool')", (lid,))
    conn.commit()

    pruned = _prune_deleted_listings(conn, ["keep"])

    assert pruned == 2  # the dropped listing's own row plus its amenity
    assert [r[0] for r in conn.execute("SELECT listing_id FROM listings")] == ["keep"]
    assert [r[0] for r in conn.execute("SELECT listing_id FROM amenities")] == ["keep"]


# --- the photo cap default (issue #17) -----------------------------------

def test_the_default_cap_is_uncapped():
    """The cap existed because Vercel's Hobby tier included only 2,000
    Advanced Operations per month and a 3,000-photo backfill once consumed
    11,000, suspending the store for 30 days. On Pro that no longer binds,
    and the cap's only remaining effect was hosted galleries showing a
    fraction of each listing's photos."""
    assert publish.DEFAULT_MAX_PHOTOS_PER_LISTING == 0


def test_the_env_override_is_retained(monkeypatch):
    """Uncapped by default, but still capped on demand -- and the value must
    come through the merged lookup, so process env wins over .env."""
    import importlib

    monkeypatch.setenv("MAX_PHOTOS_PER_LISTING", "5")
    reloaded = importlib.reload(publish)
    try:
        assert reloaded.MAX_PHOTOS_PER_LISTING == 5
    finally:
        monkeypatch.delenv("MAX_PHOTOS_PER_LISTING", raising=False)
        importlib.reload(publish)


def test_an_uncapped_run_collects_every_photo(tmp_path, monkeypatch):
    """The behaviour the default now selects: a listing with 40 photos gets
    all 40 hosted, not the first 8."""
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 41):
        (d / f"{i:02d}.jpg").write_bytes(b"x")
    monkeypatch.setattr(publish, "PHOTOS_DIR", tmp_path)
    monkeypatch.setattr(publish, "MAX_PHOTOS_PER_LISTING", publish.DEFAULT_MAX_PHOTOS_PER_LISTING)

    pending = publish._collect_pending_photos(_conn(), ["aaa"])

    assert len(pending) == 40
