"""main()'s own control flow: exit codes and per-item failure isolation.

Isolated from the rest of score_photos' tests (see test_rescore_all.py's
docstring) because these exercise main() itself rather than a helper it
calls -- every network and filesystem seam main() touches is faked here so
the only thing under test is main()'s own control flow.
"""

import sqlite3

import pytest

import score_photos
from src.turso_db import ensure_schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def test_a_missing_api_key_exits_non_zero_not_silently_successful(conn, monkeypatch, capsys):
    """Before this, main() had no return value and nothing called
    sys.exit(main()), so the script always exited 0 regardless of what
    happened inside -- a missing ANTHROPIC_API_KEY was reported as a
    successful run despite scoring nothing."""
    monkeypatch.setattr(score_photos, "stage_connection", lambda: conn)
    monkeypatch.setattr(score_photos, "load_env", lambda: {})

    assert score_photos.main() == score_photos.EXIT_NO_API_KEY
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
