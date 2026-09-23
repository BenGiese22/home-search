"""bootstrap.sh must not rewrite the checkout of a run that is still going.

It runs `git reset --hard` and `pip install` in the same directory run.py
executes the pipeline from. Sep 8-19 the launcher's in-progress guard broke
and launched bootstrap into a live run, so bootstrap now checks run.py's lock
itself.

Every test runs a copy of the script in a tmp_path "checkout" with git and
the venv stubbed, so nothing is fetched or installed.
"""

import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from ops.sandbox.run import EXIT_LOCKED, LOCK_NAME, RUN_DIR

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "ops" / "sandbox" / "bootstrap.sh"


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
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
    # A git that records what it was asked, probes the lock mid-bootstrap,
    # and leaves a background child behind holding the inherited lock fd --
    # the shape of an auto-gc git daemonises after a fetch.
    _write_exe(
        bin_dir / "git",
        'echo "git $*" >> "$GIT_LOG"\n'
        'if [ "$1" = reset ]; then\n'
        + _PROBE
        + "    sleep 2 >/dev/null 2>&1 &\n"
        "fi\n"
        'if [ "$1" = rev-parse ]; then echo abc123; fi\n'
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
    }
    return {
        "root": root,
        "script": script,
        "env": env,
        "git_log": git_log,
        "probe_log": probe_log,
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


def _run(checkout, env=None):
    return subprocess.run(
        ["bash", str(checkout["script"])],
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
