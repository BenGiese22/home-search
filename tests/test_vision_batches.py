"""The vision checkpoint, moved from a local file into Turso.

`score_photos.py` submits paid batches to a vision API, then polls for
results that can take hours. The checkpoint is what stops an interrupted run
paying for the same listings twice.

As a file it could only ever protect ONE machine. Both execution homes now
share one database, so a laptop run that submits a batch and has its lid
closed leaves its checkpoint on local disk -- and a cloud run then sees
listings with no visual_scores row, no checkpoint it can read, and
resubmits. That is real money, and it will happen once both homes are live.

The record of "already submitted" has to live where both homes look.
"""
import json
import sqlite3

import pytest

from src.db import (
    clear_vision_batch,
    load_vision_batches,
    record_vision_batch,
)
from src.turso_db import TURSO_SCHEMA_EXTRA, ensure_schema
from src.db import delete_orphaned_rows, tables_child_first


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


class _Counted:
    def __init__(self, conn):
        self.conn, self.statements = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


# --- the round trip ------------------------------------------------------

def test_a_recorded_batch_comes_back_with_its_expectations():
    """garage_expected_by_id is stored per batch, not recomputed. The listing
    set that produced a batch is the only thing that can interpret its
    results."""
    conn = _conn()
    record_vision_batch(conn, "batch_1", {"L1": True, "L2": False}, "laptop")

    batches = load_vision_batches(conn)

    assert len(batches) == 1
    assert batches[0]["batch_id"] == "batch_1"
    assert batches[0]["garage_expected_by_id"] == {"L1": True, "L2": False}


def test_booleans_survive_the_json_round_trip():
    conn = _conn()
    record_vision_batch(conn, "b", {"L1": True, "L2": False}, "h")

    got = load_vision_batches(conn)[0]["garage_expected_by_id"]
    assert got["L1"] is True and got["L2"] is False


def test_clearing_removes_only_the_named_batch():
    """Finer-grained than the old clear-everything-at-the-end: a crash
    between two batches must not make the first look unprocessed."""
    conn = _conn()
    record_vision_batch(conn, "b1", {"L1": True}, "h")
    record_vision_batch(conn, "b2", {"L2": True}, "h")

    clear_vision_batch(conn, "b1")

    assert [b["batch_id"] for b in load_vision_batches(conn)] == ["b2"]


def test_no_batches_is_an_empty_list_not_an_error():
    assert load_vision_batches(_conn()) == []


def test_recording_the_same_batch_twice_does_not_duplicate_it():
    conn = _conn()
    record_vision_batch(conn, "b", {"L1": True}, "h")
    record_vision_batch(conn, "b", {"L1": True}, "h")

    assert len(load_vision_batches(conn)) == 1


# --- round-trip budget ---------------------------------------------------

def test_each_operation_is_a_single_statement():
    conn = _conn()

    with _Counted(conn) as s:
        record_vision_batch(conn, "b", {"L1": True}, "h")
    assert len([x for x in s if x.strip().upper().startswith("INSERT")]) == 1

    with _Counted(conn) as s:
        load_vision_batches(conn)
    assert len([x for x in s if x.strip().upper().startswith("SELECT")]) == 1

    with _Counted(conn) as s:
        clear_vision_batch(conn, "b")
    assert len([x for x in s if x.strip().upper().startswith("DELETE")]) == 1


def test_loading_is_one_statement_however_many_batches():
    conn = _conn()
    for i in range(30):
        record_vision_batch(conn, f"b{i}", {f"L{i}": True}, "h")

    with _Counted(conn) as s:
        assert len(load_vision_batches(conn)) == 30
    assert len([x for x in s if x.strip().upper().startswith("SELECT")]) == 1


# --- the reason this moved: two homes, one database ----------------------

def test_a_batch_submitted_by_one_home_is_visible_to_the_other():
    """The whole point. A file checkpoint is invisible across homes, so the
    second home resubmits and the vision API is paid twice."""
    home_a = _conn()
    record_vision_batch(home_a, "batch_from_laptop", {"L1": True, "L2": True}, "laptop")

    # The other home reads the same database.
    already = {
        listing_id
        for batch in load_vision_batches(home_a)
        for listing_id in batch["garage_expected_by_id"]
    }

    assert already == {"L1", "L2"}, "the second home must see the first home's batch"


def test_the_submitting_home_is_recorded():
    """So an operator can tell which home has an in-flight batch."""
    conn = _conn()
    record_vision_batch(conn, "b", {"L1": True}, "sandbox")

    assert load_vision_batches(conn)[0]["submitted_by"] == "sandbox"


