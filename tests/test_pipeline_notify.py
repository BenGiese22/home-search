"""What the pipeline wakes someone up for.

Notification is the only way a failure in an unattended run reaches a person:
the sandbox has no terminal, and the laptop's runs happen while nobody is
watching. So the question each test asks is whether a condition that needs a
human actually produces a push -- and, just as importantly, whether a
condition that does not stays silent.
"""

import pytest

import pipeline
from pipeline import Stage, run_pipeline

STAGES = (Stage("scrape", "scrape.py"), Stage("score", "score.py"))


@pytest.fixture
def pushes():
    return []


def collect(pushes):
    return lambda title, message: pushes.append((title, message))


def run(pushes, *, codes=(0, 0), renew=None, stages=STAGES):
    sent = iter(codes)

    return run_pipeline(
        list(stages),
        runner=lambda argv, log_handle=None: next(sent),
        revalidate_fn=lambda: True,
        renew_lease=renew,
        notify_fn=collect(pushes),
    )


# --- failures -------------------------------------------------------------

def test_a_failed_stage_is_pushed(pushes):
    assert run(pushes, codes=(3,)) == 3

    assert len(pushes) == 1
    title, message = pushes[0]
    assert "scrape" in title
    assert "exited 3" in message


def test_the_push_names_the_stage_not_just_the_run(pushes):
    """The sandbox reaper also pushes on a non-zero run, but it only knows
    that the run failed. Which stage is the whole diagnostic."""
    run(pushes, codes=(0, 1))

    title, _ = pushes[0]
    assert "score" in title
    assert "scrape" not in title


def test_the_push_says_which_home_it_came_from(pushes, monkeypatch):
    """Two homes run this now. A failure that does not say where it happened
    sends an operator to the wrong machine."""
    monkeypatch.setenv("HOME_SEARCH_HOME", "sandbox")

    run(pushes, codes=(1,))

    assert "sandbox" in pushes[0][1]


# --- silence --------------------------------------------------------------

def test_a_successful_run_says_nothing(pushes):
    """A nightly success that announces itself is a notification people learn
    to swipe away, and by the time one matters they no longer read it. The
    canary is what proves the pipeline is alive."""
    assert run(pushes) == 0

    assert pushes == []


def test_a_skipped_run_says_nothing(pushes, tmp_path):
    """A trigger landing on fresh data is the freshness guard working."""
    import json
    import time

    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"finished_at": time.time()}))

    with pytest.raises(pipeline.Skipped):
        run_pipeline(
            list(STAGES),
            runner=lambda *a, **k: 0,
            marker=marker,
            max_age_hours=6.0,
            notify_fn=collect(pushes),
        )

    assert pushes == []


def test_a_failed_revalidate_says_nothing(pushes):
    """The writes all landed and the cache expires on its own. Waking someone
    for a self-healing condition trains them to ignore the ones that are not."""
    run_pipeline(
        list(STAGES),
        runner=lambda *a, **k: 0,
        revalidate_fn=lambda: False,
        notify_fn=collect(pushes),
    )

    assert pushes == []


# --- the lease ------------------------------------------------------------

def test_losing_the_lease_mid_run_is_pushed(pushes):
    """The one condition here that can cost real money: two homes writing the
    same database means a concurrent scrape re-downloading photos Compass is
    already rate-limiting us on. Unlike a failed stage it leaves no non-zero
    exit code behind for anything else to notice."""
    assert run(pushes, codes=(0, 0), renew=lambda: False) == 0

    assert len(pushes) == 2  # renewal is attempted at every stage boundary
    assert "lease" in pushes[0][0]


def test_holding_the_lease_says_nothing(pushes):
    run(pushes, renew=lambda: True)

    assert pushes == []


# --- delivery -------------------------------------------------------------

def test_a_broken_notifier_never_swallows_the_failure_it_was_reporting(capsys):
    """The reachable version of this is a malformed .env: the notify call
    goes through load_env(), so it can raise on the one run that most needed
    to report something. The exit code that says what broke must survive."""
    def explode(_title, _message):
        raise RuntimeError("ntfy down")

    code = run_pipeline(
        list(STAGES),
        runner=lambda *a, **k: 7,
        revalidate_fn=lambda: True,
        notify_fn=explode,
    )

    assert code == 7
    assert "notification failed" in capsys.readouterr().out


def test_failures_are_pushed_at_high_priority(monkeypatch):
    """Default priority is what a phone silences overnight, which is when
    these runs happen."""
    seen = {}
    monkeypatch.setattr(pipeline, "load_env", lambda: {"NTFY_TOPIC": "t"})
    monkeypatch.setattr(
        pipeline, "notify",
        lambda topic, title, message, **kw: seen.update(kw) or True,
    )

    pipeline._default_notify("title", "message")

    assert seen["priority"] == "high"


def test_no_topic_is_a_silent_no_op(monkeypatch):
    """Exactly the behaviour of every run before notifications existed."""
    monkeypatch.setattr(pipeline, "load_env", lambda: {})

    assert pipeline._default_notify("title", "message") is False
