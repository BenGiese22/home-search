"""ensure_logged_in's completion check.

Regression for a race found by a Vercel Sandbox spike on 2026-08-30: the
password field is still present for seconds after Sign In while the WAF
challenge settles and the redirect fires, so a one-shot check reported a
successful login as a failure.
"""
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.auth import LOGIN_TIMEOUT_MS, ensure_logged_in


class Locator:
    def __init__(self, count):
        self._count = count
        self.filled = None

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def fill(self, value):
        self.filled = value

    def click(self):
        pass


class Page:
    """Records the wait that decides login success."""

    def __init__(self, has_email_form=True, detach_raises=False):
        self.has_email_form = has_email_form
        self.detach_raises = detach_raises
        self.waits = []
        self.goto_url = None

    def goto(self, url):
        self.goto_url = url

    def locator(self, selector):
        if selector == 'input[type="email"]':
            return Locator(1 if self.has_email_form else 0)
        return Locator(1)

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.waits.append((selector, state, timeout))
        if self.detach_raises and state == "detached":
            raise PlaywrightTimeoutError("timed out")

    def wait_for_load_state(self, state):
        pass


class Context:
    def __init__(self):
        self.saved_to = None

    def storage_state(self, path):
        self.saved_to = path


def _run(page, tmp_path):
    context = Context()
    ensure_logged_in(context, page, "https://compass.test/login/",
                     "e@x.com", "pw", tmp_path / "state.json")
    return context


def test_waits_for_the_password_field_to_detach_rather_than_checking_once(tmp_path: Path):
    page = Page()
    _run(page, tmp_path)
    assert ('input[type="password"]', "detached", LOGIN_TIMEOUT_MS) in page.waits


def test_a_login_that_completes_slowly_still_succeeds(tmp_path: Path):
    """The regression: the redirect lands seconds after networkidle."""
    page = Page()
    context = _run(page, tmp_path)
    assert context.saved_to is not None


def test_a_password_field_that_never_detaches_is_a_real_failure(tmp_path: Path):
    page = Page(detach_raises=True)
    with pytest.raises(RuntimeError, match="password field is still showing"):
        _run(page, tmp_path)


def test_an_existing_session_skips_the_login_form_entirely(tmp_path: Path):
    page = Page(has_email_form=False)
    context = _run(page, tmp_path)
    assert page.waits == [], "no login wait when already authenticated"
    assert context.saved_to is not None


def test_session_is_persisted_after_a_successful_login(tmp_path: Path):
    page = Page()
    context = _run(page, tmp_path)
    assert context.saved_to == str(tmp_path / "state.json")
