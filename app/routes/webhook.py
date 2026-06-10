"""Ashby webhook receiver (R-01, R-02, R-03, R-04, R-05).

Flow: verify signature -> fetch full job via jobPosting.info -> run inference ->
persist as pending -> notify Slack/email. Returns 200 fast; the heavy fetch +
inference happens inline but is lightweight (one API call, static lookups).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from sqlalchemy.orm import Session

from app import ashby, inference
from app.db import SessionLocal
from app.models import JobQueue, JobStatus
from app.notify import notify_new_job

router = APIRouter(prefix="/webhooks", tags=["webhook"])


@router.post("/ashby")
async def ashby_webhook(
    request: Request,
    background: BackgroundTasks,
    ashby_signature: str | None = Header(default=None, alias="Ashby-Signature"),
) -> Response:
    raw = await request.body()
    if not ashby.verify_signature(raw, ashby_signature):
        return Response(status_code=401, content="invalid signature")

    payload = await request.json()
    parsed = ashby.parse_webhook(payload)

    if parsed.get("action") != "jobPostingPublish":
        return Response(status_code=200, content="ignored")

    posting_id = parsed.get("ashby_job_posting_id")
    if not posting_id:
        return Response(status_code=200, content="no jobPosting id")

    # Persist a minimal record immediately so nothing is lost, then enrich.
    db: Session = SessionLocal()
    try:
        job = db.query(JobQueue).filter_by(ashby_job_posting_id=posting_id).one_or_none()
        if job is None:
            job = JobQueue(ashby_job_posting_id=posting_id, title=parsed.get("title") or "(untitled)")
            db.add(job)
        job.ashby_job_id = parsed.get("ashby_job_id")
        for field in ("title", "location_country", "employment_type", "work_mode", "apply_url", "deadline"):
            if parsed.get(field):
                setattr(job, field, parsed[field])
        job.status = JobStatus.pending
        db.commit()
        job_id = job.id
    finally:
        db.close()

    # Enrich with jobPosting.info + inference in the background so we 200 fast.
    background.add_task(_enrich_and_notify, job_id, posting_id)
    return Response(status_code=200, content="ok")


def _enrich_and_notify(job_id: str, posting_id: str) -> None:
    import anyio

    anyio.run(_enrich_async, job_id, posting_id)


async def _enrich_async(job_id: str, posting_id: str) -> None:
    db: Session = SessionLocal()
    try:
        job = db.get(JobQueue, job_id)
        if job is None:
            return
        try:
            results = await ashby.fetch_job_posting(posting_id)
            info = ashby.parse_job_info(results)
            for field, value in info.items():
                if field == "is_remote":
                    continue
                if value:
                    setattr(job, field, value)
            is_remote = info.get("is_remote", False)
        except Exception as exc:  # noqa: BLE001 — keep the record, note the failure
            job.notes = f"jobPosting.info fetch failed: {exc}"
            is_remote = bool(job.work_mode and "remote" in job.work_mode.lower())

        inferred = inference.infer_all(
            title=job.title,
            department=job.department,
            country=job.location_country,
            is_remote=is_remote,
        )
        for field, value in inferred.items():
            # Don't clobber a real city from Ashby with a fallback.
            if field == "location_city" and job.location_city:
                continue
            setattr(job, field, value)
        db.commit()
        db.refresh(job)
        # Post the parent Slack message and remember its ts so dispatch updates
        # thread under it instead of spamming the channel.
        ts = notify_new_job(job)
        if ts:
            job.slack_ts = ts
            db.commit()
    finally:
        db.close()
