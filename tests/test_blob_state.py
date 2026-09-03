"""Small state files round-tripping through Vercel Blob.

Phase 3 needs two files to survive between ephemeral sandbox runs:
`compass_state.json` (the Compass session) and
`.photo_scoring_batch_state.json` (the vision batch checkpoint). Losing the
second one breaks the never-pay-twice guarantee, which is why this has
tests rather than being a two-line helper.

Both are stored with **private** access. A public Compass session is a
credential anyone with the URL can use.
"""
import json
from pathlib import Path

import pytest
import requests

from src.blob_state import (
    STATE_PREFIX,
    get_state,
    put_state,
    state_blob_url,
)

TOKEN = "vercel_blob_rw_str12345_secretpart"
STORE_ID = "str12345"


class _Response:
    def __init__(self, status_code=200, content=b"", payload=None, text=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.text = text if text is not None else content.decode(errors="replace")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# --- addressing ----------------------------------------------------------

def test_state_lives_under_a_private_host_scoped_to_the_store():
    url = state_blob_url("compass_state.json", TOKEN)

    assert url == (
        f"https://{STORE_ID}.private.blob.vercel-storage.com/"
        f"{STATE_PREFIX}compass_state.json"
    )


def test_the_host_is_lowercased_even_though_the_token_is_not():
    """A token carries the store id in mixed case; the host is lowercase.
    Verified against the real stores: token `...Ie9RPNHVTObyiwOt...` serves
    from `ie9rpnhvtobyiwot...`. DNS forgives this, string comparison does
    not."""
    url = state_blob_url("x.json", "vercel_blob_rw_Ie9RPNHVTObyiwOt_secret")

    assert "ie9rpnhvtobyiwot.private.blob.vercel-storage.com" in url
    assert "Ie9RPNHVTObyiwOt" not in url


def test_state_is_namespaced_away_from_photos():
    """photos/ is public and world-readable; state/ must never collide."""
    assert STATE_PREFIX.rstrip("/") != "photos"
    assert state_blob_url("x.json", TOKEN).count("/state/") == 1


# --- writing -------------------------------------------------------------

def test_put_state_uploads_privately(tmp_path):
    calls = []

    def put(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response(payload={"url": "https://str12345.private.blob.vercel-storage.com/state/x.json"})

    put_state("compass_state.json", b'{"cookies":[]}', TOKEN, put=put)

    assert len(calls) == 1
    assert "pathname=state%2Fcompass_state.json" in calls[0]["url"]
    assert calls[0]["headers"]["x-vercel-blob-access"] == "private", (
        "a Compass session stored publicly is a credential anyone can use"
    )
    assert calls[0]["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert calls[0]["data"] == b'{"cookies":[]}'


def test_put_state_allows_overwrite_so_a_refreshed_session_replaces_the_old():
    calls = []
    put_state("compass_state.json", b"{}", TOKEN,
              put=lambda url, **k: (calls.append(k), _Response(payload={"url": "u"}))[1])

    assert calls[0]["headers"]["x-allow-overwrite"] == "1"


def test_put_state_sends_json_content_type():
    calls = []
    put_state("s.json", b"{}", TOKEN,
              put=lambda url, **k: (calls.append(k), _Response(payload={"url": "u"}))[1])

    assert calls[0]["headers"]["x-content-type"] == "application/json"


def test_put_state_raises_on_a_bad_status():
    def put(url, **kwargs):
        return _Response(status_code=403, text="forbidden")

    with pytest.raises(RuntimeError, match="403"):
        put_state("s.json", b"{}", TOKEN, put=put)


# --- reading -------------------------------------------------------------

def test_get_state_returns_the_bytes():
    def get(url, **kwargs):
        assert kwargs["headers"]["authorization"] == f"Bearer {TOKEN}", (
            "a private blob is unreadable without the token"
        )
        return _Response(content=b'{"cookies":[1]}')

    assert get_state("compass_state.json", TOKEN, get=get) == b'{"cookies":[1]}'


def test_get_state_returns_none_when_absent():
    """First ever run: nothing has been uploaded. That is not an error --
    the caller falls back to a cold login."""
    assert get_state("nope.json", TOKEN, get=lambda url, **k: _Response(status_code=404)) is None


def test_get_state_raises_on_other_errors():
    """A 403 means the token is wrong, which is worth failing loudly on --
    silently treating it as 'no session' would trigger a needless cold
    login on every single run."""
    with pytest.raises(RuntimeError, match="403"):
        get_state("s.json", TOKEN, get=lambda url, **k: _Response(status_code=403, text="nope"))


def test_a_network_error_becomes_a_runtime_error():
    def get(url, **kwargs):
        raise requests.ConnectionError("reset")

    with pytest.raises(RuntimeError, match="reset"):
        get_state("s.json", TOKEN, get=get)


def test_a_timeout_is_always_set():
    calls = []
    get_state("s.json", TOKEN, get=lambda url, **k: (calls.append(k), _Response(content=b"x"))[1])

    assert calls[0]["timeout"] is not None


# --- round trip ----------------------------------------------------------

def test_a_session_survives_a_round_trip():
    store = {}

    def put(url, **kwargs):
        store[url.split("pathname=")[1]] = kwargs["data"]
        return _Response(payload={"url": "u"})

    def get(url, **kwargs):
        key = url.split("/state/")[1]
        raw = store.get("state%2F" + key)
        return _Response(content=raw) if raw is not None else _Response(status_code=404)

    session = json.dumps({"cookies": [{"name": "sid", "value": "abc"}]}).encode()
    put_state("compass_state.json", session, TOKEN, put=put)

    assert json.loads(get_state("compass_state.json", TOKEN, get=get)) == {
        "cookies": [{"name": "sid", "value": "abc"}]
    }
