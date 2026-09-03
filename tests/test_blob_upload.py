"""Photo upload goes straight to the Vercel Blob REST API.

It used to shell out to the `vercel` CLI once per photo, which cost ~0.6s of
Node startup apiece, made the CLI an undeclared runtime dependency, and --
worst -- defaulted to `--multipart true`. Vercel bills each part as an
Advanced Operation, so a 3,000-photo backfill once consumed 11,000 operations
and suspended the store for 30 days.

A single-part REST PUT is exactly one operation per photo, so these tests pin
"one HTTP request per photo" as hard as they pin the URL and headers.

The REST contract here was read out of @vercel/blob 2.8.0 as bundled with the
installed Vercel CLI (dist/chunk-YYMLUMXS.js: `defaultVercelBlobApiUrl`,
`BLOB_API_VERSION`, `createPutHeaders`, `createPutMethod`), not from memory --
it is the SDK's internal API and is not in the public REST docs.
"""
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest
import requests

import src.blob_upload as blob_upload
from src.photos import photo_filename
from src.blob_upload import (
    BLOB_API_URL,
    BLOB_API_VERSION,
    already_uploaded,
    upload_photo,
)
from src.turso_db import ensure_schema

TOKEN = "vercel_blob_rw_str12345_secretpart"
STORE_ID = "str12345"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


_DEFAULT = object()


class _Response:
    def __init__(self, status_code=200, payload=_DEFAULT, text=None):
        self.status_code = status_code
        # payload=None means "the body is not JSON at all", which is distinct
        # from "payload not specified" -- conflating the two silently made the
        # unparseable-body test assert nothing.
        self._payload = {
            "url": "https://str12345.public.blob.vercel-storage.com/photos/abc/01.jpg",
            "pathname": "photos/abc/01.jpg",
            "contentType": "image/jpeg",
        } if payload is _DEFAULT else payload
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# The photo's source URL is its identity: the Blob pathname carries
# sha1(url)[:8] so a replaced photo lands on a NEW pathname instead of
# overwriting the old one behind Blob's CDN cache (default max-age one month),
# which would have left the viewer showing the old image anyway.
SOURCE_URL = "https://cdn.example.com/abc/1.jpg"


@pytest.fixture
def photo(tmp_path) -> Path:
    p = tmp_path / photo_filename(1, SOURCE_URL)
    p.write_bytes(b"\xff\xd8\xff-not-really-a-jpeg")
    return p


