import json
import time
from pathlib import Path

import pytest

import pipeline

# Captured before the autouse fixture replaces it, for the two tests that
# exercise the real function rather than stubbing it out.
_REAL_DEFAULT_REVALIDATE = pipeline._default_revalidate

from pipeline import (
    STAGE_NAMES,
    Skipped,
    build_plan,
    is_fresh,
    record_success,
    run_pipeline,
)
from src.exit_codes import EXIT_PARTIAL


class Runner:
    """Records argv instead of spawning processes."""

    def __init__(self, exit_codes=None):
        self.calls = []
        self.exit_codes = exit_codes or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return self.exit_codes.get(argv[1], 0)

    @property
    def scripts(self):
        return [argv[1] for argv in self.calls]


class LoggingRunner(Runner):
    """Like Runner, but also writes into the shared log file the way a real
    subprocess would -- so a test can control what a "failed stage's own
    captured output" actually contains."""

    def __init__(self, exit_codes=None, outputs=None):
        super().__init__(exit_codes)
        self.outputs = outputs or {}

    def __call__(self, argv, log_handle=None, **kwargs):
        script = argv[1]
        if log_handle is not None and script in self.outputs:
            log_handle.write(self.outputs[script])
            log_handle.flush()
        return super().__call__(argv, log_handle=log_handle, **kwargs)


@pytest.fixture(autouse=True)
def never_revalidate_for_real(monkeypatch):
    """run_pipeline ends with a POST to the live viewer. No test may make it.

    Caught the hard way: these tests called run_pipeline() without injecting
    a revalidate, which fired real POSTs at the deployed site.
    """
    calls = []
    monkeypatch.setattr(
        pipeline, "_default_revalidate", lambda: calls.append(True) or True
    )
    return calls


def test_stages_run_in_dependency_order():
    plan = build_plan()
    # verify runs last and can fail the run: every stage before it can
    # succeed while producing something wrong.
    assert [s.name for s in plan] == [
        "scrape", "commutes", "score-photos", "score", "notify", "verify"
    ]


def test_publish_is_no_longer_a_stage():
    """The stages write Turso directly, so there is nothing to mirror. What
    publish.py did that still matters -- the revalidate POST -- moved to the
    end of run_pipeline()."""
    assert "publish" not in [s.name for s in build_plan()]
    with pytest.raises(ValueError, match="publish"):
        build_plan(only="publish")


def test_only_runs_a_single_stage():
    assert [s.name for s in build_plan(only="score")] == ["score"]


def test_from_resumes_at_a_stage_and_continues():
    assert [s.name for s in build_plan(start_from="score-photos")] == [
        "score-photos", "score", "notify", "verify"
    ]


def test_unknown_stage_name_is_rejected():
    with pytest.raises(ValueError, match="nonsense"):
        build_plan(only="nonsense")


def test_scrape_flags_are_forwarded_only_to_the_scrape_stage():
    runner = Runner()
    run_pipeline(build_plan(), runner=runner, scrape_flags=["--skip-photos", "--limit=3"])
    scrape_argv = next(a for a in runner.calls if a[1] == "scrape.py")
    assert "--skip-photos" in scrape_argv and "--limit=3" in scrape_argv
    for argv in runner.calls:
        if argv[1] != "scrape.py":
            assert "--skip-photos" not in argv


def test_dry_run_executes_nothing():
    runner = Runner()
    run_pipeline(build_plan(), runner=runner, dry_run=True)
    assert runner.calls == []


def test_a_failing_stage_stops_the_pipeline():
    """Later stages read what earlier stages write, so continuing past a
    failure would publish results computed from half-updated data."""
    runner = Runner(exit_codes={"compute_commutes.py": 2})
    code = run_pipeline(build_plan(), runner=runner)
    assert code == 2
    assert runner.scripts == ["scrape.py", "compute_commutes.py"]


def test_success_returns_zero_and_runs_everything():
    runner = Runner()
    assert run_pipeline(build_plan(), runner=runner) == 0
    assert len(runner.calls) == len(STAGE_NAMES)


# --- freshness guard: the intermittent-uptime design -----------------------

def test_is_fresh_false_when_never_run(tmp_path: Path):
    assert is_fresh(tmp_path / "none.json", max_age_hours=6) is False


