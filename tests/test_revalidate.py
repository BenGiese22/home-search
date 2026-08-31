"""The revalidate POST -- the one part of publish.py that survives it."""
import pytest
import requests

from src.revalidate import revalidate


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise requests.HTTPError(f"{self.status_code}")


def test_posts_to_the_viewers_revalidate_hook():
    calls = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _Response()

    assert revalidate("https://short-list.example", "s3cret", post=post) is True
    assert calls[0]["url"] == "https://short-list.example/api/revalidate"
    assert calls[0]["headers"]["Authorization"] == "Bearer s3cret"
    assert calls[0]["timeout"] is not None


def test_a_trailing_slash_does_not_produce_a_double_slash():
    calls = []
    revalidate("https://short-list.example/", "s", post=lambda url, **k: (
        calls.append(url), _Response())[1])

    assert calls[0] == "https://short-list.example/api/revalidate"


def test_a_failed_revalidate_is_reported_but_not_raised(capsys):
    """Every write already landed. Raising here would fail a run that
    succeeded; the viewer's cache expires on its own."""
    def post(url, **kwargs):
        raise requests.ConnectionError("connection refused")

    assert revalidate("https://short-list.example", "s", post=post) is False
    assert "cache will expire naturally" in capsys.readouterr().out


def test_a_non_2xx_response_is_a_failure_but_still_does_not_raise(capsys):
    assert revalidate(
        "https://short-list.example", "s", post=lambda url, **k: _Response(500)
    ) is False
    assert "revalidate call failed" in capsys.readouterr().out