def _capture():
    """Returns (put_callable, calls) where calls records every request."""
    calls = []

    def fake_put(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    return fake_put, calls


# --- already_uploaded is unchanged ---------------------------------------

def test_already_uploaded_false_when_no_row_exists():
    assert already_uploaded(_connect(), "abc", 1) is False


def test_already_uploaded_true_after_a_row_exists():
    conn = _connect()
    conn.execute(
        "INSERT INTO hosted_photos (listing_id, position, blob_url) "
        "VALUES ('abc', 1, 'https://example.public.blob.vercel-storage.com/abc/01.jpg')"
    )
    conn.commit()

    assert already_uploaded(conn, "abc", 1) is True


# --- the request shape ----------------------------------------------------

def test_upload_photo_puts_to_the_blob_api_with_the_pathname(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert len(calls) == 1
    assert calls[0]["url"].startswith(BLOB_API_URL)
    expected = quote(f"photos/abc/{photo_filename(1, SOURCE_URL)}", safe="")
    assert f"pathname={expected}" in calls[0]["url"]


def test_pathname_is_zero_padded_by_position(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 7, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert "pathname=photos%2Fabc%2F07-" in calls[0]["url"]


def test_a_changed_photo_gets_a_new_pathname_not_an_overwrite(photo):
    """An overwrite would sit behind Blob's CDN cache -- default
    cacheControlMaxAge is one month -- so the viewer would keep serving the
    old image even though the row and the bytes were both replaced."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)
    upload_photo(
        photo, "abc", 1, rw_token=TOKEN,
        source_url="https://cdn.example.com/abc/DIFFERENT.jpg", put=put,
    )

    assert calls[0]["url"] != calls[1]["url"]


def test_the_photo_bytes_are_the_request_body(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["data"] == photo.read_bytes()


def test_authorization_is_a_bearer_read_write_token(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_api_version_header_is_sent(photo):
    """The Blob API is versioned and rejects requests without it."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["headers"]["x-api-version"] == BLOB_API_VERSION


def test_store_id_is_parsed_out_of_the_token(photo):
    """vercel_blob_rw_<storeId>_<secret> -- the SDK splits on '_' and takes
    index 3. The API wants it as its own header."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["headers"]["x-vercel-blob-store-id"] == STORE_ID


def test_access_is_public_and_overwrite_is_allowed(photo):
    """public matches the viewer's plain <img>/next/image usage; overwrite
    keeps a rerun safe after a listing's photo changes."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["headers"]["x-vercel-blob-access"] == "public"
    assert calls[0]["headers"]["x-allow-overwrite"] == "1"


def test_content_type_is_jpeg(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["headers"]["x-content-type"] == "image/jpeg"


def test_a_request_timeout_is_always_set(photo):
    """No timeout means a stalled upload hangs the whole publish forever."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert calls[0]["timeout"] is not None


# --- the billing guarantee ------------------------------------------------

def test_exactly_one_http_request_per_photo(photo):
    """The whole point of the swap. The CLI's multipart default turned each
    photo into start + parts + complete, each billed as an Advanced
    Operation -- 11,000 of them against a 2,000/month budget. Single-part
    means one request, one operation."""
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert len(calls) == 1


def test_no_multipart_parameters_are_sent(photo):
    put, calls = _capture()

    upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert "multipart" not in calls[0]["url"].lower()
    assert not any("multipart" in k.lower() for k in calls[0]["headers"])


def test_the_module_no_longer_shells_out(photo):
    """No subprocess, so no Node startup per photo and no undeclared `vercel`
    CLI dependency on PATH."""
    assert not hasattr(blob_upload, "subprocess")
    source = Path("src/blob_upload.py").read_text()
    assert "import subprocess" not in source
    assert "subprocess.run" not in source
    assert '"vercel", "blob", "put"' not in source


# --- failure modes --------------------------------------------------------

def test_a_non_2xx_response_raises_runtime_error(photo):
    def put(url, **kwargs):
        return _Response(status_code=403, payload={"error": {"code": "forbidden"}})

    with pytest.raises(RuntimeError, match="403"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_the_error_message_carries_the_response_body(photo):
    def put(url, **kwargs):
        return _Response(status_code=401, payload=None, text="invalid token")

    with pytest.raises(RuntimeError, match="invalid token"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_a_network_error_becomes_a_runtime_error(photo):
    """Callers catch one exception type regardless of how the upload failed --
    publish.py counts failures and leaves no hosted_photos row behind."""
    def put(url, **kwargs):
        raise requests.ConnectionError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_a_response_with_no_url_raises_instead_of_returning_empty(photo):
    """Returning '' was the actual historical failure: uploads succeeded,
    rows were written with a blank URL, and every image silently broke."""
    def put(url, **kwargs):
        return _Response(payload={"pathname": "photos/abc/01.jpg"})

    with pytest.raises(RuntimeError, match="no blob URL"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_an_empty_url_string_raises(photo):
    def put(url, **kwargs):
        return _Response(payload={"url": ""})

    with pytest.raises(RuntimeError, match="no blob URL"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_an_unparseable_success_body_raises(photo):
    def put(url, **kwargs):
        return _Response(payload=None, text="<html>gateway timeout</html>")

    with pytest.raises(RuntimeError, match="no blob URL"):
        upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)


def test_a_missing_local_file_raises_before_any_request(photo, tmp_path):
    calls = []

    def put(url, **kwargs):
        calls.append(url)
        return _Response()

    with pytest.raises((FileNotFoundError, RuntimeError)):
        upload_photo(
            tmp_path / "gone.jpg", "abc", 1, rw_token=TOKEN,
            source_url=SOURCE_URL, put=put,
        )
    assert calls == [], "a missing file must not burn an operation"


# --- the returned URL -----------------------------------------------------

def test_upload_photo_returns_the_url_from_the_response(photo):
    def put(url, **kwargs):
        return _Response(payload={
            "url": "https://str12345.public.blob.vercel-storage.com/photos/abc/01.jpg"
        })

    url = upload_photo(photo, "abc", 1, rw_token=TOKEN, source_url=SOURCE_URL, put=put)

    assert url == "https://str12345.public.blob.vercel-storage.com/photos/abc/01.jpg"


def test_nothing_shells_out_to_the_vercel_cli_any_more():
    """Acceptance criterion for #15: no entry point fails fast on a missing
    `vercel` binary, and the systemd PATH workaround that existed only for
    that is gone. publish.py, which held the original preflight, was deleted
    with the Turso cutover."""
    assert not Path("publish.py").exists()
    for entry in ("scrape.py", "check.py", "pipeline.py"):
        source = Path(entry).read_text()
        assert "shutil.which" not in source
        assert 'which("vercel")' not in source
