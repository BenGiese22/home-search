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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_env, this_home
from src.notify import notify
from src.db import (
    acquire_pipeline_lease,
    release_pipeline_lease,
    renew_pipeline_lease,
)
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
    # Flags this stage accepts, keyed by the pipeline flag that turns them on.
    # Generalises what takes_scrape_flags does for scrape: a caller needs a way
    # to reach one stage's options without every stage seeing every flag.
    forwards: dict[str, str] = field(default_factory=dict)


# publish.py is gone: the stages write Turso directly, so there is nothing
# to mirror. The one part of it that had to survive -- telling the viewer its
# data changed -- is the revalidate POST at the end of run_pipeline().
STAGES: tuple[Stage, ...] = (
    Stage("scrape", "scrape.py", takes_scrape_flags=True),
    # --force-commutes recomputes every listing rather than only the ones
    # missing a usable row. Nothing else invalidates a commute row, so this is
    # the only way to re-measure the corpus after changing how commutes are
    # computed -- and it has to run inside a full pipeline rather than alone,
    # or `scores` keeps the durations the old measurement produced.
    Stage("commutes", "compute_commutes.py", forwards={"--force-commutes": "--force"}),
    Stage("score-photos", "score_photos.py"),
    Stage("score", "score.py"),
    # Last, and deliberately able to fail the run.
    #
    # Every stage above can succeed while producing something wrong -- three
    # defects in a row exited 0 with plausible output. This one states what
    # must be true and turns a violation into a non-zero exit code, which is
    # the signal run.py records and the reaper turns into a notification.
    Stage("verify", "verify.py"),
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


def _forwarded(stage: Stage, requested) -> list[str]:
    """The stage-specific flags a caller asked for, in declaration order."""
    return [flag for trigger, flag in stage.forwards.items() if trigger in requested]


def _collect_forwarded(argv) -> list[str]:
    """The stage triggers present on the command line.

    Separate from SCRAPE_FLAGS on purpose: those are matched by exact name
    and by prefix, and SCRAPE_FLAGS already contains --force. A trigger has
    to be its own word (--force-commutes) so neither list claims the other's
    flag.
    """
    return [trigger for stage in STAGES for trigger in stage.forwards if trigger in argv]


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


def _default_notify(title: str, message: str) -> bool:
    """Push a pipeline failure to a phone.

    Only failures. A nightly success that says so is a notification people
    learn to swipe away, and by the time one matters they no longer read it;
    the canary is what proves the pipeline is alive. Unset NTFY_TOPIC is a
    silent no-op, which is exactly the behaviour of every run before this.

    Never raises and never blocks a run -- see src/notify.py. A failed
    notification about a failed run must not become the thing that hides it.
    """
    return notify(
        load_env().get("NTFY_TOPIC", ""),
        title,
        message,
        priority="high",
        tags=("rotating_light",),
    )


def _lease_connection():
    """The database the cross-home lease lives in.

    Its own function so a test can substitute it without opening a session,
    and so the ~3.4s ensure_schema() that creates the table on first use
    happens in exactly one place. Imported lazily: `pipeline.py --dry-run`
    and `--help` have no business requiring Turso credentials.
    """
    from src.turso_db import stage_connection

    return stage_connection()


def run_pipeline(
    stages,
    runner=None,
    scrape_flags=None,
    dry_run=False,
    marker=None,
    max_age_hours=0.0,
    log_handle=None,
    revalidate_fn=None,
    renew_lease=None,
    notify_fn=None,
    forwarded=(),
) -> int:
    # Late-bound rather than default arguments so tests (and any caller) can
    # substitute them by patching the module attribute.
    if revalidate_fn is None:
        revalidate_fn = _default_revalidate
    if runner is None:
        runner = _default_runner
    if notify_fn is None:
        notify_fn = _default_notify

    def alert(title: str, message: str) -> None:
        """Notify without ever becoming the failure.

        src/notify.py already swallows delivery errors, but the call reaches
        it through load_env(), and a malformed .env raising here would kill
        the run at precisely the moment it was trying to report one. A
        notification that cannot be sent is a notification that cannot be
        sent; it is not a reason to lose the exit code that says what broke.
        """
        try:
            notify_fn(title, message)
        except Exception as exc:  # noqa: BLE001 -- the whole point
            print(f"pipeline: notification failed ({type(exc).__name__}: {exc})",
                  flush=True)

    if marker is not None and is_fresh(marker, max_age_hours):
        raise Skipped(f"last successful run was under {max_age_hours}h ago")

    scrape_flags = scrape_flags or []
    if dry_run:
        for stage in stages:
            argv = [sys.executable, stage.script]
            if stage.takes_scrape_flags:
                argv += scrape_flags
            argv += _forwarded(stage, forwarded)
            print(f"  would run: {' '.join(argv)}")
        return 0

    for stage in stages:
        # Push the lease out at each boundary. Nothing renews *during* a
        # stage -- score_photos.py can poll a vision batch for hours, which
        # is why the lease is long enough to cover one on its own -- but a
        # full run may still outlast a single lease, and four extra
        # statements a run is a cheap way to say so.
        if renew_lease is not None and not renew_lease():
            # We no longer hold it, which means the other home very likely
            # already started. Aborting here would not undo that and would
            # leave the data half refreshed, so this is loud and continues.
            print(
                "pipeline: WARNING lost the cross-home lease mid-run; "
                "another home may be running against the same database",
                flush=True,
            )
            # Worth waking someone for. Two homes writing the same database
            # is the one condition here that can cost real money -- a
            # concurrent scrape re-downloads photos Compass is already
            # rate-limiting us on -- and unlike a failed stage it leaves no
            # non-zero exit code behind for anything else to notice.
            alert(
                "home-search: lost the pipeline lease",
                f"A {this_home()} run lost the cross-home lease mid-run at "
                f"stage {stage.name}; another home may be running.",
            )
        argv = [sys.executable, stage.script]
        if stage.takes_scrape_flags:
            argv += scrape_flags
        argv += _forwarded(stage, forwarded)
        started = time.monotonic()
        print(f"[{stage.name}] {' '.join(argv)}", flush=True)
        code = runner(argv, log_handle=log_handle)
        elapsed = time.monotonic() - started
        if code != 0:
            print(f"[{stage.name}] FAILED (exit {code}) after {elapsed:.0f}s", flush=True)
            # Which stage, because that is the whole diagnostic. The sandbox
            # reaper also pushes on a non-zero run, but it only knows the run
            # failed; this knows where. Both stay: the reaper's exists for
            # the case this process was killed before it could say anything,
            # and a duplicate push about a real failure beats a missed one.
            alert(
                f"home-search: {stage.name} failed",
                f"The {stage.name} stage exited {code} after {elapsed:.0f}s "
                f"on {this_home()}. Later stages were skipped.",
            )
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

    forwarded = _collect_forwarded(argv)
    scrape_flags = [a for a in argv if a in SCRAPE_FLAGS]
    scrape_flags += [a for a in argv if a.startswith(SCRAPE_FLAG_PREFIXES)]
    dry_run = "--dry-run" in argv
    max_age_hours = _parse_max_age(argv)

    if dry_run:
        print("pipeline --dry-run:")
        return run_pipeline(
            stages, scrape_flags=scrape_flags, dry_run=True, forwarded=forwarded
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # A photo-scoring batch can hold the lock for hours. A trigger landing
        # during one is not a failure -- exit 0 so systemd does not flag it.
        print("pipeline: another run is in progress; exiting")
        return 0

    # Then the cross-home lease. The flock above stays and runs first: it is
    # instant, costs nothing, and works with the network down, so the
    # same-machine case never pays for a Turso round-trip. What it cannot do
    # is see the other execution home -- a sandbox run shares this database
    # and nothing else -- and two overlapping runs are how the photo
    # migration would have re-downloaded ~700 MB from Compass. See issue #59.
    #
    # No try/except around the connection on purpose: every stage writes to
    # this same database, so a run that cannot reach it was never going to
    # work, and failing here says why in one traceback instead of four
    # stages in.
    lease_conn = _lease_connection()
    lease_token = uuid.uuid4().hex
    lease = acquire_pipeline_lease(lease_conn, this_home(), lease_token)
    if not lease.mine:
        # Same contract as the flock path: exit 0. Overlapping triggers are
        # the expected shape of "trigger liberally, guard on freshness", not
        # a fault. Name the holder -- with two homes, "another run" alone
        # leaves an operator with nothing to go and look at.
        print(
            f"pipeline: {lease.held_by} has held the cross-home lease since "
            f"{lease.acquired_at}Z (expires {lease.expires_at}Z); exiting"
        )
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
                forwarded=forwarded,
                renew_lease=lambda: renew_pipeline_lease(lease_conn, lease_token),
            )
    except Skipped as exc:
        print(f"pipeline: skipped ({exc})")
        log_path.unlink(missing_ok=True)
        return 0
    finally:
        # Release before the flock, and by token: the expiry is the backstop
        # for a crash, not the normal way the lock comes free. Releasing by
        # token means a run whose lease already expired and was taken over
        # cannot delete the new holder's row on its way out.
        release_pipeline_lease(lease_conn, lease_token)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

    print(f"pipeline: {'ok' if code == 0 else f'FAILED (exit {code})'} in {time.monotonic() - started:.0f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
