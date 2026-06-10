"""Ashby integration: webhook signature verification + jobPosting.info fetch.

Auth model: Ashby uses HTTP Basic auth with the API key as the username and an
empty password. Webhooks are signed with HMAC-SHA256 over the raw request body
using the shared secret configured on the webhook.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()

ASHBY_API_BASE = "https://api.ashbyhq.com"


# ───────────────────────── webhook verification ─────────────────────────


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verify the Ashby-Signature header against the raw body.

    Accepts both bare hex digests and the `sha256=<hex>` form. If no secret is
    configured (local dev before secrets are added), verification is skipped so
    the pipeline can be exercised with test payloads.
    """
    secret = settings.ashby_webhook_secret
    if not secret:
        return True  # dev mode: no secret set yet
    if not signature_header:
        return False

    provided = signature_header.split("=", 1)[-1].strip()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


# ───────────────────────── job data fetch ─────────────────────────


async def fetch_job_posting(job_posting_id: str) -> dict[str, Any]:
    """Call jobPosting.info and return the `results` object.

    Raises httpx.HTTPError on transport failure or a RuntimeError if Ashby
    reports success: false.
    """
    if not settings.ashby_api_key:
        raise RuntimeError("ASHBY_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{ASHBY_API_BASE}/jobPosting.info",
            auth=(settings.ashby_api_key, ""),
            json={"jobPostingId": job_posting_id},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"Ashby jobPosting.info returned success=false: {data}")
    return data["results"]


# ───────────────────────── payload parsing ─────────────────────────


def _parse_deadline(value: Any) -> datetime | None:
    """applicationDeadline may be an ISO string or a nested object."""
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("deadline")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_webhook(payload: dict) -> dict:
    """Extract the bits we need from a jobPostingPublish webhook body."""
    posting = payload.get("data", {}).get("jobPosting", {})
    return {
        "ashby_job_posting_id": posting.get("id"),
        "ashby_job_id": posting.get("jobId"),
        "title": posting.get("title"),
        "location_country": posting.get("locationName"),
        "employment_type": posting.get("employmentType"),
        "work_mode": posting.get("workplaceType"),
        "apply_url": posting.get("externalLink"),
        "deadline": _parse_deadline(posting.get("applicationDeadline")),
        "action": payload.get("action"),
    }


def parse_job_info(results: dict) -> dict:
    """Normalise a jobPosting.info `results` object into JobQueue fields.

    The webhook is lightweight; jobPosting.info is the source of truth for the
    description, department, work mode, and a reliably-typed deadline.
    """
    work_mode = results.get("workplaceType")
    if not work_mode and results.get("isRemote"):
        work_mode = "Remote"

    return {
        "title": results.get("title"),
        "description_html": results.get("descriptionHtml"),
        "description_plain": results.get("descriptionPlain"),
        "apply_url": results.get("applyLink") or results.get("externalLink"),
        "location_country": results.get("locationName"),
        "employment_type": results.get("employmentType"),
        "work_mode": work_mode,
        "department": results.get("departmentName"),
        "deadline": _parse_deadline(results.get("applicationDeadline")),
        "is_remote": bool(results.get("isRemote")),
    }
