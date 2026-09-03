"""The driver assumption that makes the stream-recovery guard safe.

with_stream_recovery retries a dropped stream unless `in_transaction` is
True. The commit that added it describes the rule as "never inside a
transaction", but that is not quite the invariant. The real one is **never
after a BEGIN has landed on the server**, because the server rolls a dying
stream's transaction back -- so replaying one statement of a multi-statement
write would commit a fragment of it.

Those two readings coincide only because turso_serverless defers BEGIN:
`__enter__` issues nothing, and `_maybe_implicit_begin` fires inside
Cursor.execute. So the first statement inside a `with conn:` block runs with
in_transaction still False and IS retried -- which is safe, because nothing
has committed yet -- while the second runs with it True and is refused.

If the driver ever made BEGIN eager -- in `__enter__`, or on connect -- the
coincidence breaks, in one of two ways depending on whether in_transaction
keeps up with it:

- in_transaction reports the eager BEGIN: the first statement is refused
  rather than retried, and every recovery this guard exists for silently
  stops happening. Safe, but back to runs dying on an idle stream.
- in_transaction lags behind it: the first statement is retried *after* a
  BEGIN has landed, which is the fragment-commit this guard exists to
  prevent.

The first is what the current code would do; the second is the one that
would actually corrupt data. Neither announces itself, and nothing else in
the suite would fail. These tests pin the assumption so a driver upgrade
breaks loudly here instead.

They deliberately drive the real turso_serverless Connection rather than a
stub: a stub would only re-assert what we already believe.
"""
import pytest
from turso_serverless.connection import Connection

from src.turso_db import with_stream_recovery

STREAM_GONE = "HTTP status 404: stream not found: c4f74259:1a3c8eb"


class _FakeSession:
    """Just enough Session for Connection. `autocommit` is what
    Connection.in_transaction reads."""

    def __init__(self):
        self.autocommit = True


class _StmtResult:
    columns = ()
    rows = ()
    affected_rows = 1
    last_insert_rowid = None


def _connection(fail_on=None):
    """A real driver Connection whose statements are recorded, not sent.

    fail_on: a SQL string that raises a stream-gone error the first time it
    is issued, mimicking a server that dropped an idle stream.
    """
    conn = Connection(_FakeSession())
    conn.statements = []
    failed = {"done": False}

    def _execute_stmt(sql, params=None, named_params=None, want_rows=True):
        conn.statements.append(sql)
        if fail_on is not None and sql == fail_on and not failed["done"]:
            failed["done"] = True
            # The driver resets its baton on failure; the next statement
            # opens a fresh stream. Model that by leaving state alone.
            raise Exception(STREAM_GONE)
        if sql.startswith("BEGIN"):
            conn._session.autocommit = False
        elif sql.startswith(("COMMIT", "ROLLBACK")):
            conn._session.autocommit = True
        return _StmtResult()

    conn._execute_stmt = _execute_stmt
    return conn


def test_entering_a_with_block_issues_no_statement():
    conn = _connection()
    with conn:
        assert conn.statements == []
        assert conn.in_transaction is False
    # __exit__ commits, but there was no transaction to commit.
    assert conn.statements == []


def test_begin_is_deferred_until_the_first_dml_statement():
    conn = _connection()
    with conn:
        assert conn.in_transaction is False
        conn.execute("INSERT INTO t VALUES (1)")
        assert conn.statements[0].startswith("BEGIN")
        assert conn.in_transaction is True


def test_first_statement_in_a_with_block_runs_before_any_begin_lands():
    """The precondition for the retry being safe: at the moment the guard
    reads in_transaction for the first statement, no BEGIN has committed
    anything, so replaying it cannot commit a fragment."""
    conn = _connection()
    observed = []
    original = conn.execute

    def spy(sql, parameters=()):
        observed.append((sql, conn.in_transaction))
        return original(sql, parameters)

    conn.execute = spy
    with conn:
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("INSERT INTO t VALUES (2)")

    assert observed[0][1] is False, "first statement must precede BEGIN"
    assert observed[1][1] is True, "second statement must be inside the transaction"


def test_guard_retries_the_first_statement_and_not_the_second():
    """End to end against the real driver: the exact shape of the crash that
    killed the score_photos run, and the shape that must still fail."""
    conn = _connection(fail_on="INSERT INTO t VALUES (1)")
    wrapped = with_stream_recovery(conn)
    with conn:
        wrapped.execute("INSERT INTO t VALUES (1)")          # retried, survives
    assert conn.statements.count("INSERT INTO t VALUES (1)") == 2

    conn2 = _connection(fail_on="INSERT INTO t VALUES (2)")
    wrapped2 = with_stream_recovery(conn2)
    with pytest.raises(Exception, match="stream not found"):
        with conn2:
            wrapped2.execute("INSERT INTO t VALUES (1)")     # opens the transaction
            wrapped2.execute("INSERT INTO t VALUES (2)")     # must NOT be retried
    assert conn2.statements.count("INSERT INTO t VALUES (2)") == 1
