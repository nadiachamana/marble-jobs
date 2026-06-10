"""Dispatch orchestrator (R-11, R-12, R-14, R-16).

Runs in the background after Nadia clicks Dispatch. For each selected board it
resolves the UTM apply URL, runs the correct engine, records per-board status
live in the DB, captures screenshots on failure, auto-retries transient Playwright
errors up to twice, and alerts on failure. Paid boards are skipped for manual
handling (per the decision to keep payment flows manual for now).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import (
    BoardConfig,
    DistributionType,
    JobQueue,
    JobStatus,
    PostingAttempt,
    PostingStatus,
)
from app.notify import notify_dispatch_complete, notify_dispatch_started
from app.posting import email_engine, playwright_auth, playwright_simple
from app.posting.base import build_master_fields
from app.posting.utm import resolve_apply_url

MAX_RETRIES = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _run_engine(job, board, fields, attempt_id):
    if board.distribution_type == DistributionType.playwright_simple:
        return await playwright_simple.post(job, board, fields, attempt_id)
    if board.distribution_type == DistributionType.playwright_auth:
        return await playwright_auth.post(job, board, fields, attempt_id)
    if board.distribution_type == DistributionType.email:
        # Email engine is synchronous; run off the event loop.
        return await asyncio.to_thread(email_engine.post, job, board, fields, attempt_id)
    raise ValueError(f"Unknown distribution_type {board.distribution_type}")


async def _dispatch_one(session, job, board) -> PostingStatus:
    attempt = (
        session.query(PostingAttempt)
        .filter_by(job_id=job.id, board_id=board.id)
        .one_or_none()
    )
    if attempt is None:
        attempt = PostingAttempt(job_id=job.id, board_id=board.id)
        session.add(attempt)
        session.flush()

    # Paid boards: surface in the dashboard but leave the payment flow manual.
    if board.is_paid:
        attempt.status = PostingStatus.skipped
        attempt.error_message = "Paid board — complete payment & posting manually."
        attempt.finished_at = _now()
        session.commit()
        return attempt.status

    # Bot-protected boards (CAPTCHA / Cloudflare): can't be submitted headlessly.
    # Point the operator at assisted mode rather than failing on the challenge.
    if board.requires_assist:
        attempt.status = PostingStatus.skipped
        attempt.error_message = (
            f"Requires assisted mode ({board.assist_reason or 'bot-challenge'}). "
            f'Run locally: python -m app.assist {job.id} "{board.name}"'
        )
        attempt.finished_at = _now()
        session.commit()
        return attempt.status

    attempt.status = PostingStatus.in_progress
    attempt.started_at = _now()
    attempt.error_message = None
    session.commit()

    try:
        resolved = await resolve_apply_url(job, board)
        attempt.resolved_apply_url = resolved
    except Exception as exc:  # noqa: BLE001
        resolved = job.apply_url
        attempt.resolved_apply_url = resolved
        attempt.error_message = f"UTM resolution warning: {exc}"

    fields = build_master_fields(job, board, resolved)

    result = await _run_engine(job, board, fields, attempt.id)

    # Auto-retry transient Playwright failures (not config errors).
    while (
        not result.success
        and attempt.retry_count < MAX_RETRIES
        and board.distribution_type != DistributionType.email
        and "field_map" not in (result.error or "")
        and "credentials" not in (result.error or "")
    ):
        attempt.retry_count += 1
        session.commit()
        result = await _run_engine(job, board, fields, attempt.id)

    if result.success:
        attempt.status = PostingStatus.success
        attempt.result_url = result.result_url
        attempt.error_message = result.detail
        board.last_used_at = _now()
    else:
        attempt.status = PostingStatus.failed
        attempt.error_message = result.error
        attempt.screenshot_path = result.screenshot_path
        # Failures are reported once, threaded, in the completion summary
        # (notify_dispatch_complete) — not as separate channel messages.

    attempt.finished_at = _now()
    session.commit()
    return attempt.status


async def run_dispatch(job_id: str) -> None:
    """Entry point scheduled as a background task when Dispatch is clicked."""
    session = SessionLocal()
    try:
        job = session.get(JobQueue, job_id)
        if job is None:
            return
        job.status = JobStatus.in_progress
        job.dispatched_at = _now()
        session.commit()

        board_ids = list(job.selected_board_ids or [])
        boards = session.query(BoardConfig).filter(BoardConfig.id.in_(board_ids)).all()
        # Preserve the selection order.
        by_id = {b.id: b for b in boards}

        notify_dispatch_started(job, len(board_ids))

        statuses: list[PostingStatus] = []
        for bid in board_ids:
            board = by_id.get(bid)
            if board is None:
                continue
            statuses.append(await _dispatch_one(session, job, board))

        any_failed = any(s == PostingStatus.failed for s in statuses)
        job.status = JobStatus.failed if any_failed else JobStatus.completed
        session.commit()

        # One threaded summary of every board's outcome — replies under the
        # original "New job ready to review" message.
        session.refresh(job)
        notify_dispatch_complete(job, list(job.attempts))
    finally:
        session.close()
