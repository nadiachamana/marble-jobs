"""Playwright (authenticated) engine (R-09).

Logs in with credentials resolved from env/secrets by the board's
credentials_ref (never from the DB), then runs the shared form flow —
single page or multi-page wizard (see base.run_form_flow), including
navigating from a post-login landing page to the real posting form via
field_map["nav"]["post_link"].

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
from app.posting.base import PostResult, capture_screenshot, run_form_flow

settings = get_settings()


async def post(job, board, fields: dict, attempt_id: str, *,
               headful: bool = False, hold: bool = False) -> PostResult:
    from app.automap import clean_post_url

    field_map = dict(board.field_map or {})
    login = field_map.get("login")
    if not field_map or (len(field_map) == 1 and login):
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

    from playwright.async_api import async_playwright

    post_url = clean_post_url(board.post_url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headful, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # 1) Log in
            await page.goto(login.get("url") or post_url, wait_until="domcontentloaded", timeout=30000)
            await page.fill(login["username"], username)
            await page.fill(login["password"], password)
            await page.click(login["submit"])
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:  # noqa: BLE001 — analytics-heavy pages never settle
                await page.wait_for_timeout(3000)
            if login.get("success"):
                await page.wait_for_selector(login["success"], timeout=15000)

            # 2) Reload the post URL (login redirects often land elsewhere),
            #    then run the shared flow — it follows nav.post_link if needed.
            await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            result = await run_form_flow(page, board, fields, attempt_id,
                                         job_title=job.title, hold=hold)
            if hold and result.held:
                from app.posting.playwright_simple import _hold_handover

                result = await _hold_handover(page, board, job, result)
            return result
        except Exception as exc:  # noqa: BLE001
            shot = await capture_screenshot(page, attempt_id)
            return PostResult(success=False, error=str(exc), screenshot_path=shot)
        finally:
            await browser.close()
