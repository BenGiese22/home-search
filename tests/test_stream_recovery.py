"""Surviving an expired Turso stream.

The libsql HTTP protocol keeps server-side state (a "baton") per stream. A
stage that goes quiet on the database for a while -- scrape.py downloading
photos, score_photos.py waiting on a vision batch -- comes back to find the
server has dropped it, and the next statement fails with

    OperationalError: HTTP status 404: stream not found: <id>

Found by gate 4: a sandbox scrape bulk-wrote 88 listings, spent minutes
downloading photos, then died on the query_listings() that begins the upload
step. It is timing-dependent, so it passed the first run and failed the
second -- the worst kind of bug to leave in.

The driver resets the baton on any failure and documents that "the next
statement starts a fresh stream", explicitly leaving the retry decision to
the application. These tests pin that decision.
"""
import pytest

from src.turso_db import (
    STREAM_GONE_MARKERS,
    is_stream_gone,
    with_stream_recovery,
)


class _StreamGone(Exception):
    pass


class _FakeConn:
    """Fails a given number of times with a stream error, then succeeds."""

    def __init__(self, fail_times=1, message="HTTP status 404: stream not found: abc:123",
                 in_transaction=False):
        self.fail_times = fail_times
        self.message = message
        self.in_transaction = in_transaction
        self.calls = []
        self.row_factory = None
        self.commits = 0

    def execute(self, sql, params=()):
        self.calls.append(sql)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _StreamGone(self.message)
        return f"rows-for:{sql}"

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True

    # Real connections -- sqlite3 and turso_serverless alike -- commit on a
    # clean exit and roll back on an exception. The fake models that so the
    # wrapper is tested against the contract it actually meets.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rolled_back = True
        return False


# --- recognising the error ------------------------------------------------

def test_a_dropped_stream_is_recognised():
    assert is_stream_gone(Exception("HTTP status 404: stream not found: c4f74259:1ad658f"))


@pytest.mark.parametrize("marker", sorted(STREAM_GONE_MARKERS))
def test_every_declared_marker_is_recognised(marker):
    assert is_stream_gone(Exception(f"HTTP status 404: {marker}"))


def test_an_unrelated_error_is_not_mistaken_for_a_dropped_stream():
    """Retrying a constraint violation or a syntax error would just fail
    twice and muddy the traceback."""
    for msg in ("UNIQUE constraint failed: listings.listing_id",
                "no such table: listings",
                "HTTP status 401: unauthorized",
                "HTTP status 500: internal error"):
        assert not is_stream_gone(Exception(msg)), msg


# --- the retry ------------------------------------------------------------

def test_a_statement_is_retried_once_on_a_fresh_stream():
    inner = _FakeConn(fail_times=1)
    conn = with_stream_recovery(inner)

    assert conn.execute("SELECT * FROM listings") == "rows-for:SELECT * FROM listings"
    assert len(inner.calls) == 2, "should have retried exactly once"


def test_a_successful_statement_is_not_retried():
    inner = _FakeConn(fail_times=0)
    conn = with_stream_recovery(inner)

    conn.execute("SELECT 1")

    assert len(inner.calls) == 1


def test_it_gives_up_after_one_retry():
    """A stream that will not come back is a real failure. Retrying forever
    would turn an outage into a hang."""
    inner = _FakeConn(fail_times=5)
    conn = with_stream_recovery(inner)

    with pytest.raises(_StreamGone):
        conn.execute("SELECT 1")
    assert len(inner.calls) == 2


def test_an_unrelated_error_is_raised_immediately():
    inner = _FakeConn(fail_times=1, message="UNIQUE constraint failed")
    conn = with_stream_recovery(inner)

    with pytest.raises(_StreamGone):
        conn.execute("INSERT INTO listings VALUES (1)")
    assert len(inner.calls) == 1, "must not retry a genuine error"


def test_a_statement_inside_a_transaction_is_never_retried():
    """The killer case. When the stream dies the server rolls the
    transaction back, so retrying one statement of a multi-statement write
    would commit a fragment of it -- a listings row with no amenities, say.
    Better to fail loudly and let the run be repeated: every write in this
    project is an idempotent upsert.
    """
    inner = _FakeConn(fail_times=1, in_transaction=True)
    conn = with_stream_recovery(inner)

    with pytest.raises(_StreamGone):
        conn.execute("INSERT INTO amenities VALUES ('a','b')")
    assert len(inner.calls) == 1, "a mid-transaction statement must not be retried"


# --- transparency ---------------------------------------------------------

def test_the_wrapper_passes_other_attributes_through():
    inner = _FakeConn()
    conn = with_stream_recovery(inner)

    conn.commit()
    assert inner.commits == 1
    conn.close()
    assert inner.closed is True


def test_the_row_factory_is_visible_through_the_wrapper():
    """turso_db.connect() sets row_factory on the connection; callers and
    tests read it back."""
    inner = _FakeConn()
    inner.row_factory = "sentinel"
    conn = with_stream_recovery(inner)

    assert conn.row_factory == "sentinel"


def test_the_context_manager_still_delegates():
    inner = _FakeConn()
    conn = with_stream_recovery(inner)

    with conn:
        pass

    assert inner.commits == 1


def test_the_context_manager_still_rolls_back_on_error():
    """`with conn:` is how every batched write in src/db.py is framed; if the
    wrapper swallowed the exception a failed batch would look committed."""
    inner = _FakeConn()
    conn = with_stream_recovery(inner)

    with pytest.raises(ValueError):
        with conn:
            raise ValueError("boom")

    assert getattr(inner, "rolled_back", False) is True
    assert inner.commits == 0