def test_is_fresh_true_within_the_window(tmp_path: Path):
    marker = tmp_path / "last.json"
    record_success(marker)
    assert is_fresh(marker, max_age_hours=6) is True


def test_is_fresh_false_once_the_window_has_passed(tmp_path: Path):
    marker = tmp_path / "last.json"
    marker.write_text(json.dumps({"finished_at": time.time() - 7 * 3600}))
    assert is_fresh(marker, max_age_hours=6) is False


def test_is_fresh_false_on_a_corrupt_marker(tmp_path: Path):
    """A damaged marker must mean 'run', not 'skip forever'."""
    marker = tmp_path / "last.json"
    marker.write_text("{not json")
    assert is_fresh(marker, max_age_hours=6) is False


def test_max_age_zero_always_runs(tmp_path: Path):
    marker = tmp_path / "last.json"
    record_success(marker)
    assert is_fresh(marker, max_age_hours=0) is False


def test_run_pipeline_skips_when_fresh(tmp_path: Path):
    marker = tmp_path / "last.json"
    record_success(marker)
    runner = Runner()
    with pytest.raises(Skipped):
        run_pipeline(build_plan(), runner=runner, marker=marker, max_age_hours=6)
    assert runner.calls == []


def test_run_pipeline_records_success_for_the_next_freshness_check(tmp_path: Path):
    marker = tmp_path / "last.json"
    run_pipeline(build_plan(), runner=Runner(), marker=marker)
    assert is_fresh(marker, max_age_hours=6) is True


def test_a_failed_run_does_not_count_as_fresh(tmp_path: Path):
    """Otherwise one failure would suppress retries for the whole window."""
    marker = tmp_path / "last.json"
    run_pipeline(build_plan(), runner=Runner(exit_codes={"score.py": 1}), marker=marker)
    assert is_fresh(marker, max_age_hours=6) is False


def test_partial_run_does_not_record_success(tmp_path: Path):
    """--only score is not a full refresh and must not reset the clock."""
    marker = tmp_path / "last.json"
    run_pipeline(build_plan(only="score"), runner=Runner(), marker=marker)
    assert is_fresh(marker, max_age_hours=6) is False


# --- the revalidate ending (issue #22) -----------------------------------

def test_a_successful_run_ends_with_the_revalidate_post(never_revalidate_for_real):
    run_pipeline(build_plan(), runner=Runner())

    assert never_revalidate_for_real == [True], "a successful run must revalidate"


def test_a_failed_stage_does_not_revalidate(never_revalidate_for_real):
    """The viewer must not be told to re-read a half-updated database."""
    run_pipeline(build_plan(), runner=Runner(exit_codes={"score.py": 1}))

    assert never_revalidate_for_real == []


def test_a_dry_run_does_not_revalidate(never_revalidate_for_real):
    run_pipeline(build_plan(), runner=Runner(), dry_run=True)

    assert never_revalidate_for_real == []


def test_a_partial_run_still_revalidates(never_revalidate_for_real):
    """Every stage writes straight to the database the viewer reads, so even
    --only=score changes what it should be serving."""
    run_pipeline(build_plan(only="score"), runner=Runner())

    assert never_revalidate_for_real == [True]


def test_a_failing_revalidate_does_not_fail_the_run(monkeypatch):
    """Every write already landed; the cache expires on its own. Failing the
    run here would report failure for a run that succeeded."""
    monkeypatch.setattr(pipeline, "_default_revalidate", lambda: False)

    assert run_pipeline(build_plan(), runner=Runner()) == 0


def test_revalidate_is_skipped_when_it_is_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "load_env", lambda: {})

    assert _REAL_DEFAULT_REVALIDATE() is False
    out = capsys.readouterr().out
    assert "SHORT_LIST_URL" in out and "REVALIDATE_SECRET" in out


def test_revalidate_is_called_with_the_configured_url_and_secret(monkeypatch):
    captured = {}
    monkeypatch.setattr(pipeline, "load_env", lambda: {
        "SHORT_LIST_URL": "https://short-list.example",
        "REVALIDATE_SECRET": "s3cret",
    })
    monkeypatch.setattr(
        pipeline, "revalidate",
        lambda url, secret: captured.update(url=url, secret=secret) or True,
    )

    assert _REAL_DEFAULT_REVALIDATE() is True
    assert captured == {"url": "https://short-list.example", "secret": "s3cret"}


