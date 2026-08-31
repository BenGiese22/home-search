from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.config import Config

# Generous: the redirect lands well inside this, but a slow WAF challenge on a
# cold connection should not be mistaken for bad credentials.
LOGIN_TIMEOUT_MS = 30_000


def ensure_logged_in(
    context: BrowserContext,
    page: Page,
    url: str,
    email: str,
    password: str,
    storage_state_path: Path,
) -> None:
    """Navigate to url. If a login form appears (no valid session in the
    context's storage state), complete Compass's two-step email-then-password
    login and submit. Always persist the resulting session to
    storage_state_path so future runs can skip login.
    """
    page.goto(url)
    email_input = page.locator('input[type="email"]')
    if email_input.count() > 0:
        email_input.first.fill(email)
        page.locator('button[type="submit"]').first.click()
        page.wait_for_selector('input[type="password"]', timeout=10000)

        page.locator('input[type="password"]').first.fill(password)
        page.locator('button:has-text("Sign In")').first.click()

        # Wait for the password field to actually go away, rather than
        # checking once and assuming.
        #
        # The previous version waited for "networkidle" and then immediately
        # asserted the field was gone. Compass's login runs an invisible
        # reCAPTCHA Enterprise check and an AWS WAF challenge whose telemetry
        # settles *before* the post-login redirect fires, so networkidle
        # resolves seconds early and the assertion sees a page still mid-login.
        # A correct login was reported as a failure.
        #
        # Latent locally, because a stored session at storage_state_path means
        # this branch rarely executes -- but it fires on every cold login,
        # which is what any fresh environment does. Found by a Vercel Sandbox
        # spike on 2026-08-30 that captured POST /login/ -> 200,
        # GET /login/ -> 302, and the redirect landing on /overview/ about ten
        # seconds after this check had already raised.
        try:
            page.wait_for_selector(
                'input[type="password"]', state="detached", timeout=LOGIN_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "Compass login failed: the password field is still showing "
                f"{LOGIN_TIMEOUT_MS // 1000}s after Sign In. Check "
                "COMPASS_EMAIL/COMPASS_PASSWORD in .env, and verify the "
                "selectors in ensure_logged_in still match Compass's real "
                "login form."
            ) from None

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_state_path))


@contextmanager
def launch_authenticated_page(
    config: Config, login_url: str, storage_state_path: Path
) -> Iterator[Page]:
    """Launch a headless browser, restore any persisted session, and ensure
    it's logged in. Yields a ready-to-use Page; closes the browser on exit."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        storage_state = str(storage_state_path) if storage_state_path.exists() else None
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        ensure_logged_in(
            context, page, login_url,
            config.compass_email, config.compass_password, storage_state_path,
        )

        try:
            yield page
        finally:
            browser.close()
