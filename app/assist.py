"""Assisted (human-in-the-loop) posting — for CAPTCHA / Cloudflare / paid boards.

Runs LOCALLY on your machine (it opens a visible browser, so it can't run on the
headless Railway server). It logs in where it can, auto-fills every mapped field
from the Ashby job data, then pauses so you can solve any CAPTCHA, review, and
click Submit yourself. Nothing is bypassed — a real human completes the
challenge. You then confirm the outcome and it's recorded in the dashboard.

Usage:
    python -m app.assist <job_id> "<board name>"

Tip: run `python -m app.assist --list <job_id>` to see board names + statuses.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.db import SessionLocal
from app.models import BoardConfig, JobQueue, PostingAttempt, PostingStatus
from app.posting.base import build_master_fields, translate_value
from app.posting.utm import construct_apply_url

settings = get_settings()

# field_map keys that aren't form fields to fill.
_CONTROL_KEYS = {"login", "submit", "_success_selector", "_result_url_selector"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fill(page, board, fields: dict) -> list[str]:
    filled = []
    select_map = board.select_map or {}
    for master_field, spec in (board.field_map or {}).items():
        if master_field in _CONTROL_KEYS:
            continue
        value = fields.get(master_field)
        if value in (None, ""):
            continue
        selector = spec if isinstance(spec, str) else spec.get("selector")
        kind = "fill" if isinstance(spec, str) else spec.get("type", "fill")
        if not selector:
            continue
        value = translate_value(master_field, str(value), select_map)
        try:
            if kind == "select":
                page.select_option(selector, label=value)
            elif kind == "check":
                page.check(selector)
            else:
                page.fill(selector, value)
            filled.append(master_field)
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠ couldn't fill {master_field} ({selector}): {str(exc)[:60]}")
    return filled


def assist(job_id: str, board_name: str) -> None:
    from playwright.sync_api import sync_playwright

    session = SessionLocal()
    job = session.get(JobQueue, job_id)
    if job is None:
        print(f"Job {job_id} not found.")
        return
    board = session.query(BoardConfig).filter_by(name=board_name).one_or_none()
    if board is None:
        print(f"Board {board_name!r} not found.")
        return

    attempt = (
        session.query(PostingAttempt).filter_by(job_id=job.id, board_id=board.id).one_or_none()
    )
    if attempt is None:
        attempt = PostingAttempt(job_id=job.id, board_id=board.id)
        session.add(attempt)
    attempt.status = PostingStatus.in_progress
    attempt.started_at = _now()

    resolved = construct_apply_url(job, board.utm_source)
    attempt.resolved_apply_url = resolved
    session.commit()

    fields = build_master_fields(job, board, resolved)
    login = (board.field_map or {}).get("login")
    username, password = settings.board_credentials(board.credentials_ref or "")

    print(f"\n▶ Assisted posting: {job.title!r} → {board.name}")
    print(f"   Apply URL (UTM): {resolved}")
    print(f"   Opening a browser window…\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(board.post_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            # Best-effort auto-login if a login block + credentials exist.
            if login and username and password and page.locator(login["password"]).count():
                try:
                    page.fill(login["username"], username)
                    page.fill(login["password"], password)
                    page.click(login["submit"])
                    page.wait_for_timeout(4000)
                    page.goto(board.post_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    print("   ✓ logged in automatically")
                except Exception as exc:  # noqa: BLE001
                    print(f"   ⚠ auto-login failed ({str(exc)[:60]}). Log in manually in the window.")

            filled = _fill(page, board, fields)
            print(f"   ✓ auto-filled {len(filled)} fields: {', '.join(filled) or '(none)'}")
            print("\n   ─────────────────────────────────────────────")
            print("   Now in the browser window:")
            print("     1. Log in if needed, 2. solve any CAPTCHA,")
            print("     3. fix anything, 4. click the board's Submit button.")
            print("   ─────────────────────────────────────────────")
            input("\n   Press Enter here once you've submitted (or to stop)… ")

            ok = input("   Did it post successfully? [y/N] ").strip().lower().startswith("y")
            attempt.status = PostingStatus.success if ok else PostingStatus.failed
            attempt.error_message = "Posted via assisted mode." if ok else "Assisted attempt not completed."
            if ok:
                board.last_used_at = _now()
            attempt.finished_at = _now()
            session.commit()
            print(f"\n   Recorded: {attempt.status.value}. You can close this.\n")
        finally:
            browser.close()


def _list(job_id: str) -> None:
    session = SessionLocal()
    job = session.get(JobQueue, job_id)
    if not job:
        print("Job not found.")
        return
    print(f"\nBoards for: {job.title}\n")
    for b in session.query(BoardConfig).order_by(BoardConfig.name).all():
        flags = []
        if b.is_paid:
            flags.append("PAID")
        if b.requires_assist:
            flags.append(f"ASSIST:{b.assist_reason}")
        if not b.field_map:
            flags.append("no-map")
        print(f"  {b.name:28} {b.distribution_type.value:18} {' '.join(flags)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--list":
        _list(args[1])
    elif len(args) >= 2:
        assist(args[0], " ".join(args[1:]))
    else:
        print('Usage: python -m app.assist <job_id> "<board name>"')
        print('       python -m app.assist --list <job_id>')
