"""bootstrap.sh must not rewrite the checkout of a run that is still going.

It runs `git reset --hard` and `pip install` in the same directory run.py
executes the pipeline from. Sep 8-19 the launcher's in-progress guard broke
and launched bootstrap into a live run, so bootstrap now checks run.py's lock
itself.

Every test runs a copy of the script in a tmp_path "checkout" with git and
the venv stubbed, so nothing is fetched or installed.
"""

import fcntl
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ops.sandbox.run import DONE_NAME, EXIT_LOCKED, LOCK_NAME, RUN_DIR, STARTED_NAME

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "ops" / "sandbox" / "bootstrap.sh"


# Prepended to every stub: note it if the stub inherited bootstrap's lock
# fd (9). A child holding it keeps the lock after bootstrap itself is gone.
_FD_CHECK = 'if [ -e "/proc/$$/fd/9" ]; then echo "$(basename "$0") $1" >> "$FD_LOG"; fi\n'


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + _FD_CHECK + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# Probes, from inside bootstrap, whether the run lock is free right now.
_PROBE = (
    'python3 -c "import fcntl, sys\n'
    "f = open('data/.run/lock', 'a')\n"
    "try:\n"
    "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "    print('free')\n"
    "except BlockingIOError:\n"
    "    print('held')\n"
    '" >> "$PROBE_LOG"\n'
)


@pytest.fixture
def checkout(tmp_path):
    """A fake checkout: the script at its real relative path, a git stub
    that logs every call, and a venv whose python/pip succeed trivially."""
    root = tmp_path / "checkout"
    script = root / "ops" / "sandbox" / "bootstrap.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(BOOTSTRAP, script)

    bin_dir = tmp_path / "bin"
    git_log = tmp_path / "git.log"
    probe_log = tmp_path / "probe.log"
    fd_log = tmp_path / "fd.log"
    # A git that records what it was asked, probes the lock mid-bootstrap,
    # and leaves a background child behind holding the inherited lock fd --
    # the shape of an auto-gc git daemonises after a fetch.
    _write_exe(
        bin_dir / "git",
        'echo "git $*" >> "$GIT_LOG"\n'
        # What the markers look like the moment the first git runs.
        'if [ -n "${SNAP_LOG:-}" ] && [ ! -e "$SNAP_LOG" ]; then\n'
        '    ls data/.run > "$SNAP_LOG"; cat data/.run/started >> "$SNAP_LOG"\n'
        "fi\n"
        'if [ "$1" = reset ]; then\n'
        + _PROBE
        + "    sleep 2 >/dev/null 2>&1 &\n"
        # SIGKILL bootstrap mid-reset: no EXIT trap runs, only the
        # background child above is left.
        '    if [ -n "${KILL_BOOTSTRAP:-}" ]; then kill -9 "$PPID"; sleep 1; fi\n'
        "fi\n"
        'if [ "$1" = rev-parse ]; then echo abc123; fi\n'
        # Fail the fetch: a bootstrap that dies after its provisional marker.
        'if [ "$1" = fetch ] && [ -n "${GIT_FAIL:-}" ]; then exit "$GIT_FAIL"; fi\n'
        "exit 0\n",
    )
    _write_exe(root / "venv" / "bin" / "python", "echo stub-python\n")
    _write_exe(root / "venv" / "bin" / "pip", "exit 0\n")

    home = tmp_path / "home"
    (home / ".cache" / "ms-playwright").mkdir(parents=True)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "GIT_LOG": str(git_log),
        "PROBE_LOG": str(probe_log),
        "FD_LOG": str(fd_log),
    }
    return {
        "root": root,
        "script": script,
        "env": env,
        "git_log": git_log,
        "probe_log": probe_log,
        "fd_log": fd_log,
        "bin_dir": bin_dir,
    }


def _lock_path(root: Path) -> Path:
    return root / RUN_DIR / LOCK_NAME


