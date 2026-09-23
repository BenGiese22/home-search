"""main()'s own control flow: exit codes and per-item failure isolation.

Isolated from the rest of score_photos' tests (see test_rescore_all.py's
docstring) because these exercise main() itself rather than a helper it
calls -- every network and filesystem seam main() touches is faked here so
the only thing under test is main()'s own control flow.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import score_photos
from src.db import upsert_visual_score
from src.exit_codes import EXIT_PARTIAL
from src.turso_db import ensure_schema
from src.vision import VisualScoreResult


def _file_connection(db_path: Path) -> sqlite3.Connection:
    """A connection carrying the full hosted schema (vision_batches included,
    unlike src.db.get_connection's local-only init_db), backed by a real file
    so it can be re-opened after main() closes the handle it was given."""
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA foreign_keys = ON")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def add_listing(conn, lid):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, property_type, localized_status
           ) VALUES (?, 'A St', 'Arvada', 'CO', '80003', '$1', 3, 2.0, 1800,
                     7000, 2, 1990, 'd', 'https://x/l', 'Single Family', 'Active')""",
        (lid,),
    )
    conn.commit()


class _FakeBatches:
    """A batches API that always succeeds and ends immediately, so main()'s
    polling loop runs exactly once with nothing to process."""

    def create(self, requests):
        return SimpleNamespace(id="batch1")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        return iter([])


def _fake_client_factory(batches):
    return lambda api_key=None: SimpleNamespace(
        messages=SimpleNamespace(batches=batches)
    )


def _prepare_main(conn, monkeypatch, batches=None):
    """The env/connection/client seams every main() test needs faked, none of
    which are specific to the behaviour under test."""
    monkeypatch.setattr(score_photos, "stage_connection", lambda: conn)
    monkeypatch.setattr(score_photos, "load_env", lambda: {"ANTHROPIC_API_KEY": "sk-test"})
    monkeypatch.setattr(
        score_photos.anthropic, "Anthropic", _fake_client_factory(batches or _FakeBatches())
    )
    monkeypatch.setattr(score_photos, "count_downloaded_photos", lambda *a, **k: 10)


def test_a_missing_api_key_exits_non_zero_not_silently_successful(conn, monkeypatch, capsys):
    """Before this, main() had no return value and nothing called
    sys.exit(main()), so the script always exited 0 regardless of what
    happened inside -- a missing ANTHROPIC_API_KEY was reported as a
    successful run despite scoring nothing."""
    monkeypatch.setattr(score_photos, "stage_connection", lambda: conn)
    monkeypatch.setattr(score_photos, "load_env", lambda: {})

    assert score_photos.main() == score_photos.EXIT_NO_API_KEY
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_one_listings_bad_photos_do_not_stop_the_others_from_being_submitted(
    tmp_path: Path, monkeypatch, capsys
):
    """A corrupt or unreadable photo file for one listing must not crash the
    stage before any batch is even submitted -- the other listing's request
    still gets built and submitted."""
    # A real file, not :memory:, so the result can be read back after main()
    # closes the connection it was handed -- the same reason
    # test_score_batching.py's _score_one() re-opens by path.
    db_path = tmp_path / "t.db"
    conn = _file_connection(db_path)
    add_listing(conn, "L1")
    add_listing(conn, "L2")
    _prepare_main(conn, monkeypatch)

    def flaky_build_batch_request(listing_id, row, amenities, photo_paths):
        if listing_id == "L2":
            raise OSError("cannot read photo: truncated file")
        return SimpleNamespace(custom_id=listing_id)

    monkeypatch.setattr(score_photos, "build_batch_request", flaky_build_batch_request)

    # Partial, not 0: L2 is retried next run, but a corrupt photo that never
    # gets fixed would otherwise be retried forever without anyone hearing.
    assert score_photos.main() == EXIT_PARTIAL

    out = capsys.readouterr().out
    assert "L2: failed to build request" in out
    assert "submitted batch batch1 with 1 listings" in out

    l2_score = _file_connection(db_path).execute(
        "SELECT photo_score_unavailable FROM visual_scores WHERE listing_id = 'L2'"
    ).fetchone()
    assert l2_score["photo_score_unavailable"] == 1


def test_one_chunks_submission_failure_does_not_stop_the_others(conn, monkeypatch, capsys):
    """One chunk's API error must not cost every chunk after it its
    submission, and must not lose track of which listings were in it."""
    add_listing(conn, "L1")
    add_listing(conn, "L2")
    _prepare_main(conn, monkeypatch)
    monkeypatch.setattr(score_photos, "build_batch_request", lambda lid, *a: SimpleNamespace(custom_id=lid))
    # Forces each listing into its own chunk (see _chunk_by_size's docstring:
    # an entry larger than max_bytes still gets its own chunk).
    monkeypatch.setattr(score_photos, "MAX_BATCH_REQUEST_BYTES", 1)

    class _FailFirstBatches:
        def __init__(self):
            self.calls = 0

        def create(self, requests):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("batch API is down")
            return SimpleNamespace(id=f"batch{self.calls}")

        def retrieve(self, batch_id):
            return SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            return iter([])

    monkeypatch.setattr(
        score_photos.anthropic, "Anthropic", _fake_client_factory(_FailFirstBatches())
    )

    assert score_photos.main() == EXIT_PARTIAL

    out = capsys.readouterr().out
    assert "failed to submit batch of 1 listing(s)" in out
    assert "batch API is down" in out
    assert "submitted batch batch2 with 1 listings" in out
    assert "batch batch2: ended, processing results" in out