# --- it is not a child of listings --------------------------------------

def test_vision_batches_is_not_treated_as_a_listing_child_table():
    """It has no listing_id, deliberately: a batch spans many listings, and
    one being delisted must not delete the record of an in-flight batch that
    other listings are still waiting on."""
    assert "vision_batches" not in tables_child_first(extra_tables=("hosted_photos",))


def test_the_orphan_sweep_ignores_it():
    """delete_orphaned_rows discovers child tables by looking for a
    listing_id column. vision_batches has none, so it must be invisible to
    the sweep rather than silently emptied."""
    conn = _conn()
    record_vision_batch(conn, "b", {"L1": True}, "h")

    removed = delete_orphaned_rows(conn)

    assert "vision_batches" not in removed
    assert len(load_vision_batches(conn)) == 1


def test_the_schema_declares_it_alongside_hosted_photos():
    assert "vision_batches" in TURSO_SCHEMA_EXTRA


# --- migrating the legacy file ------------------------------------------

def test_a_legacy_checkpoint_file_is_imported_once_and_renamed(tmp_path, monkeypatch):
    """The file is renamed, not deleted. If the import failed halfway it is
    the only record that money was already spent."""
    import score_photos

    legacy = tmp_path / ".photo_scoring_batch_state.json"
    legacy.write_text(json.dumps([
        {"batch_id": "old_1", "garage_expected_by_id": {"L1": True}},
        {"batch_id": "old_2", "garage_expected_by_id": {"L2": False}},
    ]))
    monkeypatch.setattr(score_photos, "LEGACY_BATCH_STATE_PATH", legacy)
    conn = _conn()

    score_photos._migrate_legacy_checkpoint(conn)

    assert {b["batch_id"] for b in load_vision_batches(conn)} == {"old_1", "old_2"}
    assert not legacy.exists()
    assert legacy.with_suffix(".json.migrated").exists()


def test_migration_does_not_duplicate_batches_already_in_the_database(tmp_path, monkeypatch):
    import score_photos

    legacy = tmp_path / ".photo_scoring_batch_state.json"
    legacy.write_text(json.dumps([{"batch_id": "b", "garage_expected_by_id": {"L1": True}}]))
    monkeypatch.setattr(score_photos, "LEGACY_BATCH_STATE_PATH", legacy)
    conn = _conn()
    record_vision_batch(conn, "b", {"L1": True}, "laptop")

    score_photos._migrate_legacy_checkpoint(conn)

    assert len(load_vision_batches(conn)) == 1


def test_no_legacy_file_is_a_clean_no_op(tmp_path, monkeypatch):
    import score_photos

    monkeypatch.setattr(score_photos, "LEGACY_BATCH_STATE_PATH", tmp_path / "absent.json")
    conn = _conn()

    score_photos._migrate_legacy_checkpoint(conn)

    assert load_vision_batches(conn) == []


def test_an_unreadable_legacy_file_is_left_alone(tmp_path, monkeypatch, capsys):
    """Renaming a file we could not read would destroy the only record of
    in-flight batches. Better to warn and leave it."""
    import score_photos

    legacy = tmp_path / ".photo_scoring_batch_state.json"
    legacy.write_text("{ this is not json")
    monkeypatch.setattr(score_photos, "LEGACY_BATCH_STATE_PATH", legacy)

    score_photos._migrate_legacy_checkpoint(_conn())

    assert legacy.exists(), "an unreadable checkpoint must not be renamed away"
    assert "could not read" in capsys.readouterr().out


# --- resuming after a crash ---------------------------------------------

def test_resume_resolves_expectations_from_the_stored_row_not_the_db():
    """The scenario the checkpoint exists for. A crash partway through a
    batch's results leaves some listings already scored, so recomputing the
    expectation map from 'listings still missing a score' would produce a
    NARROWER set than the batch actually contains -- and the Batch API always
    returns the full original result set. The stored map is the only thing
    that can interpret those results.
    """
    conn = _conn()
    submitted = {"L1": True, "L2": False, "L3": True}
    record_vision_batch(conn, "b", submitted, "laptop")

    # ... crash here, with L1 and L2 already written to visual_scores ...
    resumed = load_vision_batches(conn)[0]["garage_expected_by_id"]

    assert resumed == submitted, "the full submitted set must survive a crash"
    assert set(resumed) >= {"L1", "L2", "L3"}
