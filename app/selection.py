"""Smart-default board selection for the review form (R-07).

Pre-checks boards likely to be right for a job so Nadia confirms in ~1-2 min:
  - tag overlap: the board's default_for_tags intersect the job's industry_tags
  - broad reach: a free, worldwide, generalist board is a safe default for any job

Paid boards and incomplete boards are never pre-checked (paid needs a payment
decision; incomplete lacks a usable field_map). They still appear, unchecked.
"""

from __future__ import annotations

from app.models import BoardConfig, BoardStatus, JobQueue


def _is_broad(board: BoardConfig) -> bool:
    meta = board.meta or {}
    region = (meta.get("region") or "").lower()
    field = (meta.get("field") or "").lower()
    broad_region = region in ("worldwide", "") or "europe" in region
    broad_field = field in ("", "generalist") or "generalist" in field
    return broad_region and broad_field


def default_checked_ids(job: JobQueue, boards: list[BoardConfig]) -> set[str]:
    job_tags = {t.lower() for t in (job.industry_tags or [])}
    chosen: set[str] = set()
    for board in boards:
        if board.is_paid:
            continue
        if board.status == BoardStatus.incomplete:
            continue
        board_tags = {t.lower() for t in (board.default_for_tags or [])}
        if job_tags & board_tags:
            chosen.add(board.id)
        elif not job_tags and _is_broad(board):
            chosen.add(board.id)
    return chosen
