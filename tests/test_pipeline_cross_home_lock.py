"""pipeline.py holding the cross-home lease alongside its flock.

The two guards are complementary and both stay. flock is instant, free, and
needs no network for the same-machine case -- a second systemd trigger while
a run is going. The Turso lease covers the case flock structurally cannot:
the other execution home, which shares the database and nothing else.

Losing either race is not a failure. A trigger landing during another run is
the normal shape of "trigger liberally, guard on freshness", so it exits 0
exactly as the flock path already does; anything else would have systemd
flagging a healthy pipeline.
"""
import sqlite3

import pytest

import pipeline
from src.db import acquire_pipeline_lease, release_pipeline_lease
from src.turso_db import ensure_schema


class Runner:
    """Records argv instead of spawning processes."""

    def __init__(self, exit_codes=None):
        self.calls = []
        self.exit_codes = exit_codes or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return self.exit_codes.get(argv[1], 0)

    @property
    def scripts(self):
        return [argv[1] for argv in self.calls]


@pytest.fixture
def lease_db():
    """One in-memory database standing in for the Turso both homes share."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


@pytest.fixture
def sandboxed_main(tmp_path, monkeypatch, lease_db):
    """main() with every path pointed at tmp_path and no real subprocess,
    no real revalidate, and no real Turso connection.

    data/ belongs to live runs; a test that writes there can clobber a real
    run's lock file or success marker.
    """
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / ".pipeline.lock")
    monkeypatch.setattr(pipeline, "MARKER_PATH", tmp_path / ".marker.json")
    monkeypatch.setattr(pipeline, "_default_revalidate", lambda: True)
    monkeypatch.setattr(pipeline, "_lease_connection", lambda: lease_db)
    monkeypatch.setattr(pipeline, "this_home", lambda: "laptop")

    runner = Runner()
    monkeypatch.setattr(pipeline, "_default_runner", runner)
    monkeypatch.setattr("sys.argv", ["pipeline.py"])
    return runner


# --- losing the cross-home race ------------------------------------------

def test_a_run_exits_zero_when_the_other_home_holds_the_lease(
    sandboxed_main, lease_db, capsys
):
    """Exactly what the flock path does. A trigger landing during another
    home's run is not a failure, and a non-zero exit would have systemd
    reporting a healthy pipeline as broken."""
    acquire_pipeline_lease(lease_db, "sandbox", "the-other-run")

    assert pipeline.main() == 0
    assert sandboxed_main.calls == [], "no stage may run without the lease"


def test_the_message_names_the_home_holding_it_and_since_when(
    sandboxed_main, lease_db, capsys
):
    """"another run is in progress" is enough when there is one machine. With
    two homes an operator needs to know which one, or the only recourse is to
    go and look at both."""
    held = acquire_pipeline_lease(lease_db, "sandbox", "the-other-run")

    pipeline.main()

    out = capsys.readouterr().out
    assert "sandbox" in out
    assert held.acquired_at in out


def test_a_lost_race_does_not_disturb_the_holders_lease(sandboxed_main, lease_db):
    """The losing run's finally block must not release the lease it never
    held -- that would hand the next trigger a lock while a run is live."""
    acquire_pipeline_lease(lease_db, "sandbox", "the-other-run")

    pipeline.main()

    row = lease_db.execute("SELECT * FROM pipeline_lock").fetchone()
    assert row["lease_token"] == "the-other-run"


# --- winning it -----------------------------------------------------------

def test_a_won_lease_runs_every_stage_and_is_released_afterwards(
    sandboxed_main, lease_db
):
    assert pipeline.main() == 0
    assert sandboxed_main.scripts == [s.script for s in pipeline.STAGES]

    assert lease_db.execute("SELECT COUNT(*) FROM pipeline_lock").fetchone()[0] == 0
    assert acquire_pipeline_lease(lease_db, "sandbox", "next").mine is True


def test_the_lease_is_released_even_when_a_stage_fails(
    sandboxed_main, lease_db, monkeypatch
):
    """A failed run must not leave the lock held for the rest of the lease.
    The expiry is the backstop for a crash, not for an ordinary failure."""
    monkeypatch.setattr(
        pipeline, "_default_runner", Runner(exit_codes={"scrape.py": 3})
    )

    assert pipeline.main() == 3
    assert lease_db.execute("SELECT COUNT(*) FROM pipeline_lock").fetchone()[0] == 0


def test_the_lease_is_released_when_the_freshness_guard_skips_the_run(
    sandboxed_main, lease_db, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["pipeline.py", "--max-age=6h"])
    pipeline.record_success(pipeline.MARKER_PATH)

    assert pipeline.main() == 0
    assert lease_db.execute("SELECT COUNT(*) FROM pipeline_lock").fetchone()[0] == 0


def test_the_run_records_which_home_holds_the_lease(sandboxed_main, lease_db):
    """Held during the run, not only asserted after it: the operator value is
    in reading the row while something is stuck."""
    seen = {}

    def spy(argv, **kwargs):
        seen.update(dict(lease_db.execute("SELECT * FROM pipeline_lock").fetchone()))
        return 0

    pipeline._default_runner = spy
    try:
        pipeline.main()
    finally:
        pipeline._default_runner = sandboxed_main
    assert seen["held_by"] == "laptop"


def test_a_dry_run_takes_no_lease(sandboxed_main, lease_db, monkeypatch):
    """It prints commands and runs nothing, so taking a cross-home lock for
    it would block a real run for no reason -- and it returns before the
    flock for the same reason."""
    monkeypatch.setattr("sys.argv", ["pipeline.py", "--dry-run"])

    assert pipeline.main() == 0
    assert lease_db.execute("SELECT COUNT(*) FROM pipeline_lock").fetchone()[0] == 0


# --- renewal --------------------------------------------------------------

def test_the_lease_is_renewed_before_each_stage(lease_db):
    """Nothing renews *during* a stage, so the lease has to outlast the
    longest one on its own -- but a full run can outlast a single lease, and
    renewing at each boundary is one statement per stage to cover that."""
    renewals = []
    run = pipeline.run_pipeline(
        list(pipeline.STAGES),
        runner=Runner(),
        revalidate_fn=lambda: True,
        renew_lease=lambda: renewals.append(True) or True,
    )

    assert run == 0
    assert len(renewals) == len(pipeline.STAGES)


def test_a_failed_renewal_warns_but_does_not_abort_the_run(capsys):
    """By the time a renewal fails the other home is already running.
    Stopping half way leaves the data in a worse state than finishing, so
    this is a loud warning and not an abort."""
    runner = Runner()

    code = pipeline.run_pipeline(
        list(pipeline.STAGES),
        runner=runner,
        revalidate_fn=lambda: True,
        renew_lease=lambda: False,
    )

    assert code == 0
    assert runner.scripts == [s.script for s in pipeline.STAGES]
    assert "lease" in capsys.readouterr().out.lower()


def test_run_pipeline_without_a_lease_still_works():
    """--dry-run, tests, and any direct caller pass no lease. The renewal
    hook has to be optional or every one of them breaks."""
    assert pipeline.run_pipeline(
        list(pipeline.STAGES), runner=Runner(), revalidate_fn=lambda: True
    ) == 0


# --- the flock stays ------------------------------------------------------

def test_the_flock_is_still_the_first_guard(sandboxed_main, lease_db, monkeypatch):
    """A same-machine second run must be stopped by the flock BEFORE any
    Turso round-trip: it is instant, free, and works with the network down.
    The lease covers the case flock cannot see, not the case it can."""
    connected = []
    monkeypatch.setattr(
        pipeline, "_lease_connection", lambda: connected.append(True) or lease_db
    )
    held = pipeline.LOCK_PATH.open("w")
    import fcntl

    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert pipeline.main() == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert connected == [], "the flock must short-circuit before Turso is touched"
    assert sandboxed_main.calls == []


def test_pipeline_still_imports_fcntl_and_locks_its_own_data_dir():
    """Pinned so a future cleanup does not read "we have a real lock now" and
    delete the cheap one."""
    import inspect

    source = inspect.getsource(pipeline.main)
    assert "fcntl.flock" in source


# --- identity -------------------------------------------------------------

def test_the_home_name_prefers_the_explicit_override(monkeypatch):
    """A sandbox's hostname is a generated id that means nothing to a human,
    so HOME_SEARCH_HOME wins where it is set."""
    from src.config import this_home

    monkeypatch.setenv("HOME_SEARCH_HOME", "sandbox")
    assert this_home() == "sandbox"


def test_the_home_name_falls_back_to_the_hostname(monkeypatch):
    import socket

    from src.config import this_home

    monkeypatch.delenv("HOME_SEARCH_HOME", raising=False)
    assert this_home() == socket.gethostname()


def test_each_run_gets_its_own_token(sandboxed_main, lease_db):
    """Ownership is the run, not the home. Two processes on one machine are
    two runs, and a shared token would let a restarted run steal its own
    predecessor's lease."""
    tokens = set()

    def collect(argv, **kwargs):
        row = lease_db.execute("SELECT lease_token FROM pipeline_lock").fetchone()
        tokens.add(row["lease_token"])
        return 0

    pipeline._default_runner = collect
    try:
        pipeline.main()
        pipeline.main()
    finally:
        pipeline._default_runner = sandboxed_main

    assert len(tokens) == 2