def _hold_lock(root: Path):
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def _run(checkout, env=None, args=()):
    return subprocess.run(
        ["bash", str(checkout["script"]), *args],
        env=env or checkout["env"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bootstrap_refuses_while_a_run_holds_the_lock(checkout):
    held = _hold_lock(checkout["root"])
    try:
        result = _run(checkout)
    finally:
        held.close()

    assert result.returncode == EXIT_LOCKED == 75
    assert "data/.run/lock" in result.stderr
    assert not checkout["git_log"].exists(), "git ran against a live checkout"


def test_bootstrap_holds_the_lock_while_it_works_and_releases_it_after(checkout):
    """A run must not start mid-reset, but run.py -- launched as the next
    command -- must be able to take the lock once bootstrap is done, even
    with a background child of git still holding the inherited fd."""
    result = _run(checkout)

    assert result.returncode == 0, result.stderr
    assert "git reset --hard FETCH_HEAD" in checkout["git_log"].read_text()
    assert checkout["probe_log"].read_text().split() == ["held"]

    with _lock_path(checkout["root"]).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


def test_the_python_fallback_refuses_too_when_flock_is_absent(checkout):
    """No util-linux flock on PATH: the python3 fcntl fallback must enforce
    the same lock with the same exit code."""
    tools = checkout["bin_dir"].parent / "tools"
    tools.mkdir()
    for name in ("bash", "dirname", "mkdir", "python3", "env"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    env = {**checkout["env"], "PATH": f"{checkout['bin_dir']}:{tools}"}

    held = _hold_lock(checkout["root"])
    try:
        result = _run(checkout, env)
    finally:
        held.close()

    assert result.returncode == EXIT_LOCKED, result.stderr
    assert not checkout["git_log"].exists()

    result = _run(checkout, env)
    assert result.returncode == 0, result.stderr
    with _lock_path(checkout["root"]).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_no_child_command_inherits_the_lock_fd(checkout):
    """git, pip and python must not hold the lock. If bootstrap is
    SIGKILLed its EXIT trap never runs, and a child still holding the fd
    keeps the lock, so run.py exits 75 until that child dies."""
    result = _run(checkout)

    assert result.returncode == 0, result.stderr
    assert checkout["git_log"].exists()
    assert not checkout["fd_log"].exists(), checkout["fd_log"].read_text()


def test_a_sigkilled_bootstrap_leaves_the_lock_free(checkout):
    env = {**checkout["env"], "KILL_BOOTSTRAP": "1"}
    result = _run(checkout, env)

    assert result.returncode == -9
    # git's background child is still running (sleep 2); it must not be
    # the thing keeping run.py out.
    with _lock_path(checkout["root"]).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_no_lock_tool_is_not_mistaken_for_a_held_lock(checkout):
    """Neither flock nor python3 on PATH is a broken image, not a run in
    progress. Reporting 75 would make the launcher skip quietly forever."""
    tools = checkout["bin_dir"].parent / "tools"
    tools.mkdir()
    for name in ("bash", "dirname", "mkdir", "env"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    env = {**checkout["env"], "PATH": str(tools)}

    result = _run(checkout, env)

    assert result.returncode not in (0, EXIT_LOCKED)
    assert "neither flock nor python3" in result.stderr
    assert "holds" not in result.stderr
    assert not checkout["git_log"].exists()


# --- provisional `started` -------------------------------------------------
# short-list's reaper fires at :00, the same minute the launcher bootstraps.
# Mid-bootstrap it saw the PREVIOUS run's `done` beside its `started` and
# stopped the sandbox; with neither marker it fell back to Sandbox.createdAt
# and judged a long-lived sandbox orphaned. Given a job, bootstrap marks the
# run started itself, under the lock, before any git or pip.


def _markers(root: Path):
    run_dir = root / RUN_DIR
    return run_dir / STARTED_NAME, run_dir / DONE_NAME


def _seed_previous_run(root: Path):
    started, done = _markers(root)
    started.parent.mkdir(parents=True, exist_ok=True)
    started.write_text('{"started_at": 1.0, "job": "pipeline", "git_sha": "old"}')
    done.write_text('{"exit_code": 0, "finished_at": 2.0, "job": "pipeline"}')
    return started.read_text(), done.read_text()


def test_a_job_arg_marks_the_run_started_before_any_git(checkout):
    _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])
    snap = checkout["bin_dir"].parent / "snap.log"
    env = {**checkout["env"], "SNAP_LOG": str(snap)}

    result = _run(checkout, env, args=("main", "pipeline"))

    assert result.returncode == 0, result.stderr
    lines = snap.read_text().splitlines()
    assert "done" not in lines, "previous run's done still beside started"
    payload = json.loads(lines[-1])
    assert set(payload) == {"started_at", "job", "provisional"}
    assert payload["job"] == "pipeline"
    assert payload["provisional"] is True
    assert isinstance(payload["started_at"], float)
    assert 1e9 < payload["started_at"] < 1e11, "started_at must be epoch seconds"

    # Success writes no `done`: run.py overwrites `started` with its own.
    assert not done.exists()
    assert json.loads(started.read_text()) == payload
    assert not list(started.parent.glob("*.tmp*"))


def test_no_job_arg_writes_no_markers(checkout):
    """Old launchers pass only a revision; they must see no change."""
    before = _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])

    result = _run(checkout, args=("main",))

    assert result.returncode == 0, result.stderr
    assert (started.read_text(), done.read_text()) == before


