"""Review dashboard pages (R-06, R-07, R-15).

Job queue, per-job review form with smart-defaulted board selector, board
library, and the code-free board onboarding form.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import automap
from app.db import get_db
from app.models import (
    BoardConfig,
    BoardStatus,
    DistributionType,
    JobQueue,
    PostingAttempt,
)
from app.selection import default_checked_ids

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


# ───────────────────────── job queue ─────────────────────────


@router.get("/", response_class=HTMLResponse)
def queue(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(JobQueue).order_by(JobQueue.created_at.desc()).all()
    # Per-job posting summary for the badges.
    summaries = {}
    for job in jobs:
        counts: dict[str, int] = {}
        for a in job.attempts:
            counts[a.status.value] = counts.get(a.status.value, 0) + 1
        summaries[job.id] = counts
    return templates.TemplateResponse(
        request, "queue.html", {"jobs": jobs, "summaries": summaries}
    )


# ───────────────────────── review form ─────────────────────────


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def review(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = db.get(JobQueue, job_id)
    if job is None:
        return HTMLResponse("Job not found", status_code=404)

    boards = db.query(BoardConfig).order_by(BoardConfig.name).all()
    defaults = job.selected_board_ids or list(default_checked_ids(job, boards))

    grouped: dict[str, list[BoardConfig]] = {
        "playwright_simple": [],
        "playwright_auth": [],
        "email": [],
    }
    for b in boards:
        grouped.setdefault(b.distribution_type.value, []).append(b)

    attempts = {a.board_id: a for a in job.attempts}
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "job": job,
            "grouped": grouped,
            "defaults": set(defaults),
            "attempts": attempts,
            "DistributionType": DistributionType,
        },
    )


# ───────────────────────── board library ─────────────────────────


@router.get("/boards", response_class=HTMLResponse)
def boards(request: Request, db: Session = Depends(get_db)):
    boards = db.query(BoardConfig).order_by(BoardConfig.name).all()
    return templates.TemplateResponse(request, "boards.html", {"boards": boards})


@router.get("/boards/new", response_class=HTMLResponse)
def board_new(request: Request):
    return templates.TemplateResponse(
        request,
        "board_form.html",
        {"board": None, "DistributionType": DistributionType, "BoardStatus": BoardStatus},
    )


@router.get("/boards/{board_id}/edit", response_class=HTMLResponse)
def board_edit(board_id: str, request: Request, db: Session = Depends(get_db)):
    board = db.get(BoardConfig, board_id)
    if board is None:
        return HTMLResponse("Board not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "board_form.html",
        {"board": board, "DistributionType": DistributionType, "BoardStatus": BoardStatus},
    )


@router.post("/boards/save")
def board_save(
    db: Session = Depends(get_db),
    board_id: str = Form(default=""),
    name: str = Form(...),
    url: str = Form(default=""),
    post_url: str = Form(default=""),
    distribution_type: str = Form(...),
    status: str = Form(default="mapped"),
    contact_email: str = Form(default=""),
    credentials_ref: str = Form(default=""),
    utm_source: str = Form(default=""),
    ashby_tracker_url: str = Form(default=""),
    is_paid: str = Form(default=""),
    field_map: str = Form(default="{}"),
    select_map: str = Form(default="{}"),
    default_for_tags: str = Form(default=""),
    notes: str = Form(default=""),
):
    board = db.get(BoardConfig, board_id) if board_id else None
    if board is None:
        board = BoardConfig(name=name)
        db.add(board)

    board.name = name
    board.url = url or None
    board.post_url = post_url or None
    board.distribution_type = DistributionType(distribution_type)
    board.status = BoardStatus(status)
    board.contact_email = contact_email or None
    board.credentials_ref = credentials_ref or None
    board.utm_source = utm_source or None
    board.ashby_tracker_url = ashby_tracker_url or None
    board.is_paid = bool(is_paid)
    board.notes = notes or None
    board.default_for_tags = [t.strip() for t in default_for_tags.split(",") if t.strip()]

    # JSON editors — keep prior value on parse error rather than wiping config.
    try:
        board.field_map = json.loads(field_map or "{}")
    except json.JSONDecodeError:
        pass
    try:
        board.select_map = json.loads(select_map or "{}")
    except json.JSONDecodeError:
        pass

    db.commit()
    return RedirectResponse(url="/boards", status_code=303)


# ───────────────────────── auto-map from URL (R-15) ─────────────────────────


@router.post("/boards/{board_id}/automap")
async def board_automap(board_id: str, db: Session = Depends(get_db)):
    """Inspect the board's live post form and auto-generate its field_map.

    This is the 'add a board with just a URL' path: fill in name + post URL
    (+ credentials_ref for login boards), save, then click Auto-map.
    """
    board = db.get(BoardConfig, board_id)
    if board is None:
        return HTMLResponse("Board not found", status_code=404)
    if not board.post_url:
        return RedirectResponse(f"/boards/{board_id}/edit?msg={quote('Set a post URL first.')}", status_code=303)

    result = await automap.automap_board(board)
    saved = automap.apply_result(board, result, db)
    n = automap.real_field_count(result["field_map"])

    if result["bot_challenge"]:
        msg = f"Bot-challenge detected — flagged for assisted mode ({result['assist_reason']})."
    elif saved and "submit" in result["field_map"]:
        msg = f"Auto-mapped {n} fields + submit button. Review and tweak below."
    elif saved:
        msg = f"Auto-mapped {n} fields, but no submit button found — add a 'submit' selector."
    else:
        msg = "Too few fields found — the form may need login or didn't finish rendering. Existing map kept."
    return RedirectResponse(f"/boards/{board_id}/edit?msg={quote(msg)}", status_code=303)


# ───────────────────────── screenshot view (R-16) ─────────────────────────


@router.get("/attempts/{attempt_id}/screenshot")
def attempt_screenshot(attempt_id: str, db: Session = Depends(get_db)):
    attempt = db.get(PostingAttempt, attempt_id)
    if attempt is None or not attempt.screenshot_path or not Path(attempt.screenshot_path).exists():
        return HTMLResponse("No screenshot", status_code=404)
    return FileResponse(attempt.screenshot_path, media_type="image/png")
