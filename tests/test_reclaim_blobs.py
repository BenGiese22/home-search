"""Reclaiming blobs that were stranded before delete_blobs existed.

Two sets, both irreversible, so both are tested harder than their size
suggests:

- Six hosted_photos rows whose source_url stayed NULL after the 2026-09-04
  backfill. Their listing has no photo_urls entry at that position, so they
  belong to a photo set the listing no longer serves.
- 1,813 URLs exported to data/archive/ before those orphaned rows were
  deleted on 2026-09-03, precisely because blob_url was the only record of
  what exists in Blob.

The rule these must not break, and which created set 2 in the first place:
**never delete a hosted_photos row without first capturing its blob_url.**
"""
import json
import sqlite3

import pytest

from ops.reclaim_stranded_blobs import (
    load_archived_urls,
    reclaim_archived,
    reclaim_stale_rows,
    stale_rows,
)
from src.turso_db import ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
        "listing_url) VALUES ('L1','a','b','CO','80020','$1',1,1,1,1,1,1,'d','u')")
    for pos, src in ((1, "https://img/1.jpg"), (2, None), (3, None)):
        conn.execute(
            "INSERT INTO hosted_photos (listing_id, position, source_url, blob_url) "
            "VALUES (?,?,?,?)", ("L1", pos, src, f"https://blob/{pos:02d}.jpg"))
    conn.commit()
    return conn


def _recorder():
    calls = []
    return calls, lambda urls, token: calls.append(list(urls))


# --- identifying what is stale -------------------------------------------

def test_stale_rows_are_exactly_those_with_no_source_url():
    rows = stale_rows(_conn())

    assert [(r["listing_id"], int(r["position"])) for r in rows] == [("L1", 2), ("L1", 3)]


def test_a_row_with_a_source_url_is_never_stale():
    assert all(r["source_url"] is None for r in stale_rows(_conn()))


def test_stale_rows_is_one_statement():
    conn = _conn()
    seen = []
    conn.set_trace_callback(seen.append)
    stale_rows(conn)
    conn.set_trace_callback(None)

    assert len([s for s in seen if s.strip().upper().startswith("SELECT")]) == 1


# --- dry run writes nothing ----------------------------------------------

def test_a_dry_run_deletes_no_rows_and_no_blobs():
    conn = _conn()
    calls, delete_fn = _recorder()

    reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=False)

    assert calls == [], "a dry run must not call the blob API"
    assert conn.execute("SELECT COUNT(*) FROM hosted_photos").fetchone()[0] == 3


def test_a_dry_run_reports_the_same_count_a_real_run_would_act_on():
    conn = _conn()
    _, delete_fn = _recorder()

    planned = reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=False)

    assert planned == 2


# --- the real thing ------------------------------------------------------

def test_rows_go_before_blobs():
    """Ordering copied from apply_delisting, and deliberate: the URLs are
    captured first, so a failed blob delete can never leave a row pointing
    at a blob that is already gone."""
    conn = _conn()
    order = []

    def delete_fn(urls, token):
        # By the time blobs are deleted the rows must already be gone.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM hosted_photos WHERE source_url IS NULL"
        ).fetchone()[0]
        order.append(("blobs", remaining))

    reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=True)

    assert order == [("blobs", 0)], "blobs were deleted before the rows"


def test_the_matching_blobs_are_deleted_and_nothing_else():
    conn = _conn()
    calls, delete_fn = _recorder()

    reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=True)

    assert calls == [["https://blob/02.jpg", "https://blob/03.jpg"]]


def test_the_row_with_a_source_url_survives():
    conn = _conn()
    _, delete_fn = _recorder()

    reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=True)

    rows = conn.execute("SELECT position FROM hosted_photos").fetchall()
    assert [int(r[0]) for r in rows] == [1]


