"""Photo upload as a stage-level concern.

Upload lived in publish.py. Once Turso is the source of truth and publish.py
is gone, it has to live where the photos are produced -- the scrape stage.

Two invariants carry over and are pinned here, because both were paid for:
the skip set comes from ONE hosted_photos query (a per-photo
already_uploaded() check once cost 12 minutes before the first upload), and
a failed upload records no row, so a rerun retries it.

One is new: recording the results is batched. The old code issued an INSERT
plus a commit per photo -- 3 round-trips each, ~2,600 for an 867-photo run.
"""
import sqlite3
import threading
from pathlib import Path

import pytest

from src.photo_upload import collect_pending_photos, upload_photos
from src.photos import photo_filename
from src.turso_db import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _url(listing_id: str, position: int) -> str:
    """A stable fake source URL per (listing, position), so tests can build
    the same content-keyed filename the pipeline would."""
    return f"https://cdn.example.com/{listing_id}/{position}.jpg"


def _name(listing_id: str, position: int) -> str:
    return photo_filename(position, _url(listing_id, position))


@pytest.fixture
def photos(tmp_path) -> Path:
    for listing_id, count in (("aaa", 3), ("bbb", 2)):
        d = tmp_path / listing_id
        d.mkdir()
        for i in range(1, count + 1):
            (d / _name(listing_id, i)).write_bytes(b"jpeg")
    return tmp_path


def _ok(path, listing_id, position, token):
    return f"https://x.public.blob.vercel-storage.com/photos/{listing_id}/{position:02d}.jpg"


class _Counter:
    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


# --- the skip set ---------------------------------------------------------

def test_pending_list_costs_exactly_one_hosted_photos_query(photos):
    """The 12-minute lesson. One whole-set read, never one per photo."""
    conn = _conn()
    with _Counter(conn) as statements:
        collect_pending_photos(conn, photos, ["aaa", "bbb"])

    reads = [s for s in statements if "hosted_photos" in s]
    assert len(reads) == 1, reads


def test_the_single_query_holds_as_the_corpus_grows(tmp_path):
    conn = _conn()
    for n in range(40):
        d = tmp_path / f"L{n}"
        d.mkdir()
        for i in range(1, 21):
            (d / _name(f"L{n}", i)).write_bytes(b"x")

    with _Counter(conn) as statements:
        pending = collect_pending_photos(conn, tmp_path, [f"L{n}" for n in range(40)])

    assert len(pending) == 800
    assert len([s for s in statements if "hosted_photos" in s]) == 1


def test_already_hosted_photos_are_skipped(photos):
    conn = _conn()
    conn.execute("INSERT INTO hosted_photos VALUES ('aaa', 1, 'https://old')")
    conn.commit()

    pending = collect_pending_photos(conn, photos, ["aaa", "bbb"])

    assert ("aaa", 1) not in [(lid, pos) for lid, pos, _ in pending]
    assert len(pending) == 4


def test_a_listing_with_no_photo_directory_is_skipped(photos):
    assert collect_pending_photos(_conn(), photos, ["missing"]) == []


def test_an_old_format_file_is_never_uploaded(tmp_path):
    """A leftover NN.jpg predates the content-keyed rename and may belong to
    a previous listing at this id -- uploading it is the stale-photo bug."""
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    (d / "02.jpg").write_bytes(b"x")
    (d / "cover.jpg").write_bytes(b"x")

    pending = collect_pending_photos(_conn(), tmp_path, ["aaa"])

    assert [(lid, pos) for lid, pos, _ in pending] == [("aaa", 1)]


def test_the_per_listing_cap_is_honoured(tmp_path):
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 21):
        (d / _name("aaa", i)).write_bytes(b"x")

    pending = collect_pending_photos(_conn(), tmp_path, ["aaa"], max_per_listing=8)

    assert [pos for _, pos, _ in pending] == list(range(1, 9))


def test_a_cap_of_zero_means_no_cap(tmp_path):
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 13):
        (d / _name("aaa", i)).write_bytes(b"x")

    assert len(collect_pending_photos(_conn(), tmp_path, ["aaa"], max_per_listing=0)) == 12


# --- uploading and recording ---------------------------------------------

