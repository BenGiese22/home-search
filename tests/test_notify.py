"""Failure notification.

The pipeline runs unattended — on a systemd timer today, on a cron in a
sandbox under Phase 3. A run that dies at 3am with nobody watching is
indistinguishable from a run that never fired, which is the failure mode
this exists to close.

Every test here pins the same property from a different angle: **notifying
must never be able to break the thing it is reporting on.** A notifier that
raises turns a failed stage into a failed run, and a notifier that hangs
holds the pipeline's flock open behind it.
"""
import pytest
import requests

from src.notify import NTFY_TIMEOUT_SECONDS, notify


class _Response:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


def _capture():
    calls = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    return post, calls


# --- the request ---------------------------------------------------------

def test_posts_the_message_to_the_topic():
    post, calls = _capture()

    assert notify("home-search", "Scrape failed", "exit 1 after 42s", post=post) is True
    assert calls[0]["url"] == "https://ntfy.sh/home-search"
    assert calls[0]["data"] == b"exit 1 after 42s"


def test_the_title_rides_in_a_header():
    """ntfy takes the body as the message and the title as a header, so a
    title in the body would just be indistinguishable prose."""
    post, calls = _capture()

    notify("t", "Scrape failed", "details", post=post)

    assert calls[0]["headers"]["Title"] == "Scrape failed"


def test_priority_and_tags_are_sent_when_given():
    post, calls = _capture()

    notify("t", "x", "y", priority="high", tags=("warning", "house"), post=post)

    assert calls[0]["headers"]["Priority"] == "high"
    assert calls[0]["headers"]["Tags"] == "warning,house"


def test_priority_defaults_and_empty_tags_are_omitted():
    """An empty Tags header renders as a stray blank tag in the ntfy client."""
    post, calls = _capture()

    notify("t", "x", "y", post=post)

    assert calls[0]["headers"]["Priority"] == "default"
    assert "Tags" not in calls[0]["headers"]


def test_a_timeout_is_always_set():
    """Without one, a hung ntfy holds the pipeline's flock open behind it."""
    post, calls = _capture()

    notify("t", "x", "y", post=post)

    assert calls[0]["timeout"] == NTFY_TIMEOUT_SECONDS


def test_unicode_in_the_body_is_encoded_not_mangled():
    post, calls = _capture()

    notify("t", "x", "scraped 6799 West 52nd Avenue — ok", post=post)

    assert calls[0]["data"] == "scraped 6799 West 52nd Avenue — ok".encode("utf-8")


# --- it must never break the caller --------------------------------------

def test_a_transport_failure_is_swallowed(capsys):
    """The run already succeeded or already failed; the notification is
    commentary. Raising here would convert a reported failure into a
    different, less informative one."""
    def post(url, **kwargs):
        raise requests.ConnectionError("connection refused")

    assert notify("t", "x", "y", post=post) is False
    assert "notification" in capsys.readouterr().out.lower()


def test_a_non_2xx_response_is_reported_but_not_raised(capsys):
    assert notify("t", "x", "y", post=lambda url, **k: _Response(503, "unavailable")) is False
    out = capsys.readouterr().out
    assert "503" in out


def test_an_unexpected_exception_is_also_swallowed(capsys):
    """Deliberately broader than requests' own hierarchy: this is the last
    thing that runs before a script exits, and there is nothing above it to
    catch anything it lets through."""
    def post(url, **kwargs):
        raise ValueError("something entirely unexpected")

    assert notify("t", "x", "y", post=post) is False
    assert capsys.readouterr().out != ""


# --- disabled by configuration -------------------------------------------

def test_an_empty_topic_is_a_silent_no_op():
    """Notification is opt-in. With NTFY_TOPIC unset the pipeline must
    behave exactly as it does today, without warnings on every run."""
    def post(url, **kwargs):
        pytest.fail("must not post without a topic")

    assert notify("", "x", "y", post=post) is False


def test_a_whitespace_topic_counts_as_unset(capsys):
    def post(url, **kwargs):
        pytest.fail("must not post without a topic")

    assert notify("   ", "x", "y", post=post) is False
    assert capsys.readouterr().out == "", "an unset topic is not worth a warning"
