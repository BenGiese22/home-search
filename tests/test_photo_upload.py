"""Photo upload as a stage-level concern.

Upload lived in publish.py. Once Turso is the source of truth and publish.py
is gone, it has to live where the photos are produced -- the scrape stage.

Three invariants carry over and are pinned here, because all three were paid
for: the skip set comes from ONE hosted_photos query (a per-photo
already_uploaded() check once cost 12 minutes before the first upload), a
failed upload records no row so a rerun retries it, and recording is batched
(the old code issued an INSERT plus a commit per photo -- 3 round-trips each,
~2,600 for an 867-photo run).

A fourth is new. The skip key is `(listing_id, position, source_url)`, not
`(listing_id, position)`: 6085 West 82nd Drive relisted under the same id
with 44 different photos in the same positions, every positional key
matched, and the viewer would have kept serving the previous listing's
images with nothing failing.
"""
import sqlite3
import threading
from pathlib import Path

import pytest

from src.photo_upload import (
    collect_pending_photos,
    get_photo_urls_by_listing,
    upload_photos,
)
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


def _urls(**counts: int) -> dict[str, list[str]]:
    return {
        listing_id: [_url(listing_id, i) for i in range(1, n + 1)]
        for listing_id, n in counts.items()
    }


@pytest.fixture
def photos(tmp_path) -> Path:
    for listing_id, count in (("aaa", 3), ("bbb", 2)):
        d = tmp_path / listing_id
        d.mkdir()
        for i in range(1, count + 1):
            (d / _name(listing_id, i)).write_bytes(b"jpeg")
    return tmp_path


@pytest.fixture
def urls() -> dict[str, list[str]]:
    return _urls(aaa=3, bbb=2)


def _ok(path, listing_id, position, token, source_url):
    return (
        "https://x.public.blob.vercel-storage.com/photos/"
        f"{listing_id}/{photo_filename(position, source_url)}"
    )


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


def _listing_row(conn, listing_id: str) -> None:
    conn.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
        "listing_url) VALUES (?, 'a', 'Arvada', 'CO', '80003', '$1', 3, 2.0, 1, 1, "
        "2, 2000, 'd', 'u')",
        (listing_id,),
    )


def _host(conn, listing_id, position, source_url, blob_url):
    conn.execute(
        "INSERT OR REPLACE INTO hosted_photos "
        "(listing_id, position, blob_url, source_url) VALUES (?, ?, ?, ?)",
        (listing_id, position, blob_url, source_url),
    )
    conn.commit()


# --- the skip set ---------------------------------------------------------

def test_pending_list_costs_exactly_one_hosted_photos_query(photos, urls):
    """The 12-minute lesson. One whole-set read, never one per photo."""
    conn = _conn()
    with _Counter(conn) as statements:
        collect_pending_photos(conn, photos, urls)

    reads = [s for s in statements if "hosted_photos" in s]
    assert len(reads) == 1, reads


def test_the_single_query_holds_as_the_corpus_grows(tmp_path):
    conn = _conn()
    for n in range(40):
        d = tmp_path / f"L{n}"
        d.mkdir()
        for i in range(1, 21):
            (d / _name(f"L{n}", i)).write_bytes(b"x")
    many = _urls(**{f"L{n}": 20 for n in range(40)})

    with _Counter(conn) as statements:
        pending = collect_pending_photos(conn, tmp_path, many)

    assert len(pending) == 800
    assert len([s for s in statements if "hosted_photos" in s]) == 1


def test_already_hosted_photos_are_skipped(photos, urls):
    conn = _conn()
    _host(conn, "aaa", 1, _url("aaa", 1), "https://old")

    pending = collect_pending_photos(conn, photos, urls)

    assert ("aaa", 1) not in [(p.listing_id, p.position) for p in pending]
    assert len(pending) == 4


