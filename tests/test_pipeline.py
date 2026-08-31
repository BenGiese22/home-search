import json
import time
from pathlib import Path

import pytest

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


def test_stages_run_in_dependency_order():
    plan = build_plan()
    assert [s.name for s in plan] == [
        "scrape", "commutes", "score-photos", "score", "publish"
    ]


def test_only_runs_a_single_stage():
    assert [s.name for s in build_plan(only="publish")] == ["publish"]


def test_from_resumes_at_a_stage_and_continues():
    assert [s.name for s in build_plan(start_from="score")] == ["score", "publish"]


def test_skip_publish_drops_the_last_stage():
    assert "publish" not in [s.name for s in build_plan(skip_publish=True)]


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
    """--only publish is not a full refresh and must not reset the clock."""
    marker = tmp_path / "last.json"
    run_pipeline(build_plan(only="publish"), runner=Runner(), marker=marker)
    assert is_fresh(marker, max_age_hours=6) is False