# --- stage-specific flag forwarding -------------------------------------
#
# scrape_flags exists because scrape.py has options a caller wants to reach.
# Every other stage had none, so there was no general mechanism -- and then
# compute_commutes.py grew --force, which is the only way to re-measure the
# corpus after changing how a commute is computed. Stage.forwards is that
# mechanism: a pipeline-level trigger maps to one stage's flag.


def test_a_forwarded_trigger_reaches_only_the_stage_that_declares_it():
    runner = Runner()
    run_pipeline(build_plan(), runner=runner, forwarded=["--force-commutes"])
    commutes_argv = next(a for a in runner.calls if a[1] == "compute_commutes.py")
    assert "--force" in commutes_argv
    for argv in runner.calls:
        if argv[1] != "compute_commutes.py":
            assert "--force" not in argv


def test_a_trigger_no_stage_declares_is_ignored():
    runner = Runner()
    run_pipeline(build_plan(), runner=runner, forwarded=["--force-nothing"])
    for argv in runner.calls:
        assert argv[2:] == []


def test_nothing_is_forwarded_when_the_trigger_is_absent():
    runner = Runner()
    run_pipeline(build_plan(), runner=runner)
    for argv in runner.calls:
        assert argv[2:] == []


def test_dry_run_shows_the_forwarded_flag_and_still_runs_nothing(capsys):
    runner = Runner()
    run_pipeline(build_plan(), runner=runner, dry_run=True, forwarded=["--force-commutes"])
    out = capsys.readouterr().out
    assert "compute_commutes.py --force" in out
    assert runner.calls == []


def test_a_single_stage_run_still_forwards():
    """--only=commutes --force-commutes is how a re-measure would be asked
    for by hand; the trigger must survive the narrowed plan."""
    runner = Runner()
    run_pipeline(build_plan(only="commutes"), runner=runner, forwarded=["--force-commutes"])
    assert runner.calls[0][2:] == ["--force"]


def test_collect_forwarded_reads_the_triggers_off_the_command_line():
    assert pipeline._collect_forwarded(["--force-commutes"]) == ["--force-commutes"]
    assert pipeline._collect_forwarded(["--max-age=5h"]) == []


def test_force_commutes_is_not_mistaken_for_the_scrape_force_flag():
    """SCRAPE_FLAGS contains --force. A prefix match would send scrape.py a
    --force it never asked for, re-fetching every photo in the corpus."""
    assert "--force-commutes" not in pipeline.SCRAPE_FLAGS
    argv = ["--force-commutes"]
    scrape_flags = [a for a in argv if a in pipeline.SCRAPE_FLAGS]
    scrape_flags += [a for a in argv if a.startswith(pipeline.SCRAPE_FLAG_PREFIXES)]
    assert scrape_flags == []


def test_the_digest_runs_after_scoring_and_before_verify():
    """After score, because the digest reports where a new listing ranks and
    nothing knows that until scoring has run. Before verify, because verify
    is allowed to fail the run -- and a new listing is still worth reporting
    on a run where some unrelated invariant broke."""
    names = [s.name for s in build_plan()]
    assert names.index("score") < names.index("notify") < names.index("verify")


def test_a_failure_is_sent_by_email_and_push(monkeypatch):
    """Both attempted, neither required. The ntfy path shipped configured and
    looked healthy for the life of the project while publishing to a topic
    nobody had ever subscribed to -- every failure alert went into a void. A
    channel that cannot be observed to be working is not a channel."""
    sent = {}
    monkeypatch.setattr(pipeline, "load_env", lambda: {
        "RESEND_API_KEY": "re_key", "DIGEST_EMAIL_TO": "ben@example.com",
        "NTFY_TOPIC": "topic",
    })
    def record(channel):
        def fn(*args, **kwargs):
            sent[channel] = (args, kwargs)
            return True
        return fn

    monkeypatch.setattr(pipeline, "send_email", record("email"))
    monkeypatch.setattr(pipeline, "notify", record("ntfy"))

    assert pipeline._default_notify("run failed", "verify: 1 violation") is True
    assert "email" in sent and "ntfy" in sent