def test_a_position_whose_url_changed_is_pending_again(tmp_path):
    """The relist case. hosted_photos already has position 3, but for the
    PREVIOUS listing's photo -- the positional key alone would skip it."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 3)).write_bytes(b"x")
    _host(conn, "aaa", 3, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old")

    pending = collect_pending_photos(
        conn, tmp_path, {"aaa": ["gone-1", "gone-2", _url("aaa", 3)]}
    )

    assert [(p.listing_id, p.position) for p in pending] == [("aaa", 3)]
    assert pending[0].source_url == _url("aaa", 3)


def test_a_superseded_photos_old_blob_url_is_carried_for_deletion(tmp_path):
    """Nothing else records what is in the store, so if the pending item does
    not carry the old blob_url the blob is stranded the moment the row is
    replaced."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old.jpg")

    pending = collect_pending_photos(conn, tmp_path, _urls(aaa=1))

    assert pending[0].superseded_blob_url == "https://blob/old.jpg"


def test_a_never_hosted_photo_supersedes_nothing(photos, urls):
    pending = collect_pending_photos(_conn(), photos, urls)

    assert all(p.superseded_blob_url is None for p in pending)


def test_a_row_with_no_source_url_is_treated_as_unknown_and_re_uploaded(tmp_path):
    """Rows written before source_url existed. Until
    ops/backfill_hosted_source_urls.py runs their identity is unknown, and
    unknown must mean "re-upload" rather than "assume it matches"."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, None, "https://blob/legacy.jpg")

    pending = collect_pending_photos(conn, tmp_path, _urls(aaa=1))

    assert [(p.listing_id, p.position) for p in pending] == [("aaa", 1)]
    assert pending[0].superseded_blob_url == "https://blob/legacy.jpg"


def test_a_listing_with_no_photo_directory_is_skipped(photos):
    assert collect_pending_photos(_conn(), photos, _urls(missing=2)) == []


def test_a_listing_with_no_urls_yields_nothing(photos):
    assert collect_pending_photos(_conn(), photos, {"aaa": []}) == []


def test_a_url_whose_file_was_never_downloaded_is_not_pending(tmp_path):
    """Upload only reports on what download actually produced -- a photo
    whose fetch failed has no file, and inventing a pending entry for it
    would just fail the upload."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 2)).write_bytes(b"x")

    pending = collect_pending_photos(conn, tmp_path, _urls(aaa=2))

    assert [p.position for p in pending] == [2]