def test_a_failure_after_the_marker_closes_it_with_done(checkout):
    """A bare `started` blocks launches for 3h. A bootstrap that dies after
    writing one must leave `done` with its exit code beside it."""
    _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])
    env = {**checkout["env"], "GIT_FAIL": "3"}

    result = _run(checkout, env, args=("main", "canary"))

    assert result.returncode == 3
    begun = json.loads(started.read_text())
    assert begun["provisional"] is True
    payload = json.loads(done.read_text())
    assert set(payload) == {"exit_code", "finished_at", "job"}
    assert payload["exit_code"] == 3
    assert payload["job"] == "canary"
    assert 1e9 < payload["finished_at"] < 1e11
    assert payload["finished_at"] >= begun["started_at"]
    assert not list(started.parent.glob("*.tmp*"))
    with _lock_path(checkout["root"]).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_held_lock_leaves_the_markers_alone(checkout):
    """The markers belong to the run holding the lock."""
    before = _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])
    held = _hold_lock(checkout["root"])
    try:
        result = _run(checkout, args=("main", "pipeline"))
    finally:
        held.close()

    assert result.returncode == EXIT_LOCKED
    assert (started.read_text(), done.read_text()) == before


def test_no_lock_tool_leaves_the_markers_alone(checkout):
    before = _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])
    tools = checkout["bin_dir"].parent / "tools"
    tools.mkdir()
    for name in ("bash", "dirname", "mkdir", "env"):
        (tools / name).symlink_to(shutil.which(name))
    env = {**checkout["env"], "PATH": str(tools)}

    result = _run(checkout, env, args=("main", "pipeline"))

    assert result.returncode == 69
    assert (started.read_text(), done.read_text()) == before


@pytest.mark.parametrize("job", ['pipe"line', "Pipeline", "a b", "x;y", ""])
def test_an_invalid_job_exits_64_before_touching_markers(checkout, job):
    before = _seed_previous_run(checkout["root"])
    started, done = _markers(checkout["root"])

    result = _run(checkout, args=("main", job))

    assert result.returncode == 64
    assert "job" in result.stderr
    assert (started.read_text(), done.read_text()) == before
    assert not checkout["git_log"].exists()


def test_the_python_fallback_writes_the_marker_too(checkout):
    """No flock (or date) on PATH: the marker still lands via python3."""
    tools = checkout["bin_dir"].parent / "tools"
    tools.mkdir()
    for name in ("bash", "dirname", "mkdir", "python3", "env", "rm", "mv"):
        (tools / name).symlink_to(shutil.which(name))
    env = {**checkout["env"], "PATH": f"{checkout['bin_dir']}:{tools}"}
    started, done = _markers(checkout["root"])

    result = _run(checkout, env, args=("main", "pipeline"))

    assert result.returncode == 0, result.stderr
    assert json.loads(started.read_text())["provisional"] is True
    assert not done.exists()