def test_a_failed_blob_delete_prints_the_urls_rather_than_losing_them(capsys):
    """The rows are already gone at this point, so these URLs are the only
    remaining handle on the blobs. Swallowing them silently is how 1,813
    blobs were stranded in the first place."""
    conn = _conn()

    def failing(urls, token):
        raise RuntimeError("blob delete failed: 503")

    reclaim_stale_rows(conn, "tok", delete_fn=failing, apply=True)

    out = capsys.readouterr().out
    assert "https://blob/02.jpg" in out and "https://blob/03.jpg" in out


def test_nothing_stale_is_a_clean_no_op():
    conn = _conn()
    conn.execute("UPDATE hosted_photos SET source_url = 'x' WHERE source_url IS NULL")
    conn.commit()
    calls, delete_fn = _recorder()

    assert reclaim_stale_rows(conn, "tok", delete_fn=delete_fn, apply=True) == 0
    assert calls == []


# --- the archived set ----------------------------------------------------

def test_archived_urls_are_read_from_the_export(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"count": 2, "rows": [
        {"listing_id": "L1", "position": 1, "blob_url": "https://blob/a.jpg"},
        {"listing_id": "L1", "position": 2, "blob_url": "https://blob/b.jpg"}]}))

    assert load_archived_urls(path) == ["https://blob/a.jpg", "https://blob/b.jpg"]


def test_a_missing_archive_is_not_an_error(tmp_path):
    assert load_archived_urls(tmp_path / "nope.json") == []


def test_archived_blobs_are_deleted_only_when_applied(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"rows": [{"blob_url": "https://blob/a.jpg"}]}))
    calls, delete_fn = _recorder()

    assert reclaim_archived(path, "tok", delete_fn=delete_fn, apply=False) == 1
    assert calls == []

    reclaim_archived(path, "tok", delete_fn=delete_fn, apply=True)
    assert calls == [["https://blob/a.jpg"]]


def test_the_archive_file_is_never_deleted(tmp_path):
    """It is the record of what was reclaimed. Removing it would leave no
    evidence of what the blobs were."""
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"rows": [{"blob_url": "https://blob/a.jpg"}]}))
    _, delete_fn = _recorder()

    reclaim_archived(path, "tok", delete_fn=delete_fn, apply=True)

    assert path.exists()


# --- the archive can go stale, and that is dangerous ---------------------

def test_an_archived_url_that_is_live_again_is_never_deleted(tmp_path):
    """Caught against production before this ran: 44 of the 1,813 archived
    URLs were live again.

    A delisted listing can come back, and if it re-uploads to the same
    pathname its 'orphaned' URL is current once more. 6085 West 82nd Drive
    did exactly that -- delisted, archived, returned as a Pending favorite,
    re-uploaded to photos/<id>/NN.jpg. Deleting on the archive alone would
    have blanked 44 live photos in the viewer with nothing failing.
    """
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"rows": [
        {"blob_url": "https://blob/gone.jpg"},
        {"blob_url": "https://blob/live-again.jpg"},
    ]}))
    calls, delete_fn = _recorder()

    reclaim_archived(
        path, "tok", live_urls={"https://blob/live-again.jpg"},
        delete_fn=delete_fn, apply=True,
    )

    assert calls == [["https://blob/gone.jpg"]]


def test_the_dry_run_count_excludes_resurrected_urls(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"rows": [
        {"blob_url": "https://blob/a.jpg"}, {"blob_url": "https://blob/b.jpg"}]}))
    _, delete_fn = _recorder()

    planned = reclaim_archived(
        path, "tok", live_urls={"https://blob/b.jpg"}, delete_fn=delete_fn, apply=False)

    assert planned == 1, "a dry run must report what a real run would delete"


def test_every_archived_url_being_live_is_a_clean_no_op(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"rows": [{"blob_url": "https://blob/a.jpg"}]}))
    calls, delete_fn = _recorder()

    assert reclaim_archived(
        path, "tok", live_urls={"https://blob/a.jpg"},
        delete_fn=delete_fn, apply=True) == 0
    assert calls == []
