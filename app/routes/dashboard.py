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


def _normalize_url(u: str | None) -> str:
    """Canonical form for duplicate-checking: lowercase, no scheme/www,
    no query/fragment, no trailing slash. '' when unset."""
    from urllib.parse import urlsplit

    if not u or not u.strip():
        return ""
    raw = u.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw.lower())
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def _find_duplicate(db: Session, name: str, url: str, post_url: str,
                    exclude_id: str = "") -> BoardConfig | None:
    """First non-archived board matching this name (case-insensitive) or any of
    these URLs (normalized), excluding the board being edited."""
    norm_name = name.strip().casefold()
    norm_urls = {_normalize_url(url), _normalize_url(post_url)} - {""}
    for b in db.query(BoardConfig).filter(BoardConfig.archived.isnot(True)).all():
        if b.id == exclude_id:
            continue
        if b.name.strip().casefold() == norm_name:
            return b
        if norm_urls & ({_normalize_url(b.url), _normalize_url(b.post_url)} - {""}):
            return b
    return None


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

    boards = (
        db.query(BoardConfig)
        .filter(BoardConfig.archived.isnot(True))
        .order_by(BoardConfig.name)
        .all()
    )
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
    rows = db.query(BoardConfig).order_by(BoardConfig.name).all()
    active = [b for b in rows if not b.archived]
    archived = [b for b in rows if b.archived]
    return templates.TemplateResponse(
        request, "boards.html",
        {"boards": active, "archived_boards": archived,
         "msg": request.query_params.get("msg")},
    )


@router.get("/boards/new", response_class=HTMLResponse)
def board_new(request: Request):
    return templates.TemplateResponse(
        request,
        "board_form.html",
        {"board": None, "DistributionType": DistributionType, "BoardStatus": BoardStatus, "env_status": []},
    )


@router.get("/boards/{board_id}/edit", response_class=HTMLResponse)
def board_edit(board_id: str, request: Request, db: Session = Depends(get_db)):
    from app.config import get_settings

    board = db.get(BoardConfig, board_id)
    if board is None:
        return HTMLResponse("Board not found", status_code=404)
    env_status = get_settings().board_env_status(board.credentials_ref) if board.credentials_ref else []
    attempt_count = db.query(PostingAttempt).filter_by(board_id=board.id).count()
    return templates.TemplateResponse(
        request,
        "board_form.html",
        {
            "board": board, "DistributionType": DistributionType, "BoardStatus": BoardStatus,
            "env_status": env_status, "coverage": board.coverage or {},
            "proposals": (board.coverage or {}).get("proposals", []),
            "attempt_count": attempt_count,
        },
    )


@router.post("/boards/{board_id}/proposals/{idx}/{action}")
def board_proposal(board_id: str, idx: int, action: str, db: Session = Depends(get_db)):
    """Approve a proposed new schema field (adds it to the master schema for all
    boards), keep it board-only, or ignore it."""
    from urllib.parse import quote

    from app import schema as S
    from app.models import SchemaExtension

    board = db.get(BoardConfig, board_id)
    if board is None:
        return HTMLResponse("Board not found", status_code=404)
    cov = dict(board.coverage or {})
    proposals = list(cov.get("proposals", []))
    if not (0 <= idx < len(proposals)):
        return RedirectResponse(url=f"/boards/{board_id}/edit", status_code=303)
    p = proposals.pop(idx)
    msg = "Proposal dismissed."
    key = p.get("suggested_key")

    if action == "approve" and key:
        if not db.query(SchemaExtension).filter_by(key=key).one_or_none():
            db.add(SchemaExtension(
                key=key, type=p.get("type", "text"), source="inferred",
                controlled_vocab=bool(p.get("enum")), enum=p.get("enum") or [],
                aliases=[p.get("label", "")], notes=p.get("reason"),
                status="approved", proposed_by_board=board.name,
            ))
            S.apply_extensions()
            msg = f"Added '{key}' to the master schema."
    elif action == "board_only" and key:
        bc = dict(board.board_config or {})
        bc[key] = p.get("label", "")
        board.board_config = bc
        msg = f"Kept '{key}' as a board-only field."

    cov["proposals"] = proposals
    cov["new_field_proposals"] = len(proposals)
    board.coverage = cov
    db.commit()
    return RedirectResponse(url=f"/boards/{board_id}/edit?msg={quote(msg)}", status_code=303)