def test_one_dead_channel_does_not_silence_the_other(monkeypatch):
    monkeypatch.setattr(pipeline, "load_env", lambda: {"RESEND_API_KEY": "re_key"})
    monkeypatch.setattr(pipeline, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "notify", lambda *a, **k: True)
    assert pipeline._default_notify("t", "m") is True

    monkeypatch.setattr(pipeline, "send_email", lambda *a, **k: True)
    monkeypatch.setattr(pipeline, "notify", lambda *a, **k: False)
    assert pipeline._default_notify("t", "m") is True


def test_no_channel_configured_reports_nothing_delivered(monkeypatch):
    monkeypatch.setattr(pipeline, "load_env", lambda: {})
    monkeypatch.setattr(pipeline, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "notify", lambda *a, **k: False)
    assert pipeline._default_notify("t", "m") is False


# --- the failure alert carries a reason and a verdict --------------------
#
# A generic "score failed, exit 1" told a human nothing about whether to get
# up and look at it. The log file every stage's subprocess already writes
# into is the mechanism: no new IPC, just reading back the slice a failed
# stage wrote between the offset recorded before it ran and EOF.


def _run_with_alert(tmp_path, runner):
    """run_pipeline against a real log file, with the alert captured instead
    of actually sent."""
    alerts = []
    log_path = tmp_path / "pipeline.log"
    with log_path.open("w+") as log_handle:
        run_pipeline(
            build_plan(),
            runner=runner,
            log_handle=log_handle,
            notify_fn=lambda title, message: alerts.append((title, message)) or True,
        )
    return alerts


def test_a_failed_stages_captured_output_reaches_the_alert(tmp_path: Path):
    runner = LoggingRunner(
        exit_codes={"score.py": 1},
        outputs={"score.py": "scoring L1\nValueError: bad amenity value\n"},
    )
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert "ValueError: bad amenity value" in message


def test_a_transient_looking_failure_says_no_action_needed(tmp_path: Path):
    runner = LoggingRunner(
        exit_codes={"score.py": 1},
        outputs={"score.py": "requests.exceptions.ConnectionError: timed out\n"},
    )
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert "no action needed" in message.lower()


def test_a_non_transient_failure_says_action_needed(tmp_path: Path):
    runner = LoggingRunner(
        exit_codes={"score.py": 1},
        outputs={"score.py": "ValueError: bad amenity value\n"},
    )
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert "action needed" in message.lower()
    assert "no action needed" not in message.lower()


def test_no_captured_output_falls_back_to_action_needed(tmp_path: Path):
    """A stage that fails without writing anything (or a log seam that
    captured nothing) must not be misread as a transient failure by
    default -- the safer of the two guesses is "go look at it"."""
    runner = LoggingRunner(exit_codes={"score.py": 1})
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert "action needed" in message.lower()


def test_existing_failure_details_survive_alongside_the_reason(tmp_path: Path):
    """The reason and verdict are additions, not replacements -- stage name,
    exit code, elapsed time, and the later-stages-skipped line must still
    all be there."""
    runner = LoggingRunner(
        exit_codes={"score.py": 3},
        outputs={"score.py": "ValueError: bad amenity value\n"},
    )
    title, message = _run_with_alert(tmp_path, runner)[0]
    assert "score" in title
    assert "score" in message and "exited 3" in message
    assert "Later stages were skipped" in message


def test_no_log_handle_falls_back_to_the_generic_message():
    """--dry-run and any other log_handle=None caller must not crash trying
    to read a reason that was never captured."""
    alerts = []
    runner = Runner(exit_codes={"score.py": 1})
    run_pipeline(
        build_plan(),
        runner=runner,
        notify_fn=lambda title, message: alerts.append((title, message)) or True,
    )
    _, message = alerts[0]
    assert "exited 1" in message
    assert "Action needed" in message


def test_the_captured_reason_is_only_this_stages_own_output(tmp_path: Path):
    """The log file is shared across every stage in the run. An earlier
    stage's chatter must not bleed into a later stage's alert."""
    runner = LoggingRunner(
        exit_codes={"score.py": 1},
        outputs={
            "scrape.py": "scrape: 12 listings fetched\n",
            "compute_commutes.py": "commutes: 12/12 routed\n",
            "score.py": "ValueError: bad amenity value\n",
        },
    )
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert "ValueError: bad amenity value" in message
    assert "12 listings fetched" not in message
    assert "12/12 routed" not in message