def test_every_pending_photo_uploads_and_is_recorded(photos):
    conn = _conn()

    uploaded, failed = upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=_ok)

    assert (uploaded, failed) == (5, 0)
    rows = conn.execute(
        "SELECT listing_id, position, blob_url FROM hosted_photos ORDER BY listing_id, position"
    ).fetchall()
    assert [(r["listing_id"], r["position"]) for r in rows] == [
        ("aaa", 1), ("aaa", 2), ("aaa", 3), ("bbb", 1), ("bbb", 2)
    ]
    assert rows[0]["blob_url"].endswith("/photos/aaa/01.jpg")


def test_a_failed_upload_records_no_row_so_a_rerun_retries_it(photos):
    def flaky(path, lid, pos, token):
        if lid == "aaa" and pos == 2:
            raise RuntimeError("blob upload failed: 500")
        return _ok(path, lid, pos, token)

    conn = _conn()
    uploaded, failed = upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=flaky)

    assert (uploaded, failed) == (4, 1)
    recorded = {(r["listing_id"], r["position"]) for r in conn.execute("SELECT * FROM hosted_photos")}
    assert ("aaa", 2) not in recorded, "a failed upload must leave no row behind"


def test_rerunning_after_a_full_upload_does_nothing(photos):
    conn = _conn()
    upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=_ok)

    calls = []

    def track(path, lid, pos, token):
        calls.append((lid, pos))
        return _ok(path, lid, pos, token)

    uploaded, failed = upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=track)

    assert (uploaded, failed) == (0, 0)
    assert calls == [], "idempotent: nothing should re-upload"


def test_a_retry_after_a_failure_uploads_only_the_failed_photo(photos):
    conn = _conn()
    state = {"fail": True}

    def flaky(path, lid, pos, token):
        if state["fail"] and lid == "aaa" and pos == 2:
            raise RuntimeError("500")
        return _ok(path, lid, pos, token)

    upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=flaky)
    state["fail"] = False

    calls = []

    def track(path, lid, pos, token):
        calls.append((lid, pos))
        return _ok(path, lid, pos, token)

    uploaded, failed = upload_photos(conn, photos, ["aaa", "bbb"], "tok", upload_fn=track)

    assert (uploaded, failed) == (1, 0)
    assert calls == [("aaa", 2)]


def test_recording_is_batched_not_one_statement_per_photo(tmp_path):
    """The old shape was INSERT + commit per photo -- 3 round-trips each,
    about 2,600 for an 867-photo run."""
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 121):
        (d / _name("aaa", i)).write_bytes(b"x")
    conn = _conn()

    with _Counter(conn) as statements:
        uploaded, failed = upload_photos(conn, tmp_path, ["aaa"], "tok", upload_fn=_ok)

    assert (uploaded, failed) == (120, 0)
    writes = [s for s in statements if s.strip().upper().startswith("INSERT")]
    assert len(writes) < 10, f"{len(writes)} inserts for 120 photos"


def test_no_pending_photos_is_a_clean_no_op(tmp_path):
    conn = _conn()

    uploaded, failed = upload_photos(
        conn, tmp_path, ["missing"], "tok",
        upload_fn=lambda *a: pytest.fail("should not upload"),
    )

    assert (uploaded, failed) == (0, 0)


def test_the_shared_connection_is_never_used_from_a_worker_thread(photos):
    """Uploads run in parallel; the connection must stay on the main thread."""
    main_thread = threading.current_thread().ident
    inner = _conn()

    class ThreadGuarded:
        def __init__(self, c):
            self._c = c

        def _check(self):
            assert threading.current_thread().ident == main_thread, (
                "the shared connection was used from a worker thread"
            )

        def execute(self, *a, **k):
            self._check()
            return self._c.execute(*a, **k)

        def commit(self, *a, **k):
            self._check()
            return self._c.commit(*a, **k)

        def __enter__(self):
            self._check()
            return self

        def __exit__(self, *exc):
            self._c.commit()
            return False

    seen = []

    def record_thread(path, lid, pos, token):
        seen.append(threading.current_thread().ident)
        return _ok(path, lid, pos, token)

    uploaded, failed = upload_photos(
        ThreadGuarded(inner), photos, ["aaa", "bbb"], "tok", upload_fn=record_thread
    )

    assert (uploaded, failed) == (5, 0)
    assert any(t != main_thread for t in seen), "uploads should run on worker threads"


def test_scrape_uploads_photos_within_its_own_stage():
    """Acceptance criterion for #21: the scrape stage, not publish.py."""
    source = Path("scrape.py").read_text()
    assert "upload_photos" in source
