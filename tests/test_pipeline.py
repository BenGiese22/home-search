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
        "scrape", "commutes", "score-photos", "score", "verify"
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
        "score-photos", "score", "verify"
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