def test_a_long_captured_reason_is_truncated_for_the_email(tmp_path: Path):
    huge = "\n".join(f"line {n}" for n in range(500))
    runner = LoggingRunner(exit_codes={"score.py": 1}, outputs={"score.py": huge})
    _, message = _run_with_alert(tmp_path, runner)[0]
    assert len(message) < len(huge)
    assert "line 499" in message
    assert "line 0" not in message


def test_a_real_stages_traceback_is_the_end_of_its_log_tail(tmp_path: Path, monkeypatch):
    """A real subprocess, not a fake runner, because the bug lived in the
    process boundary. With stdout redirected to a file, a child Python
    block-buffers it. A stage that catches its error, reports it on stderr,
    and exits nonzero (compute_commutes.py's shape) wrote that report first,
    and the buffered progress lines landed after it at exit -- so the tail
    ended in chatter and the KeyError was nowhere in the alert.

    An uncaught exception alone does not show it: CPython flushes stdout
    before printing that traceback. The report-then-exit path is the one
    that needs the child run unbuffered.
    """
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    script = tmp_path / "stage.py"
    script.write_text(
        "import sys, traceback\n"
        "for n in range(100):\n"
        "    print(f'progress {n}')\n"
        "try:\n"
        "    {}['listing_id']\n"
        "except KeyError:\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
    )
    log_path = tmp_path / "pipeline.log"
    with log_path.open("w+") as log_handle:
        code = pipeline._default_runner(
            [pipeline.sys.executable, str(script)], log_handle=log_handle
        )
        tail = pipeline._stage_log_tail(log_handle, 0)
    assert code == 1
    assert tail.splitlines()[-1] == "KeyError: 'listing_id'"


# --- the verdict reads the final exception, not the whole tail -------------
#
# A marker anywhere in the tail used to be enough. The tail is a stage's own
# chatter too, so one earlier per-listing ConnectionError log line turned a
# later real crash into "no action needed".

_TRACEBACK_KEYERROR = (
    "Traceback (most recent call last):\n"
    '  File "score.py", line 40, in main\n'
    "    row['listing_id']\n"
    "KeyError: 'listing_id'\n"
)


def _verdict(tmp_path, output):
    runner = LoggingRunner(exit_codes={"score.py": 1}, outputs={"score.py": output})
    _, message = _run_with_alert(tmp_path, runner)[0]
    return message.lower()


def test_an_earlier_logged_connection_error_does_not_excuse_a_later_crash(tmp_path: Path):
    output = (
        "listing L7: requests.exceptions.ConnectionError: reset by peer, skipping\n"
        "scored 41 listings\n" + _TRACEBACK_KEYERROR
    )
    message = _verdict(tmp_path, output)
    assert "no action needed" not in message
    assert "action needed" in message


def test_a_final_transient_exception_still_says_no_action_needed(tmp_path: Path):
    output = (
        "scored 41 listings\n"
        "Traceback (most recent call last):\n"
        '  File "score.py", line 40, in main\n'
        "requests.exceptions.ConnectionError: HTTPSConnectionPool: Max retries exceeded\n"
    )
    assert "no action needed" in _verdict(tmp_path, output)


def test_a_playwright_timeout_is_not_transient(tmp_path: Path):
    """A selector timeout after a Compass redesign fails every single run.
    It is spelled TimeoutError, but nothing about it retries its way out."""
    output = (
        "Traceback (most recent call last):\n"
        '  File "scrape.py", line 90, in main\n'
        "playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.\n"
    )
    message = _verdict(tmp_path, output)
    assert "no action needed" not in message
    assert "action needed" in message


def test_a_deliberate_exit_code_with_no_traceback_says_action_needed(tmp_path: Path):
    """A stage that chose its own nonzero exit left no exception to read. A
    network word in its last log line is not evidence the failure was one."""
    output = "listing L3: ConnectionError from mapbox\ncommutes: 0/5 routed\n"
    message = _verdict(tmp_path, output)
    assert "no action needed" not in message
    assert "action needed" in message


