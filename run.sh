#!/usr/bin/env bash
# Start the Marble Job Board dashboard locally.
# Usage:  ./run.sh         (then open http://127.0.0.1:8000)
# Stop:   Ctrl-C in this terminal.
set -e
cd "$(dirname "$0")"

# Avoid the macOS system-proxy misconfiguration that blocks outbound calls.
export HTTPS_PROXY="" HTTP_PROXY="" ALL_PROXY="" no_proxy="*"

# Seed boards on first run if the DB is empty (safe/idempotent otherwise).
.venv/bin/python -c "from app.db import SessionLocal; from app.models import BoardConfig; import sys; sys.exit(0 if SessionLocal().query(BoardConfig).count() else 1)" \
  || .venv/bin/python -m app.seed

echo ""
echo "  Dashboard starting →  http://127.0.0.1:8000"
echo "  Press Ctrl-C to stop."
echo ""
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
