"""Database engine and session management.

A single SQLAlchemy layer serves both environments:
  - local dev  -> SQLite (zero setup)
  - production -> Railway-managed Postgres (set DATABASE_URL)

JSON columns use SQLAlchemy's generic JSON type, which maps to JSONB on
Postgres and to TEXT-encoded JSON on SQLite, so the same models work on both.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# Ensure the SQLite file's directory exists before the engine connects.
if settings.is_sqlite:
    from pathlib import Path

    db_path = settings.database_url.split("///", 1)[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# SQLite needs check_same_thread off for FastAPI's threadpool; Postgres ignores it.
connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.normalized_database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the initial schema. create_all() won't ALTER existing
# tables, so we add any missing ones idempotently (dev SQLite; fresh Postgres
# on Railway gets them from create_all directly).
_ADDED_COLUMNS = {
    "job_queue": {"slack_ts": "VARCHAR(32)", "canonical": "JSON"},
    "board_config": {
        "requires_assist": "BOOLEAN", "assist_reason": "VARCHAR(255)",
        # v2 schema columns
        "required_fields": "JSON", "board_config": "JSON", "name_format": "VARCHAR(16)",
        "field_notes": "JSON", "coverage": "JSON", "schema_version": "INTEGER",
        # Soft-delete flag. Pre-existing rows get NULL, so queries must filter
        # with archived IS NOT TRUE (isnot(True)), never archived == False.
        "archived": "BOOLEAN",
    },
}


def _add_missing_columns() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table)}
            for col, coltype in columns.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))


def init_db() -> None:
    """Create tables if they do not exist. Models must be imported first."""
    from app import models  # noqa: F401  (ensures models register on Base.metadata)

    backend = "SQLite" if settings.is_sqlite else "Postgres"
    if settings.is_sqlite and not settings.database_url.endswith("marble.db"):
        print(f"[db] using {settings.database_url}")
    print(f"[db] connecting to {backend}…")
    if settings.is_sqlite:
        print("[db] ⚠ SQLite is ephemeral on Railway — set DATABASE_URL to your "
              "Postgres connection string for persistent storage.")
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    print("[db] schema ready")
