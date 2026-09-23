#!/usr/bin/env bash
#
# Bring a Vercel Sandbox checkout to a runnable state. Idempotent: the
# launcher calls it before every run, and a warm sandbox should finish it in
# seconds.
#
#     bash ops/sandbox/bootstrap.sh [revision] [job]
#
# Given a job (the name run.py will be started with), bootstrap also marks
# the run started before it does anything else -- see "provisional marker"
# below. Without one it writes no markers at all, as before.
#
# The sandbox is persistent, so this is a cold path once and a no-op after.
# Everything expensive -- the venv, the pip install, Chromium -- is skipped
# when already satisfied.
#
# It deliberately never runs `git clean`. `data/` and `venv/` are gitignored,
# which is exactly why `git reset --hard` is safe here: the tracked tree is
# replaced and the photo cache, the Compass session and the venv survive. A
# `git clean -xdf` would delete all three, and losing `data/.auth/` costs a
# cold login on every run thereafter.
set -euo pipefail

REVISION="${1:-main}"
EXIT_USAGE=64  # EX_USAGE

# The job goes into a JSON marker verbatim, so it must be a plain token.
# Checked before the lock and before any marker is touched.
JOB=""
if [ "$#" -ge 2 ]; then
    JOB="$2"
    if ! [[ "$JOB" =~ ^[a-z0-9_-]+$ ]]; then
        echo "bootstrap: job '$JOB' is not a simple token ([a-z0-9_-]+) (exit $EXIT_USAGE)" >&2
        exit "$EXIT_USAGE"
    fi
fi

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

echo "== bootstrap: $REVISION${JOB:+ ($JOB)} =="

# --- run lock -------------------------------------------------------------
# Everything below rewrites the checkout (`git reset --hard`, `pip install`)
# that a running pipeline is executing from. The launcher's in-progress
# guard is supposed to keep bootstrap away from a live run, and Sep 8-19 it
# did not: a seconds-vs-milliseconds bug in reading `started` let the 03:30
# canary launch bootstrap straight into the checkout of a run still going.
# So bootstrap checks run.py's own lock itself, before any git or pip.
#
# Same file, same exit code as run.py (data/.run/lock, EX_TEMPFAIL 75), so
# the launcher can treat both as "skipped, a run is in progress".
#
# The lock is held for all of bootstrap, so a run cannot start mid-reset
# either, and released explicitly on exit -- the launcher runs run.py as a
# separate command right after, and it must be able to take the lock. The
# explicit unlock matters: flock locks belong to the open file, which every
# child inherits, so a background process git leaves behind (auto gc) would
# otherwise keep holding it after bootstrap is gone.
#
# The unlock is not enough on its own. A SIGKILLed bootstrap never runs its
# EXIT trap, and then any child still holding fd 9 holds the lock, and run.py
# exits 75 until that child dies. So no child gets fd 9 at all: every git,
# pip and python below runs through `nolock`, which closes it for that
# command. Only lock_fd itself needs it.
#
# `flock` is util-linux, present on the sandbox image, but the python3
# fallback costs nothing and fcntl.flock is the same lock run.py takes. With
# neither, there is no way to take the lock -- a broken image, not a run in
# progress -- so that exits EX_UNAVAILABLE rather than 75, which the
# launcher would read as "skipped" on every run forever.
EXIT_LOCKED=75
EXIT_NO_LOCK_TOOL=69
LOCK_PATH="data/.run/lock"

nolock() {  # run a command without the lock fd
    "$@" 9>&-
}

lock_fd() {  # lock_fd lock|unlock -- operates on fd 9
    if command -v flock >/dev/null 2>&1; then
        if [ "$1" = lock ]; then flock -n 9; else flock -u 9; fi
    else
        python3 -c "import fcntl, sys; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB if sys.argv[1] == 'lock' else fcntl.LOCK_UN)" "$1"
    fi
}

if ! command -v flock >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
    echo "bootstrap: neither flock nor python3 is on PATH, so $LOCK_PATH cannot be taken; fix the image (exit $EXIT_NO_LOCK_TOOL)" >&2
    exit "$EXIT_NO_LOCK_TOOL"
fi

mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>>"$LOCK_PATH"
if ! lock_fd lock 2>/dev/null; then
    echo "bootstrap: a run holds $LOCK_PATH; not touching its checkout (exit $EXIT_LOCKED)" >&2
    exit "$EXIT_LOCKED"
