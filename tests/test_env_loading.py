"""Every entry point resolves configuration the same way.

`publish.py` already merged both sources; the other entry points read
`dotenv_values(".env")` only, so they could not run anywhere that supplies
configuration as process environment variables -- which is every containerised
or hosted environment, and a hard prerequisite for cloud execution.

The merge is not merely theoretical hygiene: `.env` has carried
MAX_PHOTOS_PER_LISTING=0 while publish.py read it from os.environ alone, so
the setting was silently ignored and the cap stayed at 8.
"""
import os
from pathlib import Path

import pytest

from src.config import load_env

ENTRY_POINTS = [
    "scrape.py",
    "score_photos.py",
    "check.py",
    "backfill_photos.py",
    "assess_six_houses.py",
]


def _write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body)
    return path


def test_values_come_from_the_dotenv_file(tmp_path):
    env_path = _write_env(tmp_path, "COMPASS_EMAIL=ben@example.com\n")

    assert load_env(env_path)["COMPASS_EMAIL"] == "ben@example.com"


def test_process_env_takes_precedence_over_the_dotenv_file(tmp_path, monkeypatch):
    env_path = _write_env(tmp_path, "TURSO_DATABASE_URL=libsql://from-dotenv\n")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://from-process")

    assert load_env(env_path)["TURSO_DATABASE_URL"] == "libsql://from-process"


def test_configuration_works_with_no_dotenv_file_at_all(tmp_path, monkeypatch):
    """The acceptance criterion: .env absent, configuration via process env.
    This is exactly the shape a sandbox or container runs in."""
    monkeypatch.setenv("COMPASS_EMAIL", "ben@example.com")
    monkeypatch.setenv("COMPASS_PASSWORD", "hunter2")

    env = load_env(tmp_path / "does-not-exist.env")

    assert env["COMPASS_EMAIL"] == "ben@example.com"
    assert env["COMPASS_PASSWORD"] == "hunter2"


def test_a_missing_dotenv_file_is_not_an_error(tmp_path):
    assert isinstance(load_env(tmp_path / "nope.env"), dict)


def test_behaviour_is_unchanged_when_only_the_dotenv_file_is_set(tmp_path, monkeypatch):
    """The other acceptance criterion: with .env present and no process env
    override, every value is exactly what .env says."""
    monkeypatch.delenv("COMPASS_COLLECTION_URL", raising=False)
    env_path = _write_env(
        tmp_path,
        "COMPASS_EMAIL=a@b.com\nCOMPASS_PASSWORD=pw\n"
        "COMPASS_COLLECTION_URL=https://compass.com/collections/abc\n",
    )

    env = load_env(env_path)

    assert env["COMPASS_EMAIL"] == "a@b.com"
    assert env["COMPASS_PASSWORD"] == "pw"
    assert env["COMPASS_COLLECTION_URL"] == "https://compass.com/collections/abc"


def test_a_declared_but_unset_dotenv_key_does_not_shadow_the_process_env(
    tmp_path, monkeypatch
):
    """A bare `KEY` line parses to None. Merging that in would hand callers a
    None where a real process-env value exists, or where a default should
    apply -- both worse than treating the line as absent."""
    env_path = _write_env(tmp_path, "REVALIDATE_SECRET\n")
    monkeypatch.setenv("REVALIDATE_SECRET", "s3cret")

    assert load_env(env_path)["REVALIDATE_SECRET"] == "s3cret"


def test_a_declared_but_unset_key_is_absent_rather_than_none(tmp_path, monkeypatch):
    monkeypatch.delenv("SHORT_LIST_URL", raising=False)
    env_path = _write_env(tmp_path, "SHORT_LIST_URL\n")

    env = load_env(env_path)

    assert env.get("SHORT_LIST_URL") is None
    assert env.get("SHORT_LIST_URL", "fallback") == "fallback"


def test_the_process_environment_is_still_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_PLATFORM_VAR", "1")

    assert load_env(tmp_path / "absent.env")["SOME_PLATFORM_VAR"] == "1"


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_entry_points_do_not_read_the_dotenv_file_directly(entry_point):
    """One merged lookup, one place. A direct dotenv_values(".env") call is
    the regression this issue exists to remove."""
    source = Path(entry_point).read_text()
    assert "dotenv_values" not in source, (
        f"{entry_point} still reads .env directly instead of using load_env()"
    )


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
def test_entry_points_use_the_shared_loader(entry_point):
    source = Path(entry_point).read_text()
    assert "load_env" in source, f"{entry_point} does not use load_env()"


def test_the_photo_cap_honours_the_dotenv_file():
    """The latent bug this unification fixed: MAX_PHOTOS_PER_LISTING was read
    from os.environ alone, so the value sitting in .env was silently ignored
    and the cap stayed at its default. The setting moved to scrape.py with
    the upload itself when publish.py was deleted."""
    source = Path("scrape.py").read_text()
    assert 'os.environ.get("MAX_PHOTOS_PER_LISTING"' not in source
    assert "load_env()" in source
