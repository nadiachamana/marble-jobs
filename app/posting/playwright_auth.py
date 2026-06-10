"""Playwright (authenticated) engine (R-09).

Logs in with credentials resolved from env/secrets by the board's
credentials_ref (never from the DB), then fills and submits the post form.

The board's field_map carries a "login" block describing the login flow:
  "login": {
    "url": "https://.../login",          # optional; defaults to post_url
    "username": "#email",
    "password": "#password",
    "submit": "button[type=submit]",
    "success": "#dashboard"               # optional; element proving login worked
  }
"""

from __future__ import annotations

from app.config import get_settings
from app.posting.base import SCREENSHOT_DIR, PostResult, fill_form

settings = get_settings()


async def post(job, board, fields: dict, attempt_id: str) -> PostResult:
    field_map = dict(board.field_map or {})
    login = field_map.pop("login", None)
    if not field_map:
        return PostResult(
            success=False,
            error="Board has no field_map yet. Add selectors before dispatching.",
        )
    if not login:
        return PostResult(
            success=False,
            error="Authenticated board is missing a 'login' block in field_map.",
        )

    username, password = settings.board_credentials(board.credentials_ref or "")
    if not username or not password:
        return PostResult(
            success=False,
            error=(
                f"No credentials found for credentials_ref '{board.credentials_ref}'. "
                f"Add BOARD_{(board.credentials_ref or '').upper().replace('-', '_')}_"
                "USERNAME/_PASSWORD to secrets."
            ),
        )

    submit = field_map.pop("submit", None)
    success_selector = field_map.pop("_success_selector", None)
    result_url_selector = field_map.pop("_result_url_selector", None)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # 1) Log in
            await page.goto(login.get("url") or board.post_url, wait_until="domcontentloaded", timeout=30000)
            await page.fill(login["username"], username)
            await page.fill(login["password"], password)
            await page.click(login["submit"])
            await page.wait_for_load_state("networkidle", timeout=30000)
            if login.get("success"):
                await page.wait_for_selector(login["success"], timeout=15000)

            # 2) Navigate to the post form and fill
            await page.goto(board.post_url, wait_until="domcontentloaded", timeout=30000)
            filled = await fill_form(page, field_map, board.select_map or {}, fields)

            if not submit:
                shot = await _screenshot(page, attempt_id)
                return PostResult(
                    success=False,
                    error="Filled the form but no 'submit' selector is configured — "
                    "add one to field_map (or use assisted mode) before live posting.",
                    screenshot_path=shot,
                    detail=f"Filled: {', '.join(filled)}",
                )

            selector = submit if isinstance(submit, str) else submit.get("selector")
            await page.click(selector)
            await page.wait_for_load_state("networkidle", timeout=30000)
            if success_selector:
                await page.wait_for_selector(success_selector, timeout=15000)

            result_url = None
            if result_url_selector:
                try:
                    result_url = await page.get_attribute(result_url_selector, "href")
                except Exception:
                    result_url = None

            return PostResult(success=True, result_url=result_url, detail=f"Filled: {', '.join(filled)}")
        except Exception as exc:  # noqa: BLE001
            shot = await _screenshot(page, attempt_id)
            return PostResult(success=False, error=str(exc), screenshot_path=shot)
        finally:
            await browser.close()


async def _screenshot(page, attempt_id: str) -> str | None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{attempt_id}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return None