fi

# --- provisional marker ---------------------------------------------------
# short-list's reaper fires at :00, the same minute the launcher runs
# bootstrap. Until run.py wrote its own `started`, the reaper saw either the
# PREVIOUS run's pair -- `done` beside `started`, "finished", so it stopped
# the sandbox mid-bootstrap -- or no markers at all, and then judged age by
# Sandbox.createdAt, which on a long-lived sandbox is days old, so it
# reaped it as orphaned. Bootstrap already holds the run lock, so it marks
# the run started here, before any git or pip; run.py overwrites this
# `started` with its real one when it takes the lock next.
#
# Same rules as run.py's run_job: timestamps are epoch SECONDS (a float,
# like time.time()), every write is temp file + rename in the same dir, and
# the previous `done` is removed BEFORE `started` is written, or a stale
# `done` would sit beside a fresh `started` and read as finished.
#
# A bootstrap that fails after this point closes the pair with `done` in the
# EXIT trap: a bare `started` reads as "in progress" and blocks launches for
# the 3h sandbox timeout. A successful one writes no `done` -- run.py is
# about to start. (A SIGKILLed one runs no trap; that `started` ages out.)
STARTED_PATH="data/.run/started"
DONE_PATH="data/.run/done"
MARKED=0

now_secs() {
    if command -v python3 >/dev/null 2>&1; then
        nolock python3 -c 'import time; print(repr(time.time()))'
    else
        nolock date +%s.%N
    fi
}

write_marker() {  # write_marker PATH JSON -- atomic within data/.run
    local tmp="$1.tmp.$$"
    printf '%s\n' "$2" >"$tmp" && nolock mv -f "$tmp" "$1"
}

on_exit() {
    local code=$?
    if [ "$MARKED" = 1 ] && [ "$code" -ne 0 ]; then
        write_marker "$DONE_PATH" \
            "{\"exit_code\": $code, \"finished_at\": $(now_secs || echo 0), \"job\": \"$JOB\"}" \
            || echo "bootstrap: could not write $DONE_PATH" >&2
    fi
    lock_fd unlock || true
    exec 9>&-
}
trap on_exit EXIT

if [ -n "$JOB" ]; then
    started_at="$(now_secs)"  # a bare assignment, so set -e sees a failure
    nolock rm -f "$DONE_PATH"
    write_marker "$STARTED_PATH" \
        "{\"started_at\": $started_at, \"job\": \"$JOB\", \"provisional\": true}"
    MARKED=1
fi

# --- source ---------------------------------------------------------------
# The clone is shallow (depth 1), so fetch the revision by name rather than
# assuming any history is present.
nolock git fetch --depth 1 origin "$REVISION"
nolock git reset --hard FETCH_HEAD
echo "revision: $(nolock git rev-parse --short HEAD) $(nolock git log -1 --format=%s)"

# --- python ---------------------------------------------------------------
if [ ! -x venv/bin/python ]; then
    echo "creating venv"
    nolock python3 -m venv venv
fi

# Cheap when everything is already installed, which is the warm case.
nolock venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

# --- chromium -------------------------------------------------------------
# install-deps needs root and is the one step that can legitimately fail on an
# image whose libraries are already present, so it warns rather than aborts.
# `playwright install chromium` is the real gate: if the browser is genuinely
# missing, nothing downstream can scrape and the run should stop here.
if [ ! -d "${HOME}/.cache/ms-playwright" ]; then
    if command -v sudo >/dev/null 2>&1; then
        nolock sudo venv/bin/python -m playwright install-deps chromium \
            || echo "warning: install-deps failed; continuing (the image may already carry them)"
    else
        nolock venv/bin/python -m playwright install-deps chromium \
            || echo "warning: install-deps failed and sudo is unavailable; continuing"
    fi
fi
nolock venv/bin/python -m playwright install chromium

# --- report ---------------------------------------------------------------
# Printed every run: when a sandbox misbehaves, the first question is always
# which versions it is actually holding.
echo "python:     $(nolock venv/bin/python --version 2>&1)"
echo "playwright: $(nolock venv/bin/python -m playwright --version 2>&1)"
echo "== bootstrap ok =="
