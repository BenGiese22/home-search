"""The cross-home pipeline lease.

`pipeline.py` has always guarded concurrency with `fcntl.flock` on
`data/.pipeline.lock`. That is per-machine, and it was correct while one
laptop was the only execution home. Turso is now the single source of truth
and Phase 3 adds a second home (a Vercel Sandbox), which cannot see a flock
on the laptop's filesystem and vice versa.

The damage from two overlapping runs is not "duplicate work is wasteful".
The photo migration had a window in which every `hosted_photos` row read
`source_url IS NULL` -- identity unknown -- and a concurrent scrape in that
window would have re-downloaded ~700 MB from Compass. That traffic profile
is exactly what the unresolved reCAPTCHA durability canary is measuring, so
a bulk image pull every time two runs overlap can degrade the score the
whole phase depends on.

The lease has to live where both homes look, which is the database.
"""
import sqlite3

import pytest

from src.db import (
    DEFAULT_LEASE_SECONDS,
    PIPELINE_LOCK_NAME,
    acquire_pipeline_lease,
    delete_orphaned_rows,
    release_pipeline_lease,
    renew_pipeline_lease,
    tables_child_first,
)
from src.turso_db import TURSO_SCHEMA_EXTRA, ensure_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


class _Counted:
    """Records the SQL a block issues, so a round-trip budget can be
    asserted. Every statement against hosted Turso is a ~240ms HTTP
    round-trip; the same idiom guards the vision checkpoint."""

    def __init__(self, conn):
        self.conn, self.statements = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


def _starting(statements, keyword):
    return [s for s in statements if s.strip().upper().startswith(keyword)]


def _expire_the_lease(conn) -> None:
    """Backdate the stored lease so it reads as abandoned, without waiting
    out a real lease. Writes the server's own clock, like acquire does."""
    with conn:
        conn.execute(
            "UPDATE pipeline_lock SET expires_at = datetime('now', '-1 seconds')"
        )


# --- taking the lease ----------------------------------------------------

def test_a_free_lease_is_taken_by_the_asker():
    lease = acquire_pipeline_lease(_conn(), "laptop", "token-a")

    assert lease.mine is True
    assert lease.held_by == "laptop"
    assert lease.token == "token-a"


def test_a_second_home_loses_the_race():
    """The whole point. Two homes racing must not both win."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    lost = acquire_pipeline_lease(conn, "sandbox", "token-b")

    assert lost.mine is False


def test_the_loser_is_told_which_home_holds_it_and_since_when():
    """An operator staring at "exiting" needs to know where the other run
    is, otherwise the only recourse is to guess."""
    conn = _conn()
    won = acquire_pipeline_lease(conn, "laptop", "token-a")

    lost = acquire_pipeline_lease(conn, "sandbox", "token-b")

    assert lost.held_by == "laptop"
    assert lost.acquired_at == won.acquired_at
    assert lost.token == "token-a"


def test_losing_does_not_disturb_the_holder():
    """A failed acquire must not extend, shorten, or rewrite the lease it
    failed to take."""
    conn = _conn()
    held = acquire_pipeline_lease(conn, "laptop", "token-a")

    acquire_pipeline_lease(conn, "sandbox", "token-b", lease_seconds=99999)

    row = conn.execute("SELECT * FROM pipeline_lock").fetchone()
    assert row["lease_token"] == "token-a"
    assert row["expires_at"] == held.expires_at


def test_an_expired_lease_is_free_for_the_other_home():
    """A runner that crashes -- lid closed, sandbox reaped at its 3h limit --
    must not deadlock the other home forever."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")
    _expire_the_lease(conn)

    lease = acquire_pipeline_lease(conn, "sandbox", "token-b")

    assert lease.mine is True
    assert lease.held_by == "sandbox"


