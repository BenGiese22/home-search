"""The email transport: it sends, or it says nothing and returns False."""

import json

from src.mailer import RESEND_URL, send_email


def recorder(status=200, text="", raises=None):
    def post(url, headers=None, data=None, timeout=None):
        post.calls.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        if raises:
            raise raises
        return type("R", (), {"status_code": status, "text": text})()

    post.calls = []
    return post


def test_a_successful_send_reports_true():
    post = recorder()
    assert send_email("re_key", "ben@example.com", "Subject", "Body", post=post) is True
    assert post.calls[0]["url"] == RESEND_URL


def test_the_payload_carries_the_message():
    post = recorder()
    send_email("re_key", "ben@example.com", "2 new listings", "the body", post=post)
    payload = json.loads(post.calls[0]["data"])
    assert payload["to"] == ["ben@example.com"]
    assert payload["subject"] == "2 new listings"
    assert payload["text"] == "the body"


def test_the_key_travels_as_a_bearer_token():
    post = recorder()
    send_email("re_key", "ben@example.com", "s", "b", post=post)
    assert post.calls[0]["headers"]["Authorization"] == "Bearer re_key"


def test_no_key_sends_nothing_and_says_nothing(capsys):
    """Opt-in. With the variable unset the pipeline behaves exactly as it did
    before this existed -- not a warning on every run, which is how a warning
    stops being read."""
    post = recorder()
    assert send_email("", "ben@example.com", "s", "b", post=post) is False
    assert post.calls == []
    assert capsys.readouterr().out == ""


def test_no_recipient_sends_nothing():
    post = recorder()
    assert send_email("re_key", "  ", "s", "b", post=post) is False
    assert post.calls == []


def test_a_rejected_send_reports_false_without_raising(capsys):
    post = recorder(status=422, text="domain is not verified")
    assert send_email("re_key", "ben@example.com", "s", "b", post=post) is False
    assert "domain is not verified" in capsys.readouterr().out


def test_a_transport_failure_never_escapes(capsys):
    """A failed notification about a failed run must not become the thing
    that hides it."""
    post = recorder(raises=RuntimeError("connection reset"))
    assert send_email("re_key", "ben@example.com", "s", "b", post=post) is False
    assert "connection reset" in capsys.readouterr().out


def test_the_api_key_is_never_printed(capsys):
    post = recorder(status=401, text="invalid api key provided")
    send_email("re_secret_value", "ben@example.com", "s", "b", post=post)
    assert "re_secret_value" not in capsys.readouterr().out


def test_whitespace_is_trimmed_off_the_key_and_recipient():
    post = recorder()
    send_email("  re_key \n", " ben@example.com ", "s", "b", post=post)
    assert post.calls[0]["headers"]["Authorization"] == "Bearer re_key"
    assert json.loads(post.calls[0]["data"])["to"] == ["ben@example.com"]