def test_final_exception_line_is_the_last_one_in_the_tail():
    tail = "requests.exceptions.ConnectionError: earlier\n" + _TRACEBACK_KEYERROR + "\n"
    assert pipeline._final_exception_line(tail) == "KeyError: 'listing_id'"
    # A log prefix is not an exception type.
    assert pipeline._final_exception_line("compute_commutes: 0/5 routed\n") is None


# --- EXIT_PARTIAL: some items failed, the stage's work still landed --------
#
# Stopping the run over it would throw away every item that succeeded and
# gain nothing -- the failed ones are retried next run either way.


def _run_partial(tmp_path, runner, marker=None, partial_state=None, kwargs_out=None):
    alerts = []

    def notify_fn(title, message, **kwargs):
        if kwargs_out is not None:
            kwargs_out.append(kwargs)
        alerts.append((title, message))
        return True

    log_path = tmp_path / "pipeline.log"
    with log_path.open("w+") as log_handle:
        code = run_pipeline(
            build_plan(),
            runner=runner,
            log_handle=log_handle,
            marker=marker,
            notify_fn=notify_fn,
            partial_state=partial_state,
        )
    return code, alerts


def test_a_partial_stage_does_not_stop_later_stages(tmp_path: Path):
    runner = LoggingRunner(exit_codes={"compute_commutes.py": EXIT_PARTIAL})
    code, _ = _run_partial(tmp_path, runner)
    assert code == 0
    assert len(runner.calls) == len(STAGE_NAMES)


def test_a_partial_stage_alerts_once_with_its_log_tail(tmp_path: Path, capsys):
    runner = LoggingRunner(
        exit_codes={"compute_commutes.py": EXIT_PARTIAL},
        outputs={
            "scrape.py": "scrape: 12 listings fetched\n",
            "compute_commutes.py": "commutes: 11/12 routed; L9 would not geocode\n",
        },
    )
    _, alerts = _run_partial(tmp_path, runner)
    assert len(alerts) == 1
    title, message = alerts[0]
    assert title == "home-search: commutes partially failed"
    assert "L9 would not geocode" in message
    assert "12 listings fetched" not in message
    assert "later stages still ran" in message.lower()
    assert "retried" in message.lower()
    assert f"[commutes] ok with item failures (exit {EXIT_PARTIAL})" in capsys.readouterr().out


def test_a_partial_run_still_revalidates_but_is_not_fresh(
    tmp_path: Path, never_revalidate_for_real
):
    """The writes landed, so the viewer's cache is stale either way. But the
    alert promises the failed items are retried next run, and a fresh
    marker would let --max-age skip exactly that run."""
    marker = tmp_path / "last.json"
    runner = LoggingRunner(exit_codes={"score_photos.py": EXIT_PARTIAL})
    _run_partial(tmp_path, runner, marker=marker)
    assert is_fresh(marker, max_age_hours=6) is False
    assert never_revalidate_for_real == [True]


def test_a_real_failure_after_a_partial_one_still_stops_the_run(tmp_path: Path):
    runner = LoggingRunner(
        exit_codes={"compute_commutes.py": EXIT_PARTIAL, "score.py": 1},
    )
    code, alerts = _run_partial(tmp_path, runner)
    assert code == 1
    assert runner.scripts == [
        "scrape.py", "compute_commutes.py", "score_photos.py", "score.py"
    ]
    assert [title for title, _ in alerts] == [
        "home-search: commutes partially failed",
        "home-search: score failed",
    ]


# --- a partial alert is news only when the failed set changes --------------
#
# A permanently broken listing makes its stage partial on every run. An
# alert for each is four a day forever, and the real ones drown in them.


def _partial_score(ids):
    return LoggingRunner(
        exit_codes={"score.py": EXIT_PARTIAL},
        outputs={"score.py": f"L1: failed to score\nPARTIAL: score: {ids}\n"},
    )


def test_the_same_failed_set_twice_alerts_once(tmp_path: Path, capsys):
    state = tmp_path / ".run" / "partial-alerts.json"
    _, first = _run_partial(tmp_path, _partial_score("L1,L2"), partial_state=state)
    _, second = _run_partial(tmp_path, _partial_score("L1,L2"), partial_state=state)
    assert len(first) == 1
    assert second == []
    assert "same items as last time" in capsys.readouterr().out