def test_an_old_format_file_is_never_uploaded(tmp_path):
    """A leftover NN.jpg predates the content-keyed rename and may belong to
    a previous listing at this id -- uploading it is the stale-photo bug."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    (d / "02.jpg").write_bytes(b"x")

    pending = collect_pending_photos(conn, tmp_path, _urls(aaa=2))

    assert [(p.listing_id, p.position) for p in pending] == [("aaa", 1)]


def test_the_per_listing_cap_is_honoured(tmp_path):
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 21):
        (d / _name("aaa", i)).write_bytes(b"x")

    pending = collect_pending_photos(_conn(), tmp_path, _urls(aaa=20), max_per_listing=8)

    assert [p.position for p in pending] == list(range(1, 9))


def test_a_cap_of_zero_means_no_cap(tmp_path):
    d = tmp_path / "aaa"
    d.mkdir()
    for i in range(1, 13):
        (d / _name("aaa", i)).write_bytes(b"x")

    assert len(
        collect_pending_photos(_conn(), tmp_path, _urls(aaa=12), max_per_listing=0)
    ) == 12


# --- the URL map ----------------------------------------------------------

def test_get_photo_urls_by_listing_is_one_statement_and_keeps_position_order():
    conn = _conn()
    _listing_row(conn, "aaa")
    conn.executemany(
        "INSERT INTO photo_urls (listing_id, position, url) VALUES (?, ?, ?)",
        [("aaa", 1, "second"), ("aaa", 0, "first"), ("aaa", 2, "third")],
    )
    conn.commit()

    with _Counter(conn) as statements:
        by_listing = get_photo_urls_by_listing(conn)

    assert by_listing == {"aaa": ["first", "second", "third"]}
    assert len([s for s in statements if "photo_urls" in s]) == 1


def test_get_photo_urls_by_listing_gives_a_photoless_listing_an_empty_list():
    conn = _conn()
    _listing_row(conn, "bbb")
    conn.commit()

    assert get_photo_urls_by_listing(conn) == {"bbb": []}


# --- uploading and recording ---------------------------------------------

def test_every_pending_photo_uploads_and_is_recorded(photos, urls):
    conn = _conn()

    uploaded, failed = upload_photos(conn, photos, urls, "tok", upload_fn=_ok)

    assert (uploaded, failed) == (5, 0)
    rows = conn.execute(
        "SELECT listing_id, position, blob_url, source_url FROM hosted_photos "
        "ORDER BY listing_id, position"
    ).fetchall()
    assert [(r["listing_id"], r["position"]) for r in rows] == [
        ("aaa", 1), ("aaa", 2), ("aaa", 3), ("bbb", 1), ("bbb", 2)
    ]
    assert rows[0]["blob_url"].endswith(f"/photos/aaa/{_name('aaa', 1)}")


def test_the_source_url_is_recorded_alongside_the_blob_url(photos, urls):
    """Without it the next run cannot tell a matching photo from a replaced
    one, which is the whole bug."""
    conn = _conn()

    upload_photos(conn, photos, urls, "tok", upload_fn=_ok)

    row = conn.execute(
        "SELECT source_url FROM hosted_photos WHERE listing_id = 'aaa' AND position = 2"
    ).fetchone()
    assert row["source_url"] == _url("aaa", 2)


def test_a_replacement_upload_overwrites_the_row_and_keeps_one_per_position(tmp_path):
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old.jpg")

    upload_photos(conn, tmp_path, _urls(aaa=1), "tok", upload_fn=_ok)

    rows = conn.execute("SELECT source_url, blob_url FROM hosted_photos").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_url"] == _url("aaa", 1)
    assert rows[0]["blob_url"] != "https://blob/old.jpg"


def test_a_failed_upload_records_no_row_so_a_rerun_retries_it(photos, urls):
    def flaky(path, lid, pos, token, source_url):
        if lid == "aaa" and pos == 2:
            raise RuntimeError("blob upload failed: 500")
        return _ok(path, lid, pos, token, source_url)

    conn = _conn()
    uploaded, failed = upload_photos(conn, photos, urls, "tok", upload_fn=flaky)

    assert (uploaded, failed) == (4, 1)
    recorded = {
        (r["listing_id"], r["position"])
        for r in conn.execute("SELECT * FROM hosted_photos")
    }
    assert ("aaa", 2) not in recorded, "a failed upload must leave no row behind"


def test_rerunning_after_a_full_upload_does_nothing(photos, urls):
    conn = _conn()
    upload_photos(conn, photos, urls, "tok", upload_fn=_ok)

    calls = []

    def track(path, lid, pos, token, source_url):
        calls.append((lid, pos))
        return _ok(path, lid, pos, token, source_url)

    uploaded, failed = upload_photos(conn, photos, urls, "tok", upload_fn=track)

    assert (uploaded, failed) == (0, 0)
    assert calls == [], "idempotent: nothing should re-upload"


def test_a_retry_after_a_failure_uploads_only_the_failed_photo(photos, urls):
    conn = _conn()
    state = {"fail": True}

    def flaky(path, lid, pos, token, source_url):
        if state["fail"] and lid == "aaa" and pos == 2:
            raise RuntimeError("500")
        return _ok(path, lid, pos, token, source_url)

    upload_photos(conn, photos, urls, "tok", upload_fn=flaky)
    state["fail"] = False

    calls = []

    def track(path, lid, pos, token, source_url):
        calls.append((lid, pos))
        return _ok(path, lid, pos, token, source_url)

    uploaded, failed = upload_photos(conn, photos, urls, "tok", upload_fn=track)

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
        uploaded, failed = upload_photos(
            conn, tmp_path, _urls(aaa=120), "tok", upload_fn=_ok
        )

    assert (uploaded, failed) == (120, 0)
    writes = [s for s in statements if s.strip().upper().startswith("INSERT")]
    assert len(writes) < 10, f"{len(writes)} inserts for 120 photos"


def test_no_pending_photos_is_a_clean_no_op(tmp_path):
    conn = _conn()

    uploaded, failed = upload_photos(
        conn, tmp_path, _urls(missing=2), "tok",
        upload_fn=lambda *a: pytest.fail("should not upload"),
    )

    assert (uploaded, failed) == (0, 0)


def test_the_shared_connection_is_never_used_from_a_worker_thread(photos, urls):
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

    def record_thread(path, lid, pos, token, source_url):
        seen.append(threading.current_thread().ident)
        return _ok(path, lid, pos, token, source_url)

    uploaded, failed = upload_photos(
        ThreadGuarded(inner), photos, urls, "tok", upload_fn=record_thread
    )

    assert (uploaded, failed) == (5, 0)
    assert any(t != main_thread for t in seen), "uploads should run on worker threads"


def test_the_superseded_blob_is_deleted_after_its_replacement_row_is_written(tmp_path):
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old.jpg")
    rows_at_delete_time = []

    def delete_fn(urls, token):
        rows_at_delete_time.append(
            conn.execute(
                "SELECT source_url FROM hosted_photos WHERE listing_id = 'aaa'"
            ).fetchone()["source_url"]
        )

    upload_photos(
        conn, tmp_path, _urls(aaa=1), "tok", upload_fn=_ok, delete_fn=delete_fn
    )

    assert rows_at_delete_time == [_url("aaa", 1)], (
        "the replacement row must be written before its predecessor's blob goes"
    )


def test_nothing_is_deleted_for_a_photo_that_was_never_hosted(photos, urls):
    conn = _conn()
    deleted = []

    upload_photos(
        conn, photos, urls, "tok", upload_fn=_ok,
        delete_fn=lambda u, t: deleted.extend(u),
    )

    assert deleted == []


def test_a_failed_upload_leaves_its_predecessors_blob_alone(tmp_path):
    """The old blob is still the one the row points at, so deleting it would
    break the viewer for a photo that never got replaced."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old.jpg")
    deleted = []

    def boom(*args):
        raise RuntimeError("500")

    upload_photos(
        conn, tmp_path, _urls(aaa=1), "tok", upload_fn=boom,
        delete_fn=lambda u, t: deleted.extend(u),
    )

    assert deleted == []
    row = conn.execute("SELECT blob_url FROM hosted_photos").fetchone()
    assert row["blob_url"] == "https://blob/old.jpg"


def test_a_failed_blob_delete_prints_the_url_and_does_not_fail_the_run(tmp_path, capsys):
    """A stranded blob costs a fraction of a cent; a failed scrape costs the
    run. The URL is printed because nothing else records it any more."""
    conn = _conn()
    d = tmp_path / "aaa"
    d.mkdir()
    (d / _name("aaa", 1)).write_bytes(b"x")
    _host(conn, "aaa", 1, "https://cdn.example.com/aaa/OLD.jpg", "https://blob/old.jpg")

    def boom(urls, token):
        raise RuntimeError("HTTP 500")

    uploaded, failed = upload_photos(
        conn, tmp_path, _urls(aaa=1), "tok", upload_fn=_ok, delete_fn=boom
    )

    assert (uploaded, failed) == (1, 0)
    assert "https://blob/old.jpg" in capsys.readouterr().out


def test_scrape_uploads_photos_within_its_own_stage():
    """Acceptance criterion for #21: the scrape stage, not publish.py."""
    source = Path("scrape.py").read_text()
    assert "upload_photos" in source
