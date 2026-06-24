"""Playwright (simple) engine (R-08): no authentication.

Opens the board's post URL, fills the form from the board's field_map, submits,
and captures a screenshot on failure. Conventions a board's field_map may set:
  "submit": {"selector": "...", "type": "click"}   -> the submit button
  "_success_selector": "..."                          -> element proving success
  "_result_url_selector": "..."                       -> link to the live posting
"""

from __future__ import annotations

from app.posting.base import SCREENSHOT_DIR, PostResult, fill_form


async def post(job, board, fields: dict, attempt_id: str) -> PostResult:
    field_map = dict(board.field_map or {})
    if not field_map:
        return PostResult(
            success=False,
            error=(
                "Board has no field_map yet. Add selectors via the board "
                "onboarding UI before dispatching to this board."
            ),
        )

    submit = field_map.pop("submit", None)
    success_selector = field_map.pop("_success_selector", None)
    result_url_selector = field_map.pop("_result_url_selector", None)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()
        try:
            await page.goto(board.post_url, wait_until="domcontentloaded", timeout=30000)
            filled = await fill_form(page, field_map, board.select_map or {}, fields)

            if not submit:
                # Never report success without actually submitting.
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

            return PostResult(
                success=True,
                result_url=result_url,
                detail=f"Filled: {', '.join(filled)}",
            )
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
