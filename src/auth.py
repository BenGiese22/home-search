from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from src.config import Config


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
        page.wait_for_load_state("networkidle")

        if page.locator('input[type="password"]').count() > 0:
            raise RuntimeError(
                "Compass login failed: the password field is still showing "
                "after Sign In. Check COMPASS_EMAIL/COMPASS_PASSWORD in .env, "
                "and verify the selectors in ensure_logged_in still match "
                "Compass's real login form."
            )

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
