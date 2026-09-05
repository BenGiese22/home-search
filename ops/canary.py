"""Prove the sandbox can still reach Compass, cheaply and without writing.

    venv/bin/python ops/canary.py

Run nightly by its own cron, half an hour before nothing else. The pipeline
is expensive and slow; this is the part of it that breaks silently. Compass
can rotate a session, change a selector, or start refusing a datacenter IP,
and every one of those shows up here as a FAIL a day before it would have
shown up as an empty database.

**Read-only, deliberately.** No Turso connection, no photo downloads, no
writes anywhere except the Compass session itself, which the browser refreshes
as a side effect of proving it works. A canary that could corrupt the data it
watches would be worse than no canary.

Output is one JSON line so the sandbox log stays greppable, plus one ntfy
push so a failure reaches a phone rather than a log nobody reads.
"""

import json
import sys
from pathlib import Path
from typing import Callable

# Run as `python ops/<name>.py`, which puts ops/ on sys.path rather than the
# repo root. Same line as ops/reclaim_stranded_blobs.py, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.auth import launch_authenticated_page
from src.config import load_config, load_env
from src.notify import notify
from src.scraper import fetch_collection_tabs

DATA_DIR = Path("data")
AUTH_STATE_PATH = DATA_DIR / ".auth" / "compass_state.json"
LOGIN_URL = "https://www.compass.com/login/"

IP_URL = "https://api.ipify.org"
IP_TIMEOUT_SECONDS = (5, 10)


def egress_ip(get: Callable = requests.get) -> str:
    """Which IP Compass actually saw. Best effort: this is a diagnostic, and
    failing the canary because an unrelated IP service was down would be a
    false alarm about the thing the canary exists to detect.

    Worth recording because it is the one variable the sandbox changes. Every
    scrape from a laptop came from a residential address; these come from a
    datacenter range, and if Compass ever starts treating the two differently
    this line is what says so.
    """
    try:
        response = get(IP_URL, timeout=IP_TIMEOUT_SECONDS)
        return response.text.strip() if response.ok else ""
    except requests.RequestException:
        return ""


def verdict(counts: dict[str, int], errors: dict[str, str], tabs) -> bool:
    """PASS iff every configured tab was fetched and returned something.

    Zero is a failure, not an empty collection. A selector change, an expired
    session and a WAF block all produce a clean fetch of nothing, which is
    precisely the silent failure this exists to catch -- and downstream, an
    empty tab is what makes the delisting cascade consider deleting everything
    it covers.
    """
    if errors:
        return False
    return all(counts.get(tab, 0) > 0 for tab in tabs)


def run_canary(
    env: dict[str, str],
    *,
    launch=launch_authenticated_page,
    fetch=fetch_collection_tabs,
    ip=egress_ip,
    notify_fn=notify,
    state_path: Path = AUTH_STATE_PATH,
) -> tuple[bool, dict]:
    """Returns (passed, report). Everything it talks to is injected, so the
    tests exercise the real decision without a browser."""
    config = load_config(env)
    if not config.collection_url:
        raise ValueError("COMPASS_COLLECTION_URL must be set for the canary")

    cold = False

    def saw_login_form() -> None:
        nonlocal cold
        cold = True

    with launch(config, LOGIN_URL, state_path, on_cold_login=saw_login_form) as page:
        result = fetch(page, config.collection_url, config.collection_tabs)

    passed = verdict(result.counts, result.errors, config.collection_tabs)
    report = {
        "pass": passed,
        "warm_session": not cold,
        "egress_ip": ip(),
        "counts": result.counts,
        "errors": result.errors,
    }

    total = sum(result.counts.values())
    detail = ", ".join(f"{tab}={n}" for tab, n in sorted(result.counts.items()))
    if result.errors:
        detail += ("; " if detail else "") + ", ".join(
            f"{tab} failed" for tab in sorted(result.errors)
        )
    notify_fn(
        env.get("NTFY_TOPIC", ""),
        f"home-search: canary {'PASS' if passed else 'FAIL'}",
        f"{total} listings ({detail or 'nothing fetched'}); "
        f"{'warm' if not cold else 'COLD'} session",
        priority="default" if passed else "high",
        tags=("white_check_mark",) if passed else ("rotating_light",),
    )
    return passed, report


def main() -> int:
    env = load_env()
    try:
        passed, report = run_canary(env)
    except Exception as exc:
        # A crash is a failure like any other, and the message is the most
        # useful part of it. Never the exception's environment, though: this
        # line ends up in a sandbox log.
        report = {"pass": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(report), flush=True)
        notify(
            env.get("NTFY_TOPIC", ""),
            "home-search: canary FAIL",
            f"canary crashed: {type(exc).__name__}",
            priority="high",
            tags=("rotating_light",),
        )
        return 1

    print(json.dumps(report), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
