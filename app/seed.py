"""Seed board_config from the source CSV (Phase 1 deliverable).

Run:  python -m app.seed [path/to/job-boards-airtable.csv]

Idempotent: upserts by board name. Credentials found in the CSV are written to
secrets/board_credentials.env (gitignored) keyed by credentials_ref — they are
never written into the database, per the plan's vault requirement (R-13).
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.db import SessionLocal, init_db
from app.inference import _TAG_SYNONYMS
from app.models import BoardConfig, BoardStatus, DistributionType

# Prefer the full local CSV (has Username/Password to regenerate the secrets
# file); fall back to the sanitized, committed seed used in deployed envs.
DEFAULT_CSV = "job-boards-airtable.csv" if Path("job-boards-airtable.csv").exists() else "boards_seed.csv"
CRED_FILE = Path("secrets/board_credentials.env")

# CSV "Distribution" text -> our enum.
_DISTRIBUTION_MAP = {
    "playwright (simple)": DistributionType.playwright_simple,
    "playwright (authentication)": DistributionType.playwright_auth,
    "playwright (auth)": DistributionType.playwright_auth,
    "email": DistributionType.email,
}

_STATUS_MAP = {
    "mapped": BoardStatus.mapped,
    "incomplete": BoardStatus.incomplete,
    "active": BoardStatus.active,
    "paused": BoardStatus.paused,
}


def _utm_from_url(url: str | None) -> str | None:
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("utm_source")
    return vals[0] if vals else None


def _credref_key(credentials_ref: str) -> str:
    return credentials_ref.upper().replace("-", "_").replace(" ", "_")


def _extract_tags(*texts: str | None) -> list[str]:
    """Find canonical climate tags mentioned anywhere in the given CSV cells."""
    found: list[str] = []
    blob = " ".join(t.lower() for t in texts if t)
    for needle, canonical in _TAG_SYNONYMS.items():
        if needle in blob and canonical not in found:
            found.append(canonical)
    return found


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def seed_from_csv(csv_path: str = DEFAULT_CSV) -> tuple[int, int]:
    init_db()
    rows_seen = 0
    creds: list[str] = []

    # utf-8-sig strips a leading BOM if Airtable exported one.
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        session = SessionLocal()
        try:
            for row in reader:
                name = _clean(row.get("Name"))
                if not name:
                    continue
                rows_seen += 1

                distribution_raw = (row.get("Distribution") or "").strip().lower()
                distribution = _DISTRIBUTION_MAP.get(distribution_raw)
                if distribution is None:
                    # Unknown/blank distribution -> default to email so the row is
                    # still onboarded and flagged for an operator to fix.
                    distribution = DistributionType.email

                status_raw = (row.get("Automation Status") or "").strip().lower()
                status = _STATUS_MAP.get(status_raw, BoardStatus.mapped)

                tracker = _clean(row.get("Ashby Tracker"))
                credentials_ref = _clean(row.get("Code-tracker"))
                eligibility = (row.get("Eligibility") or "").strip().lower()

                meta = {
                    "tier": _clean(row.get("Tier")),
                    "eligibility": _clean(row.get("Eligibility")),
                    "type": _clean(row.get("Type")),
                    "region": _clean(row.get("Region")),
                    "category": _clean(row.get("Category")),
                    "seniority_target": _clean(row.get("Seniority Target")),
                    "field": _clean(row.get("Field")),
                }

                values = dict(
                    name=name,
                    url=_clean(row.get("URL")),
                    post_url=_clean(row.get("URL to post")),
                    distribution_type=distribution,
                    status=status,
                    credentials_ref=credentials_ref,
                    contact_email=_clean(row.get("Contact Email")),
                    utm_source=_utm_from_url(tracker),
                    ashby_tracker_url=tracker,
                    is_paid=(eligibility == "paid"),
                    notes=_clean(row.get("Notes")),
                    raw_map=_clean(row.get("Map")),
                    default_for_tags=_extract_tags(row.get("Field"), row.get("Posted Positions")),
                    meta=meta,
                )

                # Apply any derived field_map / select_map overlay for this board.
                from app.field_maps import BOT_PROTECTED, FIELD_MAPS, SELECT_MAPS

                if name in FIELD_MAPS:
                    values["field_map"] = FIELD_MAPS[name]
                if name in SELECT_MAPS:
                    values["select_map"] = SELECT_MAPS[name]
                if name in BOT_PROTECTED:
                    values["requires_assist"] = True
                    values["assist_reason"] = BOT_PROTECTED[name]

                existing = session.query(BoardConfig).filter_by(name=name).one_or_none()
                if existing:
                    for key, val in values.items():
                        # Don't wipe a field_map edited in the UI unless we have one to apply.
                        if key in ("field_map", "select_map") and not val:
                            continue
                        setattr(existing, key, val)
                else:
                    session.add(BoardConfig(**values))

                # Stash credentials for the secrets file, never the DB.
                username = _clean(row.get("Username"))
                password = _clean(row.get("Password"))
                if credentials_ref and (username or password):
                    key = _credref_key(credentials_ref)
                    creds.append(f"# {name}")
                    creds.append(f"BOARD_{key}_USERNAME={username or ''}")
                    creds.append(f"BOARD_{key}_PASSWORD={password or ''}")
                    creds.append("")

            session.commit()
        finally:
            session.close()

    if creds:
        CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Auto-generated by app/seed.py from the board CSV. Gitignored.\n"
            "# Loaded into the environment at startup by app/config.py.\n\n"
        )
        CRED_FILE.write_text(header + "\n".join(creds) + "\n")

    return rows_seen, len(creds)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    seen, _ = seed_from_csv(path)
    print(f"Seeded/updated {seen} boards from {path}")
    if CRED_FILE.exists():
        print(f"Wrote board credentials to {CRED_FILE} (gitignored)")
