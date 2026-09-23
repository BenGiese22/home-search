#!/usr/bin/env bash
#
# Bring a Vercel Sandbox checkout to a runnable state. Idempotent: the
# launcher calls it before every run, and a warm sandbox should finish it in
# seconds.
#
#     bash ops/sandbox/bootstrap.sh [revision]
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
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

echo "== bootstrap: $REVISION =="

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
# `flock` is util-linux, present on the sandbox image, but the python3
# fallback costs nothing and fcntl.flock is the same lock run.py takes.
EXIT_LOCKED=75
LOCK_PATH="data/.run/lock"

lock_fd() {  # lock_fd lock|unlock -- operates on fd 9
    if command -v flock >/dev/null 2>&1; then
        if [ "$1" = lock ]; then flock -n 9; else flock -u 9; fi
    else
        python3 -c "import fcntl, sys; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB if sys.argv[1] == 'lock' else fcntl.LOCK_UN)" "$1"
    fi
}

mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>>"$LOCK_PATH"
if ! lock_fd lock 2>/dev/null; then
    echo "bootstrap: a run holds $LOCK_PATH; not touching its checkout (exit $EXIT_LOCKED)" >&2
    exit "$EXIT_LOCKED"
fi
trap 'lock_fd unlock || true; exec 9>&-' EXIT

# --- source ---------------------------------------------------------------
# The clone is shallow (depth 1), so fetch the revision by name rather than
# assuming any history is present.
git fetch --depth 1 origin "$REVISION"
git reset --hard FETCH_HEAD
echo "revision: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

# --- python ---------------------------------------------------------------
if [ ! -x venv/bin/python ]; then
    echo "creating venv"
    python3 -m venv venv
fi

# Cheap when everything is already installed, which is the warm case.
venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

# --- chromium -------------------------------------------------------------
# install-deps needs root and is the one step that can legitimately fail on an
# image whose libraries are already present, so it warns rather than aborts.
# `playwright install chromium` is the real gate: if the browser is genuinely
# missing, nothing downstream can scrape and the run should stop here.
if [ ! -d "${HOME}/.cache/ms-playwright" ]; then
    if command -v sudo >/dev/null 2>&1; then
        sudo venv/bin/python -m playwright install-deps chromium \
            || echo "warning: install-deps failed; continuing (the image may already carry them)"
    else
        venv/bin/python -m playwright install-deps chromium \
            || echo "warning: install-deps failed and sudo is unavailable; continuing"
    fi
fi
venv/bin/python -m playwright install chromium

# --- report ---------------------------------------------------------------
# Printed every run: when a sandbox misbehaves, the first question is always
# which versions it is actually holding.
echo "python:     $(venv/bin/python --version 2>&1)"
echo "playwright: $(venv/bin/python -m playwright --version 2>&1)"
echo "== bootstrap ok =="
