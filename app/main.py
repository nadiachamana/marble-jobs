"""FastAPI application entrypoint.

Serves the webhook receiver and the review dashboard from a single app, as the
plan specifies (FastAPI + Jinja2, no separate frontend). Run locally with:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import SessionLocal, init_db
from app.routes import dashboard, dispatch, webhook

app = FastAPI(title="Marble Job Board Automation")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    for d in ("data", "data/screenshots", "data/attachments", "static"):
        Path(d).mkdir(parents=True, exist_ok=True)

    # First boot on a fresh database (e.g. Railway Postgres): seed the boards so
    # the app comes up fully configured without a manual seed step. Idempotent —
    # only runs when board_config is empty.
    try:
        from app.models import BoardConfig
        from app.seed import seed_from_csv

        with SessionLocal() as session:
            if session.query(BoardConfig).count() == 0:
                seed_from_csv()
    except Exception as exc:  # noqa: BLE001 — never block startup on seeding
        print(f"[startup] board auto-seed skipped: {exc}")


# Static assets (CSS).
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(webhook.router)
app.include_router(dashboard.router)
app.include_router(dispatch.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    # Production entrypoint (Docker/Railway): read PORT from the environment in
    # Python so we never depend on shell `$PORT` expansion, which fails when the
    # platform runs the start command without a shell.
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
