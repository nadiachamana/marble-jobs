"""Review submission, dispatch, and retry (R-12, R-14).

Saving the review updates the editable fields and the board selection. Dispatch
flips the job to queued and schedules the background posting run. Retry re-runs a
single failed board attempt.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import JobQueue, JobStatus, PostingAttempt, PostingStatus
from app.posting.dispatcher import run_dispatch

router = APIRouter(tags=["dispatch"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_int(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


@router.post("/jobs/{job_id}/review")
def save_review(
    job_id: str,
    db: Session = Depends(get_db),
    seniority: str = Form(default=""),
    function_category: str = Form(default=""),
    industry_tags: str = Form(default=""),
    location_city: str = Form(default=""),
    salary_min: str = Form(default=""),
    salary_max: str = Form(default=""),
    salary_currency: str = Form(default=""),
    deadline: str = Form(default=""),
    board_ids: list[str] = Form(default=[]),
):
    job = db.get(JobQueue, job_id)
    if job is None:
        return RedirectResponse(url="/", status_code=303)

    job.seniority = seniority or None
    job.function_category = function_category or None
    job.industry_tags = [t.strip() for t in industry_tags.split(",") if t.strip()]
    job.location_city = location_city or None
    job.salary_min = _parse_int(salary_min)
    job.salary_max = _parse_int(salary_max)
    job.salary_currency = salary_currency or None
    if deadline:
        try:
            job.deadline = datetime.fromisoformat(deadline)
        except ValueError:
            pass
    job.selected_board_ids = board_ids
    job.reviewed_at = _now()
    if job.status == JobStatus.pending:
        job.status = JobStatus.reviewed
    db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/dispatch")
def dispatch(
    job_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    seniority: str = Form(default=""),
    function_category: str = Form(default=""),
    industry_tags: str = Form(default=""),
    location_city: str = Form(default=""),
    salary_min: str = Form(default=""),
    salary_max: str = Form(default=""),
    salary_currency: str = Form(default=""),
    deadline: str = Form(default=""),
    board_ids: list[str] = Form(default=[]),
):
    # Persist the latest form edits first (same fields as save_review).
    save_review(
        job_id, db, seniority, function_category, industry_tags, location_city,
        salary_min, salary_max, salary_currency, deadline, board_ids,
    )
    job = db.get(JobQueue, job_id)
    if job is None or not job.selected_board_ids:
        return RedirectResponse(url=f"/jobs/{job_id}?error=no_boards", status_code=303)

    # Pre-flight: block dispatch if any selected (non-paid, non-assist) board is
    # missing a required value — surface it here, not as a failed submit.
    from app import validation
    from app.models import BoardConfig

    blockers = []
    for b in db.query(BoardConfig).filter(BoardConfig.id.in_(job.selected_board_ids)).all():
        if b.is_paid or b.requires_assist:
            continue
        miss = validation.missing_required(job, b)
        if miss:
            blockers.append(f"{b.name}: {', '.join(validation.field_label(k) for k in miss)}")
    if blockers:
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/jobs/{job_id}?error=required&detail={quote(' · '.join(blockers)[:400])}",
            status_code=303,
        )

    job.status = JobStatus.queued
    db.commit()

    background.add_task(_run, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.post("/attempts/{attempt_id}/retry")
def retry(attempt_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    attempt = db.get(PostingAttempt, attempt_id)
    if attempt is None:
        return RedirectResponse(url="/", status_code=303)
    attempt.status = PostingStatus.queued
    attempt.retry_count = 0
    attempt.error_message = None
    attempt.screenshot_path = None
    db.commit()
    background.add_task(_run_single, attempt.job_id, attempt.board_id)
    return RedirectResponse(url=f"/jobs/{attempt.job_id}", status_code=303)


@router.post("/attempts/{attempt_id}/result_url")
def set_result_url(
    attempt_id: str,
    db: Session = Depends(get_db),
    result_url: str = Form(default=""),
):
    """Manually record the live posting URL for a board (e.g. after assisted/manual
    posting) so it shows as a link in the tracking view."""
    attempt = db.get(PostingAttempt, attempt_id)
    if attempt is None:
        return RedirectResponse(url="/", status_code=303)
    attempt.result_url = result_url.strip() or None
    db.commit()
    return RedirectResponse(url=f"/jobs/{attempt.job_id}", status_code=303)


def _run(job_id: str) -> None:
    import anyio

    anyio.run(run_dispatch, job_id)


def _run_single(job_id: str, board_id: str) -> None:
    """Re-run just one board for a retry."""
    import anyio

    from app.db import SessionLocal
    from app.models import BoardConfig
    from app.notify import _post_slack
    from app.posting.dispatcher import _dispatch_one

    async def _go():
        session = SessionLocal()
        try:
            job = session.get(JobQueue, job_id)
            board = session.get(BoardConfig, board_id)
            if job and board:
                status = await _dispatch_one(session, job, board)
                # Threaded retry result under the job's original Slack message
                # (only when we have a thread to reply to).
                if job.slack_ts:
                    icon = {"success": ":white_check_mark:", "failed": ":x:", "skipped": ":fast_forward:"}
                    _post_slack(
                        f":repeat: Retry — {icon.get(status.value, '•')} *{board.name}* → {status.value}",
                        thread_ts=job.slack_ts,
                    )
        finally:
            session.close()

    anyio.run(_go)
