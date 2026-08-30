import subprocess
import sqlite3
from pathlib import Path

import pytest

from src.turso_sync import ensure_schema
from src.blob_upload import already_uploaded, upload_photo


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_already_uploaded_false_when_no_row_exists():
    conn = _connect()

    assert already_uploaded(conn, "abc", 1) is False


def test_already_uploaded_true_after_a_row_exists():
    conn = _connect()
    conn.execute(
        "INSERT INTO hosted_photos (listing_id, position, blob_url) "
        "VALUES ('abc', 1, 'https://example.public.blob.vercel-storage.com/abc/01.jpg')"
    )
    conn.commit()

    assert already_uploaded(conn, "abc", 1) is True


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_upload_photo_returns_the_url_from_stdout():
    calls = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return _FakeCompletedProcess(
            stdout="https://example.public.blob.vercel-storage.com/abc/01.jpg\n"
        )

    url = upload_photo(
        Path("data/photos/abc/01.jpg"), "abc", 1, rw_token="rwtoken123", run=fake_run,
    )

    assert url == "https://example.public.blob.vercel-storage.com/abc/01.jpg"
    assert calls[0][:3] == ["vercel", "blob", "put"]
    assert "--pathname" in calls[0]
    assert calls[0][calls[0].index("--pathname") + 1] == "photos/abc/01.jpg"
    assert "--rw-token" in calls[0]
    assert calls[0][calls[0].index("--rw-token") + 1] == "rwtoken123"
    assert "--allow-overwrite" in calls[0]
    assert "--access" in calls[0]
    assert calls[0][calls[0].index("--access") + 1] == "public"


def test_upload_photo_raises_on_nonzero_exit():
    def fake_run(cmd, capture_output, text, check):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr="401 unauthorized"
        )

    with pytest.raises(RuntimeError):
        upload_photo(Path("data/photos/abc/01.jpg"), "abc", 1, rw_token="bad", run=fake_run)


class _Result:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


def test_url_is_read_from_stderr_because_that_is_where_the_cli_prints_it():
    """The real `vercel blob put` writes "> Success! <url>" to STDERR and
    leaves stdout empty. The first real sync recorded ~200 photos with a
    blank blob_url before this was noticed."""
    real_stderr = (
        "Vercel CLI 59.5.0 (Node.js 22.23.2)\n"
        "Uploading blob\n"
        "> Success! https://ie9rpnhvtobyiwot.public.blob.vercel-storage.com/photos/abc/01.jpg\n"
    )
    url = upload_photo(
        Path("data/photos/abc/01.jpg"), "abc", 1, rw_token="t",
        run=lambda *a, **k: _Result(stdout="", stderr=real_stderr),
    )
    assert url == "https://ie9rpnhvtobyiwot.public.blob.vercel-storage.com/photos/abc/01.jpg"


def test_stdout_still_wins_when_the_cli_prints_there():
    url = upload_photo(
        Path("p.jpg"), "abc", 1, rw_token="t",
        run=lambda *a, **k: _Result(stdout="https://x.public.blob.vercel-storage.com/a.jpg\n"),
    )
    assert url == "https://x.public.blob.vercel-storage.com/a.jpg"


def test_a_missing_url_raises_instead_of_returning_an_empty_string():
    """Returning '' was the actual failure mode: uploads succeeded, rows were
    written with a blank URL, and every image silently broke. Fail loudly."""
    with pytest.raises(RuntimeError, match="no blob URL"):
        upload_photo(
            Path("p.jpg"), "abc", 1, rw_token="t",
            run=lambda *a, **k: _Result(stdout="", stderr="Uploading blob\n"),
        )