@router.post("/boards/save")
async def board_save(
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
    name_format: str = Form(default=""),
    form_layout: str = Form(default=""),
    region: str = Form(default=""),
    field: str = Form(default=""),
    login_url: str = Form(default=""),
    login_username: str = Form(default=""),
    login_password: str = Form(default=""),
    login_submit: str = Form(default=""),
    run_automap: str = Form(default="", alias="automap"),
):
    # Duplicate guard: refuse to create (or rename/re-URL) a board that already
    # exists under the same name or the same site URL.
    dup = _find_duplicate(db, name, url, post_url, exclude_id=board_id)
    if dup is not None:
        msg = (
            f"Not saved — '{dup.name}' already exists with this name/URL. "
            f"Open it from the board library and edit it instead of adding a duplicate."
        )
        back = f"/boards/{board_id}/edit" if board_id else "/boards/new"
        return RedirectResponse(url=f"{back}?msg={quote(msg)}", status_code=303)

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
    board.name_format = name_format or None
    board.notes = notes or None
    board.default_for_tags = [t.strip() for t in default_for_tags.split(",") if t.strip()]

    # Region / field are board metadata (shown in the library, drives smart defaults).
    meta = dict(board.meta or {})
    if region:
        meta["region"] = region
    if field:
        meta["field"] = field
    if form_layout:
        meta["form_layout"] = form_layout
    board.meta = meta

    # JSON editors — keep prior value on parse error rather than wiping config.
    try:
        new_field_map = json.loads(field_map or "{}")
    except json.JSONDecodeError:
        new_field_map = dict(board.field_map or {})
    # Login selectors from the form override/augment the field_map's login block
    # (the auth engine + auto-mapper read field_map["login"]). Selectors only —
    # the username/password values live in Railway env vars (credentials_ref).
    if login_username or login_password or login_submit:
        new_field_map["login"] = {
            **({"url": login_url} if login_url else {}),
            "username": login_username, "password": login_password, "submit": login_submit,
        }
    board.field_map = new_field_map
    try:
        board.select_map = json.loads(select_map or "{}")
    except json.JSONDecodeError:
        pass

    db.commit()

    # One-click "Create & auto-map": save, then inspect the live form and fill
    # the field_map automatically so the operator never writes JSON.
    if run_automap and board.post_url:
        try:
            result = await automap.automap_board(board)
        except Exception as exc:  # noqa: BLE001 — surface the reason, never 500
            import traceback
            traceback.print_exc()
            return RedirectResponse(
                f"/boards/{board.id}/edit?msg={quote('Saved. Auto-map failed: ' + str(exc)[:200])}",
                status_code=303,
            )
        saved = automap.apply_result(board, result, db)
        n = automap.real_field_count(result["field_map"])
        if result["bot_challenge"]:
            msg = f"Saved. Bot-challenge detected — flagged for assisted mode ({result['assist_reason']})."
        elif saved and "submit" in result["field_map"]:
            msg = f"Saved and auto-mapped {n} fields + submit button."
        elif saved:
            msg = f"Saved and auto-mapped {n} fields — no submit button found; add one to go live."
        else:
            msg = "Saved, but auto-map found too few fields (form may need login or didn't render)."
        msg += _automap_nav_note(result)
        return RedirectResponse(url=f"/boards/{board.id}/edit?msg={quote(msg)}", status_code=303)

    return RedirectResponse(url="/boards", status_code=303)


@router.post("/boards/{board_id}/delete")
def board_delete(board_id: str, db: Session = Depends(get_db)):
    """Delete a board. Boards with posting history are archived instead (their
    per-job status rows keep rendering); boards never used are removed outright."""
    board = db.get(BoardConfig, board_id)
    if board is None:
        return RedirectResponse(url="/boards", status_code=303)
    name = board.name
    has_history = db.query(PostingAttempt).filter_by(board_id=board.id).count() > 0
    if has_history:
        board.archived = True
        msg = f"'{name}' archived — it has posting history, so it's hidden but not erased. Restore it anytime below."
    else:
        db.delete(board)
        msg = f"'{name}' deleted."
    db.commit()
    return RedirectResponse(url=f"/boards?msg={quote(msg)}", status_code=303)


@router.post("/boards/{board_id}/restore")
def board_restore(board_id: str, db: Session = Depends(get_db)):
    board = db.get(BoardConfig, board_id)
    if board is not None:
        board.archived = False
        db.commit()
    return RedirectResponse(url=f"/boards?msg={quote(board.name + ' restored.') if board else ''}", status_code=303)


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

    try:
        result = await automap.automap_board(board)
    except Exception as exc:  # noqa: BLE001 — surface the reason, never 500
        import traceback
        traceback.print_exc()
        return RedirectResponse(
            f"/boards/{board_id}/edit?msg={quote('Auto-map failed: ' + str(exc)[:200])}",
            status_code=303,
        )
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
    msg += _automap_nav_note(result)
    return RedirectResponse(f"/boards/{board_id}/edit?msg={quote(msg)}", status_code=303)


def _automap_nav_note(result: dict) -> str:
    """Human note about multi-page navigation appended to auto-map messages."""
    note = ""
    if result.get("pages", 1) > 1:
        note += f" Walked a {result['pages']}-page wizard."
    if result.get("nav_blocked"):
        note += f" ⚠ Wizard stopped early — {result['nav_blocked']}."
    return note


# ───────────────────────── screenshot view (R-16) ─────────────────────────


@router.get("/attempts/{attempt_id}/screenshot")
def attempt_screenshot(attempt_id: str, db: Session = Depends(get_db)):
    attempt = db.get(PostingAttempt, attempt_id)
    if attempt is None or not attempt.screenshot_path or not Path(attempt.screenshot_path).exists():
        return HTMLResponse("No screenshot", status_code=404)
    return FileResponse(attempt.screenshot_path, media_type="image/png")