class _AlwaysFailingBatches:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create(self, requests):
        self.calls += 1
        raise self.exc


def _two_single_listing_chunks(conn, monkeypatch, batches):
    add_listing(conn, "L1")
    add_listing(conn, "L2")
    _prepare_main(conn, monkeypatch, batches)
    monkeypatch.setattr(
        score_photos, "build_batch_request", lambda lid, *a: SimpleNamespace(custom_id=lid)
    )
    monkeypatch.setattr(score_photos, "MAX_BATCH_REQUEST_BYTES", 1)


def test_every_chunk_failing_to_submit_is_a_real_failure(conn, monkeypatch, capsys):
    """The API is down and nothing is already in flight: nothing was scored
    and nothing will be. That used to exit 0 with "no listings had enough
    photos to score", which was false on both counts."""
    batches = _AlwaysFailingBatches(RuntimeError("batch API is down"))
    _two_single_listing_chunks(conn, monkeypatch, batches)

    assert score_photos.main() == score_photos.EXIT_SUBMIT_FAILED

    out = capsys.readouterr().out
    assert batches.calls == 2
    assert "no listings had enough photos" not in out
    assert "all 2 batch submission(s) failed" in out


def test_a_rejected_api_key_stops_at_the_first_chunk(conn, monkeypatch, capsys):
    """An expired key fails every chunk identically. Trying each one just
    repeats the same 401 N times in the alert."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    auth_error = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx.Response(401, request=request), body=None
    )
    batches = _AlwaysFailingBatches(auth_error)
    _two_single_listing_chunks(conn, monkeypatch, batches)

    assert score_photos.main() == score_photos.EXIT_SUBMIT_FAILED

    out = capsys.readouterr().out
    assert batches.calls == 1
    assert "ANTHROPIC_API_KEY was rejected" in out


def test_nothing_to_score_still_exits_zero(conn, monkeypatch, capsys):
    _prepare_main(conn, monkeypatch)

    assert score_photos.main() == 0
    assert "nothing to score" in capsys.readouterr().out


def test_only_too_few_photos_says_so(conn, monkeypatch, capsys):
    add_listing(conn, "L1")
    _prepare_main(conn, monkeypatch)
    monkeypatch.setattr(score_photos, "count_downloaded_photos", lambda *a, **k: 0)
    monkeypatch.setattr(score_photos, "hydrate_from_blob", lambda *a, **k: 0)

    assert score_photos.main() == 0
    assert "no listings had enough photos to score" in capsys.readouterr().out


# --- --rescore-all must never replace a good score with nothing -----------


def _seed_scored_listing(tmp_path: Path):
    db_path = tmp_path / "t.db"
    conn = _file_connection(db_path)
    add_listing(conn, "L1")
    upsert_visual_score(conn, "L1", VisualScoreResult(
        condition_photo_score=80.0, outdoor_photo_score=70.0, garage_attached=None))
    return db_path, conn


def _l1_visual_row(db_path: Path):
    return _file_connection(db_path).execute(
        "SELECT * FROM visual_scores WHERE listing_id = 'L1'"
    ).fetchone()


def test_rescore_all_keeps_a_good_score_when_the_request_cannot_be_built(
    tmp_path: Path, monkeypatch, capsys
):
    db_path, conn = _seed_scored_listing(tmp_path)
    _prepare_main(conn, monkeypatch)
    monkeypatch.setattr(score_photos.sys, "argv", ["score_photos.py", "--rescore-all"])

    def broken(*a):
        raise OSError("cannot read photo: truncated file")

    monkeypatch.setattr(score_photos, "build_batch_request", broken)

    assert score_photos.main() == EXIT_PARTIAL

    row = _l1_visual_row(db_path)
    assert row["photo_score_unavailable"] == 0
    assert row["condition_photo_score"] == 80.0
    assert "kept its existing score" in capsys.readouterr().out


def test_rescore_all_keeps_a_good_score_when_the_photos_are_gone(
    tmp_path: Path, monkeypatch, capsys
):
    """Photos don't survive the sandbox, and Blob can fail too. Neither is a
    reason to throw away a score the listing already earned."""
    db_path, conn = _seed_scored_listing(tmp_path)
    _prepare_main(conn, monkeypatch)
    monkeypatch.setattr(score_photos.sys, "argv", ["score_photos.py", "--rescore-all"])
    monkeypatch.setattr(score_photos, "count_downloaded_photos", lambda *a, **k: 0)
    monkeypatch.setattr(score_photos, "hydrate_from_blob", lambda *a, **k: 0)

    assert score_photos.main() == EXIT_PARTIAL

    row = _l1_visual_row(db_path)
    assert row["photo_score_unavailable"] == 0
    assert row["condition_photo_score"] == 80.0
    assert "kept its existing score" in capsys.readouterr().out


def test_a_result_that_failed_at_the_api_makes_the_run_partial(conn, monkeypatch, capsys):
    """Submitted, paid for, and came back errored: the listing is unscored,
    and the exit code has to say so."""
    add_listing(conn, "L1")

    class _ErroredResultBatches(_FakeBatches):
        def results(self, batch_id):
            return iter([SimpleNamespace(
                custom_id="L1", result=SimpleNamespace(type="errored"))])

    _prepare_main(conn, monkeypatch, _ErroredResultBatches())
    monkeypatch.setattr(
        score_photos, "build_batch_request", lambda lid, *a: SimpleNamespace(custom_id=lid)
    )

    assert score_photos.main() == EXIT_PARTIAL
    assert "partial run: 1 listing(s) could not be scored: L1" in capsys.readouterr().out
