"""Run one job inside the sandbox and leave a record of what happened.

    venv/bin/python ops/sandbox/run.py pipeline
    venv/bin/python ops/sandbox/run.py canary

This is the only thing the launcher starts, and it is deliberately thin: it
dispatches to a script that already exists and writes two marker files around
it.

## Why markers rather than a callback

The runner holds no Vercel credential and cannot report in. An OIDC token
expires mid-run (2h TTL against a pipeline that can run for hours) and a
personal token would give a VM driving Chromium against a third-party site
team-wide access. So the run leaves its state on the sandbox's own disk, and
the reaper -- which does hold a credential, because it is a Vercel function --
reads it from outside.

`started` without `done` means a run is in progress -- unless `started` is
older than the 3h sandbox timeout, which the launcher ignores (short-list
PR #28): the sandbox that wrote it cannot still be alive. The reaper stops a
sandbox stuck that way past its age limit. Both present means the run
finished and the sandbox is billing for nothing.

Every timestamp in the markers (`started_at`, `finished_at`) is epoch
SECONDS, from time.time(). short-list is JavaScript, where Date.now() is
milliseconds, so its reader must multiply by 1000 before comparing. The
Sep 8-19 outage was exactly that mismatch: read as milliseconds, every
`started` looked decades stale, the in-progress guard never held, and the
03:30 canary bootstrapped into a pipeline run still going. bootstrap.sh now
checks the lock itself (exit 75, same as EXIT_LOCKED), but the guard is
still the launcher's first line.

run.py is not the only writer of `started`. A launcher that passes the job
to bootstrap (`bootstrap.sh <revision> <job>`) gets a provisional marker
written under the same lock before any git or pip -- the previous `done`
removed, then `{"started_at", "job", "provisional": true}` -- so the reaper
never sees the last run's finished pair, or no markers at all, while
bootstrap works. run_job overwrites it with the real marker (no
`provisional` key). A bootstrap that fails after writing it closes the pair
itself with `done` and its exit code; one that succeeds leaves `started`
alone for this script.

Stdlib only, and it never reads or prints an environment variable. The
secrets arrive in this process's environment purely to be inherited by the
child.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = Path("data/.run")
LOG_DIR = Path("data/logs")

LOCK_NAME = "lock"
STARTED_NAME = "started"
DONE_NAME = "done"

# EX_TEMPFAIL. Not a failure: another runner already holds the markers, and
# the right response is to leave them alone and let that run finish.
EXIT_LOCKED = 75
EXIT_USAGE = 2

# The pipeline's own freshness guard. Five hours against a nightly cron means
# a manually triggered run an hour later still does the work, while a double
# fire in the same window does not.
JOBS: dict[str, list[str]] = {
    "pipeline": ["pipeline.py", "--max-age=5h"],
    "canary": ["ops/canary.py"],
}


def git_sha(root: Path, run=subprocess.run) -> str:
    """Which revision is actually running. Best effort: a missing .git is not
    worth failing a run over, and the marker's readers all tolerate ''."""
    try:
        result = run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def write_marker(path: Path, payload: dict) -> None:
    """Write a marker so no reader can ever see half of one.

    The reaper polls every ten minutes and the sandbox can be killed at any
    moment, so a marker must appear complete or not at all. Rename is atomic
    within a directory; a plain write is not.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def tee_runner(argv: list[str], log_path: Path) -> int:
    """Run the child with its output going to the log *and* to our stdout.

    Both, because they answer different questions. The log survives on the
    sandbox disk for the next run to find; our stdout is what `cmd.logs()`
    returns to anyone watching from the Vercel side, who otherwise sees a
    three-hour silence.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        return process.wait()


def run_job(
    job: str,
    *,
    root: Path = ROOT,
    runner=tee_runner,
    now=time.time,
    sha=None,
) -> int:
    """Bracket one job between its two markers. Returns the child's exit code.

    Assumes the caller already holds the lock.
    """
    script = JOBS[job]
    run_dir = root / RUN_DIR
    done_path = run_dir / DONE_NAME

    # Order matters. Clear the previous run's `done` *before* announcing this
    # one, or there is a window where a stale `done` sits beside a fresh
    # `started` and both the launcher and the reaper read the run as finished
    # the moment it begins.
    done_path.unlink(missing_ok=True)
    write_marker(
        run_dir / STARTED_NAME,
        {
            # Epoch seconds, not ms -- see the module docstring.
            "started_at": now(),
            "job": job,
            "git_sha": sha if sha is not None else git_sha(root),
        },
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    argv = [sys.executable, *script]
    print(f"[run] {job}: {' '.join(argv)}", flush=True)
    try:
        code = runner(argv, root / LOG_DIR / f"{job}-{stamp}.log")
    except BaseException:
        # A crash in the runner itself must still close the marker pair.
        # Leaving only `started` behind reads as "in progress", and the
        # launcher would refuse every subsequent run until that `started`
        # aged past the 3h sandbox timeout (or the reaper's age limit
        # stopped the sandbox) -- a lost night for a crash that took
        # seconds.
        write_marker(
            done_path,
            {"exit_code": 70, "finished_at": now(), "job": job},
        )
        raise

    write_marker(done_path, {"exit_code": code, "finished_at": now(), "job": job})
    print(f"[run] {job}: exit {code}", flush=True)
    return code


def main(argv: list[str] | None = None, root: Path = ROOT, runner=tee_runner) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in JOBS:
        print(f"usage: run.py {{{'|'.join(JOBS)}}}", file=sys.stderr)
        return EXIT_USAGE
    job = args[0]

    # Every relative path below -- and inside pipeline.py, and inside every
    # stage it starts -- is repo-relative.
    os.chdir(root)

    run_dir = root / RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (run_dir / LOCK_NAME).open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Touch nothing. The markers belong to the run that holds the lock,
        # and rewriting `started` here would reset its apparent age and hide
        # a hang from the reaper.
        print("run.py: a run is already in progress; exiting", file=sys.stderr)
        return EXIT_LOCKED

    try:
        return run_job(job, root=root, runner=runner)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
