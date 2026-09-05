"""Moving the Compass session between the desktop and the private Blob store.

The session is a credential and the only thing standing between the pipeline
and a cold login on every run, so both directions are about not destroying a
working copy: an empty push, or a pull that silently lands on a good file.

Nothing here makes a request.
"""

import pytest

from ops.state import LOCAL_PATH, STATE_NAME, TOKEN_VAR, main, pull, push

TOKEN = "vercel_blob_rw_StoreId123_secret"


class Response:
    def __init__(self, status=200, content=b"", payload=None):
        self.status_code = status
        self.content = content
        self._payload = payload or {"url": "https://x/state/compass_state.json"}
        self.text = ""

    def json(self):
        return self._payload


# --- push -----------------------------------------------------------------

def test_push_uploads_the_local_session(tmp_path):
    local = tmp_path / "compass_state.json"
    local.write_bytes(b'{"cookies":[]}')
    sent = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = headers
        return Response()

    push(TOKEN, local=local, put=fake_put)

    assert sent["data"] == b'{"cookies":[]}'
    assert f"state%2F{STATE_NAME}" in sent["url"] or STATE_NAME in sent["url"]


def test_push_uses_private_access(tmp_path):
    """A Compass session behind a public URL is a credential anyone holding
    the link can replay."""
    local = tmp_path / "compass_state.json"
    local.write_bytes(b"{}")
    sent = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        sent.update(headers)
        return Response()

    push(TOKEN, local=local, put=fake_put)

    assert sent["x-vercel-blob-access"] == "private"


def test_push_refuses_an_empty_file(tmp_path):
    """Zero bytes is what a half-written session looks like, and pushing one
    replaces the last good copy with nothing."""
    local = tmp_path / "compass_state.json"
    local.write_bytes(b"")

    with pytest.raises(ValueError, match="empty"):
        push(TOKEN, local=local, put=lambda *a, **k: Response())


def test_push_says_what_to_do_when_there_is_no_session(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run a scrape first"):
        push(TOKEN, local=tmp_path / "missing.json", put=lambda *a, **k: Response())


# --- pull -----------------------------------------------------------------

def test_pull_writes_the_stored_session(tmp_path):
    local = tmp_path / "nested" / "compass_state.json"

    data = pull(TOKEN, local=local, get=lambda *a, **k: Response(content=b'{"c":1}'))

    assert data == b'{"c":1}'
    assert local.read_bytes() == b'{"c":1}'


def test_pull_refuses_to_land_on_an_existing_session(tmp_path):
    """There is no way to tell which copy is newer -- the store returns bytes,
    not a version -- so the default must never silently replace one that might
    be the working session."""
    local = tmp_path / "compass_state.json"
    local.write_bytes(b"the good one")

    with pytest.raises(FileExistsError, match="--force"):
        pull(TOKEN, local=local, get=lambda *a, **k: Response(content=b"other"))

    assert local.read_bytes() == b"the good one"


def test_force_replaces_it(tmp_path):
    local = tmp_path / "compass_state.json"
    local.write_bytes(b"old")

    pull(TOKEN, local=local, force=True, get=lambda *a, **k: Response(content=b"new"))

    assert local.read_bytes() == b"new"


def test_pull_round_trips_bytes_unchanged(tmp_path):
    """The session is JSON that Playwright wrote and Playwright reads back;
    anything that reformats it is a bug."""
    original = b'{"cookies": [{"name": "x", "value": "\\u00e9"}]}'
    local = tmp_path / "compass_state.json"

    pull(TOKEN, local=local, get=lambda *a, **k: Response(content=original))

    assert local.read_bytes() == original


def test_pull_reports_an_empty_store(tmp_path):
    with pytest.raises(FileNotFoundError, match="push one first"):
        pull(TOKEN, local=tmp_path / "x.json", get=lambda *a, **k: Response(status=404))


# --- cli ------------------------------------------------------------------

def test_an_unknown_command_is_refused():
    assert main(["sync"], env={TOKEN_VAR: TOKEN}) == 2
    assert main([], env={TOKEN_VAR: TOKEN}) == 2


def test_a_missing_token_names_the_variable(capsys):
    assert main(["push"], env={}) == 1
    err = capsys.readouterr().err
    assert TOKEN_VAR in err


def test_the_error_never_prints_the_token(capsys):
    assert main(["push"], env={TOKEN_VAR: ""}) == 1
    assert TOKEN not in capsys.readouterr().err


def test_the_default_target_is_the_path_the_scraper_writes():
    """If these ever diverge, push uploads a stale file and pull lands
    somewhere the browser never looks."""
    import scrape

    assert LOCAL_PATH == scrape.AUTH_STATE_PATH