def test_an_unexpired_lease_is_still_held_after_the_original_home_asks_again():
    """A second process on the SAME home is still a second run. Identity is
    the per-run token, not the home name, or a restarted laptop run would
    quietly steal its own predecessor's lease."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    second = acquire_pipeline_lease(conn, "laptop", "token-a-second-process")

    assert second.mine is False
    assert second.token == "token-a"


def test_the_default_lease_covers_the_longest_stage():
    """score_photos.py polls a vision batch for hours. A lease shorter than
    the longest single stage expires mid-run and lets the other home in --
    which is the failure this exists to prevent, arriving late."""
    assert DEFAULT_LEASE_SECONDS >= 6 * 3600


# --- renewing ------------------------------------------------------------

def test_the_holder_can_renew_its_own_lease():
    conn = _conn()
    taken = acquire_pipeline_lease(conn, "laptop", "token-a", lease_seconds=60)

    assert renew_pipeline_lease(conn, "token-a", lease_seconds=99999) is True

    row = conn.execute("SELECT expires_at FROM pipeline_lock").fetchone()
    assert row["expires_at"] > taken.expires_at


def test_renewing_someone_elses_lease_is_refused():
    conn = _conn()
    held = acquire_pipeline_lease(conn, "laptop", "token-a")

    assert renew_pipeline_lease(conn, "token-b", lease_seconds=99999) is False

    row = conn.execute("SELECT * FROM pipeline_lock").fetchone()
    assert row["expires_at"] == held.expires_at


def test_renewing_a_lease_nobody_holds_is_refused_not_an_error():
    assert renew_pipeline_lease(_conn(), "token-a") is False


# --- releasing -----------------------------------------------------------

def test_the_holder_releases_and_the_lock_is_free_again():
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    assert release_pipeline_lease(conn, "token-a") is True

    assert acquire_pipeline_lease(conn, "sandbox", "token-b").mine is True


def test_releasing_someone_elses_lease_is_refused():
    """The dangerous one. A stale runner whose lease already expired and was
    taken by the other home must not free the new holder's lease on its way
    out -- that would hand a third run the lock while two are live."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    assert release_pipeline_lease(conn, "token-b") is False

    row = conn.execute("SELECT * FROM pipeline_lock").fetchone()
    assert row["lease_token"] == "token-a"


