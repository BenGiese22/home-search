from pathlib import Path

from playwright.sync_api import BrowserContext, Page


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
