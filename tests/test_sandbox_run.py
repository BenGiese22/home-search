"""The sandbox runner's contract with the launcher and the reaper.

Those two read `data/.run/started` and `data/.run/done` off the sandbox disk
and decide from them alone whether a run is going, whether to start one, and
whether to stop a VM that is billing. Everything asserted here is something
one of them would get wrong if the marker were missing, stale, or malformed.

No test starts a real subprocess: the runner is injected.
"""

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from ops.sandbox import run as runner_mod
from ops.sandbox.run import (
    DONE_NAME,
    EXIT_LOCKED,
    EXIT_USAGE,
    JOBS,
    RUN_DIR,
    STARTED_NAME,
    git_sha,
    main,
    run_job,
    tee_runner,
    write_marker,
)


@pytest.fixture(autouse=True)
def stay_put():
    """main() chdirs to the repo root, because every path below it -- here and
    inside pipeline.py and every stage it starts -- is repo-relative. In a
    test that root is a tmp_path, so it has to be put back."""
    before = os.getcwd()
    yield
    os.chdir(before)


@pytest.fixture
def root(tmp_path):
    (tmp_path / RUN_DIR).mkdir(parents=True)
    return tmp_path


def markers(root):
    started = root / RUN_DIR / STARTED_NAME
    done = root / RUN_DIR / DONE_NAME
    return (
        json.loads(started.read_text()) if started.exists() else None,
        json.loads(done.read_text()) if done.exists() else None,
    )


def ok(_argv, _log):
    return 0


# --- marker shape ---------------------------------------------------------
# The TypeScript parsers reject a marker whose fields are the wrong type, and
# a rejected marker is indistinguishable from no run at all.

def test_started_marker_carries_what_the_launcher_reads(root):
    run_job("pipeline", root=root, runner=ok, now=lambda: 1000.0, sha="abc123")

    started, _ = markers(root)
    assert started == {"started_at": 1000.0, "job": "pipeline", "git_sha": "abc123"}


def test_done_marker_carries_what_the_reaper_reads(root):
    run_job("canary", root=root, runner=lambda a, l: 3, now=lambda: 2000.0, sha="")

    _, done = markers(root)
    assert done == {"exit_code": 3, "finished_at": 2000.0, "job": "canary"}


def test_a_failing_child_is_reported_as_a_failing_run(root):
    code = run_job("pipeline", root=root, runner=lambda a, l: 1, sha="")

    assert code == 1
    _, done = markers(root)
    assert done["exit_code"] == 1


# --- ordering -------------------------------------------------------------

def test_started_exists_and_done_does_not_while_the_child_runs(root):
    """The whole in-progress signal. If `done` outlived the start of the next
    run, the launcher would see a finished run and start a second one on top
    of it."""
    seen = {}

    def observe(_argv, _log):
        seen["markers"] = markers(root)
        return 0

    run_job("pipeline", root=root, runner=observe, sha="")

    started, done = seen["markers"]
    assert started is not None
    assert done is None


def test_a_previous_runs_done_is_cleared_before_the_new_start(root):
    write_marker(
        root / RUN_DIR / DONE_NAME,
        {"exit_code": 0, "finished_at": 1.0, "job": "canary"},
    )

    stale = {}

    def observe(_argv, _log):
        stale["done"] = (root / RUN_DIR / DONE_NAME).exists()
        return 0

    run_job("pipeline", root=root, runner=observe, sha="")

    assert stale["done"] is False


def test_a_crashing_runner_still_closes_the_marker_pair(root):
    """Otherwise the sandbox looks permanently in-progress: the launcher
    refuses every later run and only the reaper's age limit recovers it."""
    def explode(_argv, _log):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_job("pipeline", root=root, runner=explode, now=lambda: 5.0, sha="")

    _, done = markers(root)
    assert done == {"exit_code": 70, "finished_at": 5.0, "job": "pipeline"}


def test_a_marker_is_never_half_written(root):
    """Renamed into place, so a reader polling every ten minutes against a
    sandbox that can be killed mid-write sees a whole marker or none."""
    path = root / RUN_DIR / STARTED_NAME
    write_marker(path, {"started_at": 1.0, "job": "pipeline", "git_sha": ""})

    assert json.loads(path.read_text())["job"] == "pipeline"
    assert not list((root / RUN_DIR).glob("*.tmp"))


# --- dispatch -------------------------------------------------------------

@pytest.mark.parametrize(
    "job,expected",
    [("pipeline", ["pipeline.py", "--max-age=5h"]), ("canary", ["ops/canary.py"])],
)
def test_each_job_runs_its_own_script(root, job, expected):
    calls = []

    run_job(job, root=root, runner=lambda a, l: calls.append(a) or 0, sha="")

    assert calls[0][1:] == expected


def test_the_pipeline_job_carries_a_freshness_guard(root):
    """A double-fired cron must not re-scrape. pipeline.py's own guard is what
    makes a redundant trigger free, and it only applies when asked for."""
    assert any(a.startswith("--max-age=") for a in JOBS["pipeline"])


def test_an_unknown_job_is_refused(root):
    assert main(["nonsense"], root=root, runner=ok) == EXIT_USAGE
    assert markers(root) == (None, None)


def test_no_job_is_refused(root):
    assert main([], root=root, runner=ok) == EXIT_USAGE


# --- the lock -------------------------------------------------------------

def test_a_second_runner_exits_without_touching_the_markers(root):
    """The markers belong to the run holding the lock. Rewriting `started`
    here would reset its apparent age and hide a hang from the reaper."""
    import fcntl

    write_marker(
        root / RUN_DIR / STARTED_NAME,
        {"started_at": 1.0, "job": "pipeline", "git_sha": "held"},
    )
    holder = (root / RUN_DIR / "lock").open("w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code = main(["pipeline"], root=root, runner=ok)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert code == EXIT_LOCKED
    started, _ = markers(root)
    assert started["git_sha"] == "held"


def test_the_lock_is_released_so_the_next_run_can_take_it(root):
    """A run that held the lock and never gave it back would wedge the sandbox
    until the reaper stopped it."""
    assert main(["canary"], root=root, runner=ok) == 0
    assert main(["canary"], root=root, runner=ok) == 0


# --- git sha --------------------------------------------------------------

def test_git_sha_is_best_effort(root):
    def fails(*a, **k):
        raise OSError("no git")

    assert git_sha(root, run=fails) == ""


def test_git_sha_ignores_a_failed_command(root):
    def failed(*a, **k):
        return subprocess.CompletedProcess([], 128, stdout="", stderr="fatal")

    assert git_sha(root, run=failed) == ""


# --- secrets --------------------------------------------------------------

def test_the_runner_never_reads_the_environment():
    """Secrets pass through this process only to be inherited by the child.
    Reading one is the first step toward logging one, and this module's output
    is captured by the Vercel side.

    Parsed rather than grepped: a previous guard of this kind in check.py
    matched its own explanatory comment and passed while the code was wrong.
    """
    tree = ast.parse(Path(runner_mod.__file__).read_text())

    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
    ]
    assert reads == []


# --- tee ------------------------------------------------------------------

def test_output_reaches_both_the_log_and_stdout(root, capsys):
    """The log survives on the sandbox disk; stdout is what anyone watching
    from the Vercel side sees during a run that is otherwise silent for hours."""
    log = root / "data" / "logs" / "canary-x.log"

    code = tee_runner(["printf", "hello\\n"], log)

    assert code == 0
    assert log.read_text() == "hello\n"
    assert "hello" in capsys.readouterr().out
