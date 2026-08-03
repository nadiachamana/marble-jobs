"""Playwright (simple) engine (R-08): no authentication.

Opens the board's post URL and runs the shared form flow (single page or
multi-page wizard — see base.run_form_flow). Conventions a board's field_map
may set:
  "submit": {"selector": "...", "type": "click"}   -> the submit button
  "nav": {"post_link": ..., "next": ..., "final_submit": ..., "pages": N}
  "_success_selector": "..."                          -> element proving success
  "_result_url_selector": "..."                       -> link to the live posting
"""

from __future__ import annotations

from app.posting.base import PostResult, capture_screenshot, run_form_flow


async def post(job, board, fields: dict, attempt_id: str, *,
               headful: bool = False, hold: bool = False) -> PostResult:
    from app.automap import clean_post_url
    from playwright.async_api import async_playwright

    if not (board.field_map or {}):
        return PostResult(
            success=False,
            error=(
                "Board has no field_map yet. Add selectors via the board "
                "onboarding UI before dispatching to this board."
            ),
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headful, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()
        try:
            await page.goto(clean_post_url(board.post_url), wait_until="domcontentloaded", timeout=30000)
            result = await run_form_flow(page, board, fields, attempt_id,
                                         job_title=job.title, hold=hold)
            if hold and result.held:
                result = await _hold_handover(page, board, job, result)
            return result
        except Exception as exc:  # noqa: BLE001
            shot = await capture_screenshot(page, attempt_id)
            return PostResult(success=False, error=str(exc), screenshot_path=shot)
        finally:
            await browser.close()


async def _hold_handover(page, board, job, result: PostResult) -> PostResult:
    """Local hold-mode test: everything is filled; a human reviews the wizard
    and clicks the final submit themselves, then we record the outcome."""
    import asyncio

    from app.posting.base import find_result_url

    print("\n   ✋ HOLD — all pages filled; the final submit was NOT clicked.")
    print("      In the browser window: review each page (Back/Next), fix anything,")
    print("      then click the final submit (e.g. 'Add Vacancy') yourself.")
    await asyncio.to_thread(input, "\n   Press Enter here AFTER submitting (or to abort)… ")
    ok = (await asyncio.to_thread(input, "   Did it post successfully? [y/N] ")).strip().lower().startswith("y")
    if not ok:
        return PostResult(success=False, error="Hold-mode run not submitted.",
                          detail=result.detail, screenshot_path=result.screenshot_path)
    result_url = await find_result_url(page, board, job.title)
    if result_url:
        typed = (await asyncio.to_thread(
            input, f"   Live post URL — Enter to accept, or paste another:\n      {result_url}\n   > ")).strip()
        result_url = typed or result_url
    else:
        result_url = (await asyncio.to_thread(
            input, "   Paste the live posting URL (optional): ")).strip() or None
    return PostResult(success=True, result_url=result_url,
                      detail=(result.detail or "") + " · submitted by human (hold mode)")
