"""Run the four pipeline stages in order.

    python pipeline.py                      # full refresh
    python pipeline.py --dry-run            # print the commands, run nothing
    python pipeline.py --from=score         # resume after a partial failure
    python pipeline.py --only=score         # one stage
    python pipeline.py --max-age=6h         # skip if data is already fresh
    python pipeline.py --limit=5 --new-listing   # scrape flags pass through

Each stage runs as a subprocess so it keeps its own __main__ behavior, .env
loading, and crash isolation. Every stage is already incremental -- each one
decides for itself what is stale -- so a redundant run is cheap and a repeated
run never re-pays the vision API.

## Why the freshness guard exists

This machine's uptime is unpredictable: it may be off for two days, or on all
day. A pure wall-clock schedule fits neither -- it either misses every window
while the machine is off, or fires repeatedly while it is on.

The design instead triggers *liberally* (at boot, on an interval, and on a
calendar) and guards on freshness here. Whichever trigger fires first does the
work; the rest find a recent success marker and exit 0 immediately. That gives
a run shortly after the machine comes up -- when the data is staleest and
someone is most likely to look at the viewer -- without doing the work three
times on a day it stays on.

Only a full run records success. A partial run (--only/--from) deliberately
does not reset the clock, since it did not refresh everything.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_env
from src.revalidate import revalidate

DATA_DIR = Path("data")
LOG_DIR = DATA_DIR / "logs"
LOCK_PATH = DATA_DIR / ".pipeline.lock"
MARKER_PATH = DATA_DIR / ".pipeline-last-success.json"

DEFAULT_MAX_AGE_HOURS = 6.0


@dataclass(frozen=True)
class Stage:
    name: str
    script: str
    takes_scrape_flags: bool = False


# publish.py is gone: the stages write Turso directly, so there is nothing
# to mirror. The one part of it that had to survive -- telling the viewer its
# data changed -- is the revalidate POST at the end of run_pipeline().
STAGES: tuple[Stage, ...] = (
    Stage("scrape", "scrape.py", takes_scrape_flags=True),
    Stage("commutes", "compute_commutes.py"),
    Stage("score-photos", "score_photos.py"),
    Stage("score", "score.py"),
)
STAGE_NAMES = [s.name for s in STAGES]


class Skipped(Exception):
    """Raised when a run is a no-op because the data is already fresh."""


def build_plan(only=None, start_from=None) -> list[Stage]:
    for requested in (only, start_from):
        if requested is not None and requested not in STAGE_NAMES:
            raise ValueError(f"unknown stage {requested!r}; expected one of {STAGE_NAMES}")
    if only:
        return [s for s in STAGES if s.name == only]
    stages = list(STAGES)
    if start_from:
        stages = stages[STAGE_NAMES.index(start_from):]
    return stages


def is_fresh(marker: Path, max_age_hours: float) -> bool:
    """True when a full run finished within max_age_hours.

    Any doubt means "not fresh" -- a missing, unreadable, or corrupt marker
    must cause a run rather than suppress one forever.
    """
    if max_age_hours <= 0 or not marker.exists():
        return False
    try:
        finished_at = json.loads(marker.read_text())["finished_at"]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return False
    return (time.time() - float(finished_at)) < max_age_hours * 3600


def record_success(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "finished_at": time.time(),
        "finished_at_iso": datetime.now(timezone.utc).isoformat(),
    }))


def _default_runner(argv, log_handle=None):
    process = subprocess.run(argv, stdout=log_handle, stderr=subprocess.STDOUT)
    return process.returncode


def _default_revalidate() -> bool:
    """Tell the hosted viewer its data changed.

    short-list caches its reads behind cacheTag('listings'), so its whole
    contract with this project is "the Turso database you read is fresh, and
    someone POSTs revalidate after writing". The stages now keep the first
    half directly; this is the second, and the only surviving line of
    publish.py.
    """
    env = load_env()
    missing = [k for k in ("SHORT_LIST_URL", "REVALIDATE_SECRET") if not env.get(k)]
    if missing:
        print(f"skipping revalidate: {', '.join(missing)} not set", flush=True)
        return False
    return revalidate(env["SHORT_LIST_URL"], env["REVALIDATE_SECRET"])


def run_pipeline(
    stages,
    runner=_default_runner,
    scrape_flags=None,
    dry_run=False,
    marker=None,
    max_age_hours=0.0,
    log_handle=None,
    revalidate_fn=None,
) -> int:
    # Late-bound rather than a default argument so tests (and any caller)
    # can substitute it by patching the module attribute.
    if revalidate_fn is None:
        revalidate_fn = _default_revalidate

    if marker is not None and is_fresh(marker, max_age_hours):
        raise Skipped(f"last successful run was under {max_age_hours}h ago")

    scrape_flags = scrape_flags or []
    if dry_run:
        for stage in stages:
            argv = [sys.executable, stage.script]
            if stage.takes_scrape_flags:
                argv += scrape_flags
            print(f"  would run: {' '.join(argv)}")
        return 0

    for stage in stages:
        argv = [sys.executable, stage.script]
        if stage.takes_scrape_flags:
            argv += scrape_flags
        started = time.monotonic()
        print(f"[{stage.name}] {' '.join(argv)}", flush=True)
        code = runner(argv, log_handle=log_handle)
        elapsed = time.monotonic() - started
        if code != 0:
            print(f"[{stage.name}] FAILED (exit {code}) after {elapsed:.0f}s", flush=True)
            # Later stages read what earlier stages write, so continuing would
            # publish results computed from half-updated data.
            return code
        print(f"[{stage.name}] ok in {elapsed:.0f}s", flush=True)

    # Every stage wrote straight to the database the viewer reads, so any
    # successful run -- full or partial -- has changed what it should serve.
    # A failed revalidate is deliberately not a failed run: the writes all
    # landed and the cache expires on its own.
    if stages:
        revalidate_fn()

    if marker is not None and list(stages) == list(STAGES):
        record_success(marker)
    return 0


def _parse_max_age(argv) -> float:
    for arg in argv:
        if arg.startswith("--max-age="):
            raw = arg.split("=", 1)[1].strip().lower().rstrip("h")
            return float(raw)
    return 0.0


def _parse_value(argv, prefix):
    for arg in argv:
        if arg.startswith(prefix):
            return arg.split("=", 1)[1]
    return None


SCRAPE_FLAG_PREFIXES = ("--limit=",)
SCRAPE_FLAGS = ("--skip-photos", "--new-listing", "--force", "--backfill-missing")


def main() -> int:
    argv = sys.argv[1:]
    try:
        stages = build_plan(
            only=_parse_value(argv, "--only="),
            start_from=_parse_value(argv, "--from="),
        )
    except ValueError as exc:
        print(f"pipeline: {exc}")
        return 2

    scrape_flags = [a for a in argv if a in SCRAPE_FLAGS]
    scrape_flags += [a for a in argv if a.startswith(SCRAPE_FLAG_PREFIXES)]
    dry_run = "--dry-run" in argv
    max_age_hours = _parse_max_age(argv)

    if dry_run:
        print("pipeline --dry-run:")
        return run_pipeline(stages, scrape_flags=scrape_flags, dry_run=True)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # A photo-scoring batch can hold the lock for hours. A trigger landing
        # during one is not a failure -- exit 0 so systemd does not flag it.
        print("pipeline: another run is in progress; exiting")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"pipeline-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    started = time.monotonic()
    print(f"pipeline: logging to {log_path}")
    try:
        with log_path.open("w") as log_handle:
            code = run_pipeline(
                stages,
                scrape_flags=scrape_flags,
                marker=MARKER_PATH,
                max_age_hours=max_age_hours,
                log_handle=log_handle,
            )
    except Skipped as exc:
        print(f"pipeline: skipped ({exc})")
        log_path.unlink(missing_ok=True)
        return 0
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    print(f"pipeline: {'ok' if code == 0 else f'FAILED (exit {code})'} in {time.monotonic() - started:.0f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
