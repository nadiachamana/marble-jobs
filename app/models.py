"""ORM models: the data-driven heart of the system.

Design principle from the plan: a job board is a *row*, not a function. The
posting engine reads board_config rows and knows what to do, so onboarding a
new board is an INSERT, never a code change.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────── enums ───────────────────────────


class DistributionType(str, enum.Enum):
    playwright_simple = "playwright_simple"
    playwright_auth = "playwright_auth"
    email = "email"


class BoardStatus(str, enum.Enum):
    mapped = "mapped"
    incomplete = "incomplete"
    active = "active"
    paused = "paused"


class JobStatus(str, enum.Enum):
    pending = "pending"        # webhook received, awaiting review
    reviewed = "reviewed"      # Nadia confirmed fields (optional intermediate)
    queued = "queued"          # dispatch clicked, engines about to run
    in_progress = "in_progress"
    completed = "completed"    # all selected boards reached a terminal state
    failed = "failed"          # at least one board failed and none pending


class PostingStatus(str, enum.Enum):
    queued = "queued"
    in_progress = "in_progress"
    success = "success"
    failed = "failed"
    pending_retry = "pending_retry"
    skipped = "skipped"        # e.g. paid board left for manual handling


# ─────────────────────────── board_config ───────────────────────────


class BoardConfig(Base):
    """One row per job board. Adding a board = inserting a row."""

    __tablename__ = "board_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1024))
    post_url: Mapped[str | None] = mapped_column(String(1024))
    distribution_type: Mapped[DistributionType] = mapped_column(
        Enum(DistributionType), nullable=False
    )
    status: Mapped[BoardStatus] = mapped_column(
        Enum(BoardStatus), default=BoardStatus.mapped, nullable=False
    )
    platform_group: Mapped[str | None] = mapped_column(String(64))

    # Credentials are referenced, never stored. The ref keys env vars / secrets.
    credentials_ref: Mapped[str | None] = mapped_column(String(128))

    # Email-type boards
    contact_email: Mapped[str | None] = mapped_column(String(255))

    # UTM tracking
    utm_source: Mapped[str | None] = mapped_column(String(128))
    ashby_tracker_url: Mapped[str | None] = mapped_column(String(1024))

    # The two JSONB maps that make onboarding code-free.
    # field_map: master field -> board form selector/name
    field_map: Mapped[dict] = mapped_column(JSON, default=dict)
    # select_map: standard option value -> board-specific dropdown value
    select_map: Mapped[dict] = mapped_column(JSON, default=dict)

    # Smart-default selection hints
    default_for_tags: Mapped[list] = mapped_column(JSON, default=list)
    default_for_depts: Mapped[list] = mapped_column(JSON, default=list)

    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    # Board needs a human-in-the-loop step (CAPTCHA / Cloudflare / bot-challenge).
    # Auto-dispatch skips these with a pointer to assisted mode; never bypassed.
    requires_assist: Mapped[bool] = mapped_column(Boolean, default=False)
    assist_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    # Extra board metadata from the source CSV (region, type, category,
    # eligibility, seniority_target, field). Drives the board library UI and
    # smart-default selection without needing dedicated columns per attribute.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    # Raw human-readable form map from the source CSV, kept so an operator can
    # build field_map selectors later via the onboarding UI.
    raw_map: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list["PostingAttempt"]] = relationship(back_populates="board")


# ─────────────────────────── job_queue ───────────────────────────


class JobQueue(Base):
    """Each Ashby job posting, from webhook through review, dispatch, completion."""

    __tablename__ = "job_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ashby_job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ashby_job_posting_id: Mapped[str | None] = mapped_column(String(64), index=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description_html: Mapped[str | None] = mapped_column(Text)
    description_plain: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(String(1024))  # base, UTM resolved per board

    location_country: Mapped[str | None] = mapped_column(String(128))
    location_city: Mapped[str | None] = mapped_column(String(128))
    employment_type: Mapped[str | None] = mapped_column(String(64))
    work_mode: Mapped[str | None] = mapped_column(String(64))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Inferred / editable fields
    seniority: Mapped[str | None] = mapped_column(String(64))
    function_category: Mapped[str | None] = mapped_column(String(128))
    industry_tags: Mapped[list] = mapped_column(JSON, default=list)
    department: Mapped[str | None] = mapped_column(String(128))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))

    selected_board_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.pending, nullable=False, index=True
    )

    # Timestamp ("ts") of the parent Slack message, so every status update for
    # this job is posted as a reply in the same thread instead of the channel.
    slack_ts: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list["PostingAttempt"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


# ─────────────────────────── posting_attempt ───────────────────────────


class PostingAttempt(Base):
    """One (job × board) posting attempt, tracked live in the dashboard."""

    __tablename__ = "posting_attempt"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_queue.id"), index=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("board_config.id"), index=True)

    status: Mapped[PostingStatus] = mapped_column(
        Enum(PostingStatus), default=PostingStatus.queued, nullable=False
    )
    resolved_apply_url: Mapped[str | None] = mapped_column(String(1024))
    result_url: Mapped[str | None] = mapped_column(String(1024))  # live posting URL, if returned
    error_message: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(1024))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped["JobQueue"] = relationship(back_populates="attempts")
    board: Mapped["BoardConfig"] = relationship(back_populates="attempts")