def test_a_changed_failed_set_alerts_again(tmp_path: Path):
    state = tmp_path / "partial-alerts.json"
    _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    _, alerts = _run_partial(tmp_path, _partial_score("L1,L2"), partial_state=state)
    assert [t for t, _ in alerts] == ["home-search: score partially failed"]


def test_a_clean_run_of_the_stage_clears_it(tmp_path: Path):
    """Fixed, then broken the same way again, is a new failure."""
    state = tmp_path / "partial-alerts.json"
    _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    _run_partial(tmp_path, LoggingRunner(), partial_state=state)
    assert "score" not in json.loads(state.read_text())
    _, alerts = _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    assert len(alerts) == 1


def test_one_stages_set_does_not_suppress_anothers(tmp_path: Path):
    state = tmp_path / "partial-alerts.json"
    _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    runner = LoggingRunner(
        exit_codes={"score_photos.py": EXIT_PARTIAL},
        outputs={"score_photos.py": "PARTIAL: score-photos: L1\n"},
    )
    _, alerts = _run_partial(tmp_path, runner, partial_state=state)
    assert [t for t, _ in alerts] == ["home-search: score-photos partially failed"]


def test_a_partial_stage_with_no_partial_line_always_alerts(tmp_path: Path):
    """No ids means nothing to compare, and a missed alert is the worse
    mistake."""
    state = tmp_path / "partial-alerts.json"
    runner = LoggingRunner(exit_codes={"compute_commutes.py": EXIT_PARTIAL})
    _, first = _run_partial(tmp_path, runner, partial_state=state)
    _, second = _run_partial(tmp_path, runner, partial_state=state)
    assert len(first) == len(second) == 1


def test_a_corrupt_state_file_alerts_rather_than_suppressing(tmp_path: Path):
    state = tmp_path / "partial-alerts.json"
    state.write_text("{not json")
    _, alerts = _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    assert len(alerts) == 1
    assert json.loads(state.read_text()) == {"score": ["L1"]}


def test_a_suppressed_partial_run_is_still_not_fresh(tmp_path: Path):
    state = tmp_path / "partial-alerts.json"
    marker = tmp_path / "last.json"
    _run_partial(tmp_path, _partial_score("L1"), partial_state=state)
    _run_partial(tmp_path, _partial_score("L1"), marker=marker, partial_state=state)
    assert is_fresh(marker, max_age_hours=6) is False


def test_a_partial_alert_is_normal_priority_and_tagged_apart(tmp_path: Path):
    """A failure stops the run and gets high priority. Items failing in a
    run that finished can wait for morning."""
    kwargs = []
    runner = LoggingRunner(exit_codes={"compute_commutes.py": EXIT_PARTIAL, "score.py": 1})
    _run_partial(tmp_path, runner, kwargs_out=kwargs)
    partial_kwargs, failure_kwargs = kwargs
    assert partial_kwargs["priority"] == "default"
    assert partial_kwargs["tags"] != failure_kwargs.get("tags", pipeline.FAILURE_TAGS)
    assert failure_kwargs.get("priority", "high") == "high"


def test_default_notify_passes_the_priority_and_tags_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(pipeline, "load_env", lambda: {"NTFY_TOPIC": "t"})
    monkeypatch.setattr(pipeline, "send_email", lambda *a, **k: False)
    monkeypatch.setattr(
        pipeline, "notify",
        lambda topic, title, message, **kw: seen.update(kw) or True,
    )
    pipeline._default_notify("t", "m", priority="default", tags=("warning",))
    assert seen == {"priority": "default", "tags": ("warning",)}


def test_a_long_id_list_still_parses_past_the_alert_cap(tmp_path: Path):
    """The alert's tail is capped by characters, which would cut the
    PARTIAL: prefix off a long line and make every run look new."""
    ids = ",".join(f"L{n:05d}" for n in range(1000))
    state = tmp_path / "partial-alerts.json"
    _run_partial(tmp_path, _partial_score(ids), partial_state=state)
    _, alerts = _run_partial(tmp_path, _partial_score(ids), partial_state=state)
    assert alerts == []
