"""Browser smoke test for the generic operator console.

This verifies UI wiring and responsive layout only. It is not evidence of live
model inference, a verified repair, or a successful container build.
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


async def sign_in(page, operator_token: str) -> None:
    """Sign in only when the login form has actually rendered."""
    token_input = page.get_by_label("Operator token", exact=True)
    try:
        await token_input.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        return  # Session cookie already present; the console rendered directly.
    await token_input.fill(operator_token)
    await page.get_by_role("button", name="Continue", exact=True).click()
    error = page.locator(".login-error")
    await page.locator(".ops-topbar").or_(error).first.wait_for()
    if await error.count():
        raise RuntimeError("Operator sign-in failed: " + (await error.inner_text()).strip())


async def main() -> None:
    operator_token = os.getenv("OPERATOR_TOKEN", "")
    if len(operator_token) < 24:
        raise RuntimeError("Set OPERATOR_TOKEN to run the authenticated browser smoke test")

    target = Path(".local/screenshots")
    target.mkdir(parents=True, exist_ok=True)
    base_url = os.getenv("WEB_URL", "http://127.0.0.1:3000").rstrip("/")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page = await context.new_page()
        await page.goto(base_url + "/")
        await sign_in(page, operator_token)

        await page.locator(".ops-topbar").wait_for()
        await page.locator("aside[aria-label='Integrations']").wait_for()
        await page.locator("main h1").first.wait_for()
        await page.screenshot(path=str(target / "operator-console.png"), full_page=True)

        for width in (375, 768, 1440):
            await page.set_viewport_size({"width": width, "height": 1000})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        await context.close()
        await browser.close()

    print("Browser smoke passed. UI screenshot saved; no repair evidence was inferred.")


if __name__ == "__main__":
    asyncio.run(main())
