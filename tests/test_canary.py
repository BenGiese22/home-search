"""What the canary must call a failure.

The canary's only job is to fail loudly a day before the pipeline would fail
quietly, so the tests are almost entirely about the verdict: which conditions
count as broken, and whether a broken one can slip through as a pass.

Nothing here launches a browser or makes a request.
"""

import json
from contextlib import contextmanager

import pytest
import requests

from ops.canary import egress_ip, main, run_canary, verdict
from src.scraper import CollectionFetch

ENV = {
    "COMPASS_EMAIL": "a@b.com",
    "COMPASS_PASSWORD": "pw",
    "COMPASS_COLLECTION_URL": "https://www.compass.com/collection/x/favorites",
    "NTFY_TOPIC": "topic",
}


def fake_launch(result, *, cold=False):
    @contextmanager
    def launch(_config, _url, _state, on_cold_login=None):
        if cold and on_cold_login is not None:
            on_cold_login()
        yield object()

    return launch


def run(result, *, env=None, cold=False, sent=None):
    return run_canary(
        dict(env or ENV),
        launch=fake_launch(result, cold=cold),
        fetch=lambda *_a: result,
        ip=lambda: "1.2.3.4",
        notify_fn=(lambda *a, **k: sent.append((a, k)) if sent is not None else True),
    )


# --- the verdict ----------------------------------------------------------

def test_every_tab_returning_listings_passes():
    assert verdict({"favorites": 3, "matches": 120}, {}, ("favorites", "matches"))


def test_an_empty_tab_is_a_failure_not_an_empty_collection():
    """A selector change, an expired session and a WAF block all look like a
    clean fetch of nothing. That is the silent failure this exists to catch --
    and downstream it is what makes the delisting cascade consider deleting
    every listing the tab covered."""
    assert not verdict({"favorites": 0, "matches": 120}, {}, ("favorites", "matches"))


def test_a_missing_tab_is_a_failure():
    assert not verdict({"matches": 120}, {}, ("favorites", "matches"))


def test_a_tab_error_fails_even_when_the_others_returned_listings():
    assert not verdict(
        {"matches": 120}, {"favorites": "timeout"}, ("favorites", "matches")
    )


# --- the report -----------------------------------------------------------

def test_a_passing_run_reports_what_it_saw():
    fetch = CollectionFetch(counts={"favorites": 2, "matches": 9}, errors={})

    passed, report = run(fetch)

    assert passed
    assert report == {
        "pass": True,
        "warm_session": True,
        "egress_ip": "1.2.3.4",
        "counts": {"favorites": 2, "matches": 9},
        "errors": {},
    }


def test_a_cold_login_is_recorded_but_does_not_fail_the_run():
    """Warm-session-first is the design, so logging in cold every night is
    worth seeing -- but a cold login that succeeds proves Compass is reachable
    and the credentials work, which is what the canary was asked."""
    fetch = CollectionFetch(counts={"favorites": 2, "matches": 9}, errors={})

    passed, report = run(fetch, cold=True)

    assert passed
    assert report["warm_session"] is False


def test_the_report_is_one_json_line(capsys, monkeypatch):
    import ops.canary as canary

    monkeypatch.setattr(canary, "load_env", lambda: dict(ENV))
    monkeypatch.setattr(
        canary, "run_canary", lambda _env: (True, {"pass": True, "counts": {}})
    )

    assert main() == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"pass": True, "counts": {}}
    assert "\n" not in out


# --- exit codes -----------------------------------------------------------

def test_a_failing_canary_exits_nonzero(capsys, monkeypatch):
    """The runner records this exit code in the `done` marker, and the reaper
    turns a non-zero one into a push notification."""
    import ops.canary as canary

    monkeypatch.setattr(canary, "load_env", lambda: dict(ENV))
    monkeypatch.setattr(canary, "run_canary", lambda _env: (False, {"pass": False}))

    assert main() == 1


def test_a_crash_is_a_failure_and_still_notifies(capsys, monkeypatch):
    import ops.canary as canary

    sent = []
    monkeypatch.setattr(canary, "load_env", lambda: dict(ENV))
    monkeypatch.setattr(
        canary, "run_canary", lambda _env: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(canary, "notify", lambda *a, **k: sent.append((a, k)))

    assert main() == 1
    report = json.loads(capsys.readouterr().out.strip())
    assert report["pass"] is False
    assert "RuntimeError" in report["error"]
    assert sent and "FAIL" in sent[0][0][1]


# --- the notification -----------------------------------------------------

def test_a_failure_is_pushed_at_high_priority():
    """The whole point of the canary is that someone finds out. A default
    priority is what a phone silences overnight."""
    sent = []
    run(CollectionFetch(counts={"favorites": 0}, errors={}), sent=sent)

    (_topic, title, message), kwargs = sent[0]
    assert "FAIL" in title
    assert kwargs["priority"] == "high"


def test_a_pass_is_pushed_at_default_priority():
    sent = []
    run(
        CollectionFetch(counts={"favorites": 2, "matches": 9}, errors={}),
        sent=sent,
    )

    (_topic, title, message), kwargs = sent[0]
    assert "PASS" in title
    assert "11 listings" in message
    assert kwargs["priority"] == "default"


def test_a_failed_tab_is_named_in_the_message():
    sent = []
    run(
        CollectionFetch(counts={"matches": 9}, errors={"favorites": "timeout"}),
        sent=sent,
    )

    message = sent[0][0][2]
    assert "favorites failed" in message


def test_no_topic_means_no_push_rather_than_a_crash():
    env = dict(ENV)
    del env["NTFY_TOPIC"]
    sent = []

    passed, _ = run(
        CollectionFetch(counts={"favorites": 2, "matches": 9}, errors={}),
        env=env,
        sent=sent,
    )

    assert passed
    assert sent[0][0][0] == ""


# --- egress ip ------------------------------------------------------------

def test_the_egress_ip_is_recorded():
    class Response:
        ok = True
        text = " 3.4.5.6\n"

    assert egress_ip(get=lambda *a, **k: Response()) == "3.4.5.6"


def test_a_dead_ip_service_never_fails_the_canary():
    """A false alarm about the thing the canary exists to detect is worse
    than a missing diagnostic."""
    def down(*_a, **_k):
        raise requests.RequestException("no route")

    assert egress_ip(get=down) == ""


def test_a_non_ok_ip_response_is_ignored():
    class Response:
        ok = False
        text = "<html>error</html>"

    assert egress_ip(get=lambda *a, **k: Response()) == ""


# --- configuration --------------------------------------------------------

def test_the_canary_needs_a_collection_to_check():
    env = dict(ENV)
    env["COMPASS_COLLECTION_URL"] = ""
    env["LISTING_URLS"] = "https://www.compass.com/listing/1"

    with pytest.raises(ValueError, match="COMPASS_COLLECTION_URL"):
        run(CollectionFetch(), env=env)
