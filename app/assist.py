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

_assist_sessionmaker = None


def _session():
    """Session for the assist tool. Uses ASSIST_DATABASE_URL (e.g. the Railway
    Postgres public URL) when set, so the local tool can act on real production
    jobs; otherwise falls back to the normal local database."""
    global _assist_sessionmaker
    url = settings.assist_database_url.strip()
    if not url:
        return SessionLocal()
    if _assist_sessionmaker is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        engine = create_engine(url, pool_pre_ping=True, future=True)
        _assist_sessionmaker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        host = url.split("@")[-1].split("/")[0]
        print(f"[assist] using production database at {host}")
    return _assist_sessionmaker()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Short per-field timeout: in assisted mode a human finishes anything we miss,
# so we skip a stubborn/wrong selector in a few seconds instead of stalling 30s.
FILL_TIMEOUT_MS = 4000


def _fill(page, board, fields: dict) -> tuple[list[str], list[str]]:
    """Best-effort auto-fill. Returns (filled, skipped) master-field names.

    Never raises — a field that can't be filled is left for the human."""
    filled: list[str] = []
    skipped: list[str] = []
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
            if kind == "richtext":
                # Rich-text editor (TinyMCE/CKEditor): fill the contenteditable
                # iframe body, not the hidden backing textarea.
                if "ifr" in selector or "iframe" in selector.lower():
                    page.frame_locator(selector).locator("body").fill(value, timeout=FILL_TIMEOUT_MS)
                else:
                    page.locator(selector).first.fill(value, timeout=FILL_TIMEOUT_MS)
                filled.append(master_field)
                continue
            loc = page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=FILL_TIMEOUT_MS)
            if kind == "select":
                loc.select_option(label=value, timeout=FILL_TIMEOUT_MS)
            elif kind == "check":
                loc.check(timeout=FILL_TIMEOUT_MS)
            else:
                loc.fill(value, timeout=FILL_TIMEOUT_MS)
            filled.append(master_field)
        except Exception:  # noqa: BLE001 — leave it for the human
            skipped.append(f"{master_field} ({selector})")
    return filled, skipped


def _db_location() -> str:
    if settings.is_sqlite:
        return "LOCAL SQLite (this computer)"
    return "the production database"


def _job_not_found(session, job_id: str) -> None:
    print(f"\nJob {job_id} not found in {_db_location()}.\n")
    jobs = session.query(JobQueue).order_by(JobQueue.created_at.desc()).limit(10).all()
    if jobs:
        print("Jobs that ARE in this database:")
        for j in jobs:
            print(f"   {j.id}  ·  {j.title}  ({j.status.value})")
    if settings.is_sqlite:
        print(
            "\nReal jobs from Ashby live in the Railway (production) database, not here.\n"
            "To assist a real job, point this tool at production for one run:\n"
            '   DATABASE_URL="<your Railway Postgres PUBLIC url>" \\\n'
            f'   python -m app.assist {job_id} "<Board Name>"\n'
            "(Copy the URL from Railway → Postgres → Variables → DATABASE_PUBLIC_URL.)"
        )


def assist(job_id: str, board_name: str) -> None:
    from playwright.sync_api import sync_playwright

    session = _session()
    job = session.get(JobQueue, job_id)
    if job is None:
        _job_not_found(session, job_id)
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

    # If the job has no description yet (Ashby enrichment may have failed), fetch
    # the full JD live so assisted mode can fill it.
    if not (job.description_plain or job.description_html) and job.ashby_job_posting_id and settings.ashby_api_key:
        try:
            import anyio

            from app import ashby

            results = anyio.run(ashby.fetch_job_posting, job.ashby_job_posting_id)
            info = ashby.parse_job_info(results)
            job.description_plain = info.get("description_plain") or job.description_plain
            job.description_html = info.get("description_html") or job.description_html
            session.commit()
            print("   ✓ fetched the full job description from Ashby")
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠ couldn't fetch description from Ashby: {str(exc)[:70]}")

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

            # Best-effort auto-login for boards whose login page has no CAPTCHA.
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

            # Let the human clear any CAPTCHA / login / navigation FIRST, so the
            # auto-fill runs on the real posting form — not the challenge page.
            print("\n   In the browser window, get the empty POSTING FORM on screen:")
            print("     • solve any CAPTCHA / Cloudflare check")
            print("     • log in if it wasn't automatic")
            print("     • open the 'post a job' form")
            input("\n   …then press Enter HERE to auto-fill it. ")

            filled, skipped = _fill(page, board, fields)
            print(f"\n   ✓ auto-filled {len(filled)} field(s): {', '.join(filled) or '(none)'}")
            if skipped:
                print(f"   ⊘ couldn't auto-fill {len(skipped)} — fill these by hand:")
                for s in skipped:
                    print(f"       · {s}")
            print("\n   Job data for copy/paste (for any fields we don't auto-fill):")
            print(f"       Company:      Marble")
            print(f"       Role:         {job.title}")
            print(f"       Location:     {fields.get('city') or ''} {fields.get('country') or ''}".rstrip())
            print(f"       Work mode:    {job.work_mode or 'Hybrid'}")
            print(f"       Employment:   {job.employment_type or 'Full-time'}")
            print(f"       Apply URL:    {resolved}")
            print(f"       Contact:      {fields.get('contact_name')} <{fields.get('contact_email')}>")
            print(f"       Phone:        {fields.get('contact_phone')}")
            print(f"       Description:  {len(job.description_plain or '')} chars "
                  f"{'(auto-filled if mapped)' if job.description_plain else '— none available'}")
            print("\n   ─────────────────────────────────────────────")
            print("   Review the form, complete anything left, then click Submit.")
            print("   ─────────────────────────────────────────────")
            input("\n   Press Enter here once you've submitted (or to stop)… ")

            ok = input("   Did it post successfully? [y/N] ").strip().lower().startswith("y")
            attempt.status = PostingStatus.success if ok else PostingStatus.failed
            attempt.error_message = "Posted via assisted mode." if ok else "Assisted attempt not completed."
            if ok:
                board.last_used_at = _now()
            attempt.finished_at = _now()
            session.commit()

            # Notify Slack — threaded under the job's original message when present.
            try:
                from app.notify import _post_slack

                icon = ":white_check_mark:" if ok else ":x:"
                _post_slack(
                    f"{icon} *Assisted posting* — {board.name} → {job.title}: *{attempt.status.value}*",
                    thread_ts=job.slack_ts,
                )
            except Exception:  # noqa: BLE001
                pass

            db = "the production dashboard" if not settings.is_sqlite else "the local DB"
            print(f"\n   Recorded: {attempt.status.value} → saved to {db} + Slack. You can close this.\n")
        finally:
            browser.close()


def _list(job_id: str) -> None:
    session = _session()
    job = session.get(JobQueue, job_id)
    if not job:
        _job_not_found(session, job_id)
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
    print(f'\nRun assisted posting with:\n  python -m app.assist {job_id} "<Board Name>"\n')


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--list":
        _list(args[1])
    elif len(args) == 1 and args[0] != "--list":
        # A bare job_id → show that job's boards + the exact command to use.
        _list(args[0])
    elif len(args) >= 2:
        assist(args[0], " ".join(args[1:]))
    else:
        print('Usage: python -m app.assist <job_id> "<board name>"')
        print('       python -m app.assist <job_id>          # list this job\'s boards')
