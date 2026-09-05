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