def test_releasing_twice_is_idempotent():
    """Release runs in a finally block, and a crash path can reach it after
    the lease is already gone."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    assert release_pipeline_lease(conn, "token-a") is True
    assert release_pipeline_lease(conn, "token-a") is False


def test_releasing_a_lock_that_was_never_taken_is_not_an_error():
    assert release_pipeline_lease(_conn(), "token-a") is False


# --- round-trip budget ---------------------------------------------------

def test_acquire_is_one_statement_and_never_reads_first():
    """A read-then-write acquire is not a lock: two homes can both read
    "free" before either writes. Atomicity has to come from the single
    statement, so a SELECT appearing here is a correctness bug, not a
    performance one."""
    conn = _conn()

    with _Counted(conn) as statements:
        acquire_pipeline_lease(conn, "laptop", "token-a")

    assert len(_starting(statements, "INSERT")) == 1
    assert _starting(statements, "SELECT") == []
    assert _starting(statements, "UPDATE") == []


def test_a_lost_acquire_is_also_one_statement():
    """The loser learns who holds the lease from the same statement, not
    from a follow-up SELECT."""
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    with _Counted(conn) as statements:
        lost = acquire_pipeline_lease(conn, "sandbox", "token-b")

    assert lost.held_by == "laptop"
    assert len(_starting(statements, "INSERT")) == 1
    assert _starting(statements, "SELECT") == []


def test_renew_and_release_are_one_statement_each():
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    with _Counted(conn) as statements:
        renew_pipeline_lease(conn, "token-a")
    assert len(_starting(statements, "UPDATE")) == 1
    assert _starting(statements, "SELECT") == []

    with _Counted(conn) as statements:
        release_pipeline_lease(conn, "token-a")
    assert len(_starting(statements, "DELETE")) == 1
    assert _starting(statements, "SELECT") == []


# --- the clock ------------------------------------------------------------

def test_expiry_is_measured_by_the_databases_clock_not_the_callers():
    """The two homes are different machines. If each stamped the lease from
    its own clock, a laptop running a few minutes fast would read a live
    sandbox lease as expired and take it. Every timestamp in this table is
    written by datetime('now') inside the statement, so both homes are
    compared against the one clock they share."""
    conn = _conn()

    with _Counted(conn) as statements:
        acquire_pipeline_lease(conn, "laptop", "token-a")

    sql = _starting(statements, "INSERT")[0]
    # A caller-supplied timestamp would have been bound as a parameter and
    # so would appear nowhere in the SQL text; datetime('now') has to.
    assert sql.count("datetime('now'") >= 2, sql
    assert conn.execute("SELECT * FROM pipeline_lock").fetchone()["acquired_at"]


# --- it is not a child of listings ---------------------------------------

def test_pipeline_lock_is_not_treated_as_a_listing_child_table():
    """It has no listing_id, deliberately. tables_child_first and
    delete_orphaned_rows both discover child tables by that column, so a
    lock enrolled in the delisting cascade would be deleted out from under a
    live run by an unrelated listing going away."""
    assert "pipeline_lock" not in tables_child_first(extra_tables=("hosted_photos",))


def test_the_orphan_sweep_ignores_it():
    conn = _conn()
    acquire_pipeline_lease(conn, "laptop", "token-a")

    removed = delete_orphaned_rows(conn)

    assert "pipeline_lock" not in removed
    assert conn.execute("SELECT COUNT(*) FROM pipeline_lock").fetchone()[0] == 1


def test_the_table_has_no_listing_id_column():
    """The property the two sweeps key on, asserted directly rather than
    only through them."""
    conn = _conn()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_lock)")}

    assert "listing_id" not in columns
    assert {"lock_name", "lease_token", "held_by", "acquired_at", "expires_at"} <= columns


def test_the_schema_declares_it_alongside_hosted_photos():
    assert "pipeline_lock" in TURSO_SCHEMA_EXTRA


def test_there_is_one_lock_row_however_many_runs_ask():
    """lock_name is the primary key, which is what makes the upsert a race
    the database resolves rather than one we resolve."""
    conn = _conn()
    for i in range(5):
        acquire_pipeline_lease(conn, f"home-{i}", f"token-{i}")

    rows = conn.execute("SELECT lock_name FROM pipeline_lock").fetchall()
    assert [row["lock_name"] for row in rows] == [PIPELINE_LOCK_NAME]


# --- the driver assumption the atomicity rests on -------------------------

def test_turso_serverless_wraps_dml_in_a_deferred_begin_not_an_immediate_one():
    """Pinned because the acquire deliberately does NOT rely on
    BEGIN IMMEDIATE.

    The obvious way to make a read-then-write acquire safe is
    `BEGIN IMMEDIATE`, which takes the write lock up front. This driver
    never issues one: `_maybe_implicit_begin` emits `BEGIN <isolation_level>`
    and `isolation_level` defaults to "DEFERRED", so a transaction takes no
    write lock until its first write -- exactly the window a read-then-write
    acquire would race in. Hence the single conditional upsert, whose
    atomicity is the statement's own.

    If a future driver made IMMEDIATE the default this test fails loudly,
    which is the moment to reconsider -- not silently inherit a guarantee we
    never asked for.
    """
    from turso_serverless.connection import Connection

    class _Session:
        autocommit = True

    class _Result:
        columns, rows, affected_rows, last_insert_rowid = (), (), 1, None

    conn = Connection(_Session())
    issued = []

    def _execute_stmt(sql, params=None, named_params=None, want_rows=True):
        issued.append(sql)
        if sql.startswith("BEGIN"):
            conn._session.autocommit = False
        return _Result()

    conn._execute_stmt = _execute_stmt
    conn.execute("INSERT INTO pipeline_lock VALUES (1)")

    assert issued[0] == "BEGIN DEFERRED"
    assert "IMMEDIATE" not in issued[0]


def test_a_returned_row_not_rowcount_is_what_says_we_won():
    """Why acquire uses RETURNING. turso_serverless sets `rowcount` to -1
    for any statement that comes back with columns, and its value for a
    conditional upsert that updated nothing is not something we have
    verified against the server. A returned row is positive evidence and
    behaves identically on sqlite3 and on Turso."""
    conn = _conn()

    cursor = conn.execute(
        "INSERT INTO pipeline_lock "
        "(lock_name, lease_token, held_by, acquired_at, expires_at) "
        "VALUES ('x', 't', 'h', datetime('now'), datetime('now')) "
        "RETURNING lease_token"
    )

    assert cursor.fetchone()["lease_token"] == "t"


# --- a lease is not a substitute for the flock ---------------------------

def test_two_homes_and_one_database_is_the_scenario_this_covers():
    """End to end, in the shape the incident would take: the laptop starts a
    run, the cloud cron fires ten minutes later, and the cloud run must find
    the lock taken rather than start a second scrape."""
    laptop = _conn()
    cloud = laptop  # one database, which is the entire point

    laptop_lease = acquire_pipeline_lease(laptop, "laptop", "run-1")
    cloud_lease = acquire_pipeline_lease(cloud, "sandbox", "run-2")

    assert laptop_lease.mine and not cloud_lease.mine
    assert cloud_lease.held_by == "laptop"

    # The laptop finishes; the next cloud trigger gets in.
    release_pipeline_lease(laptop, "run-1")
    assert acquire_pipeline_lease(cloud, "sandbox", "run-3").mine is True


@pytest.mark.parametrize("keyword", ["INSERT", "UPDATE", "DELETE"])
def test_no_operation_loops_over_rows(keyword):
    """There is exactly one lock row, so nothing here can loop -- but the
    budget is asserted rather than assumed, because a per-row round-trip has
    twice cost this project minutes it could not spare."""
    conn = _conn()
    with _Counted(conn) as statements:
        acquire_pipeline_lease(conn, "laptop", "token-a")
        renew_pipeline_lease(conn, "token-a")
        release_pipeline_lease(conn, "token-a")

    assert len(_starting(statements, keyword)) == 1
