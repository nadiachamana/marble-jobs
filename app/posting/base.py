"""Shared posting primitives: the master field schema and form-fill helpers.

The master field schema (Section 4 of the plan) is the canonical set of values
every job produces. Boards consume a subset. A board's `field_map` maps these
master field names onto its own form, so the engines never hardcode a board.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCREENSHOT_DIR = Path("data/screenshots")
ATTACHMENT_DIR = Path("data/attachments")

# Per-field fill timeout. Short so a wrong/missing selector is skipped quickly
# instead of stalling the whole posting on Playwright's 30s default.
FILL_TIMEOUT_MS = 8000


@dataclass
class PostResult:
    """Outcome of a single board posting attempt."""

    success: bool
    result_url: str | None = None        # live posting URL, if the board returns one
    screenshot_path: str | None = None   # captured on failure for debugging (R-16)
    error: str | None = None
    detail: str | None = None            # freeform note (e.g. fields filled)

# Static Marble boilerplate used as the "company description" on every board.
MARBLE_BOILERPLATE = (
    "Marble is a climate tech venture studio. We partner with scientists, "
    "engineers, and operators to create companies solving hard climate problems "
    "in the world's largest industries. Our first 14 companies are building "
    "transformative products across energy, industry, agriculture, and climate "
    "resilience. Learn more at https://marble.studio."
)


def build_master_fields(job, board, resolved_apply_url: str | None) -> dict[str, Any]:
    """Assemble the canonical field set a board can draw from.

    `resolved_apply_url` is the per-board UTM-tracked apply URL (see utm.py);
    falls back to the job's base apply URL.
    """
    from app.config import get_settings
    from app.inference import _EMPLOYMENT_NORM, _WORKMODE_NORM

    settings = get_settings()
    tags = job.industry_tags or []
    emp = job.employment_type or "Full-time"
    emp = _EMPLOYMENT_NORM.get(emp.lower(), emp)
    work_mode = job.work_mode or "Hybrid"
    work_mode = _WORKMODE_NORM.get(work_mode.lower(), work_mode)

    salary = None
    if job.salary_min:
        currency = job.salary_currency or ""
        salary = (
            f"{currency} {job.salary_min}"
            if job.salary_min == job.salary_max
            else f"{currency} {job.salary_min}–{job.salary_max}"
        ).strip()

    fields = {
        "title": job.title,
        "description_html": job.description_html or "",
        "description_plain": job.description_plain or "",
        "company_description": MARBLE_BOILERPLATE,
        "company_name": "Marble",
        "organization": "Marble",  # alias many boards use for the employer name
        "apply_url": resolved_apply_url or job.apply_url or "",
        "work_mode": work_mode,
        "country": job.location_country or "",
        "city": job.location_city or "",
        # Boards almost always require employment type / contract type — default
        # to Full-time (Marble co-founder/residency roles) when Ashby is silent.
        "employment_type": emp,
        "deadline": job.deadline.date().isoformat() if job.deadline else "",
        "contact_email": settings.marble_contact_email,
        "contact_name": settings.marble_contact_name,
        "contact_first_name": settings.marble_contact_name.split(" ", 1)[0],
        "contact_last_name": (settings.marble_contact_name.split(" ", 1) + [""])[1],
        "contact_phone": settings.marble_contact_phone,
        "phone": settings.marble_contact_phone,
        "seniority": job.seniority or "",
        "function_category": job.function_category or "",
        "category": job.function_category or "",  # alias boards use for job category
        "industry_tags": ", ".join(tags),
        "sector": ", ".join(tags) or (job.function_category or ""),
        "salary": salary or "",
        "salary_min": str(job.salary_min) if job.salary_min else "",
        "salary_max": str(job.salary_max) if job.salary_max else "",
        "salary_currency": job.salary_currency or "",
        # Sensible constant defaults for common board questions.
        "compensated": "Yes",
        "start_date": "As soon as possible",
        "listing_type": "Free",
    }

    # contact.full_name ordering varies per board (KTH "Nadia Chamana" vs FR
    # "Chamana Nadia") — honour the board's name_format flag.
    if getattr(board, "name_format", None) == "last_first":
        fields["contact_name"] = f"{fields['contact_last_name']} {fields['contact_first_name']}".strip()

    # ── v2: expose every value under canonical dotted keys too ──
    # 1) the job's stored canonical payload (rich inferred fields like headline,
    #    seo, role_functions) for any key not already provided;
    # 2) flat→canonical aliases so a field_map keyed by canonical OR legacy works.
    from app.schema import LEGACY_ALIASES

    for flat, canon in LEGACY_ALIASES.items():
        if flat in fields and canon not in fields:
            fields[canon] = fields[flat]
    for k, v in (getattr(job, "canonical", None) or {}).items():
        fields.setdefault(k, "" if v is None else (", ".join(map(str, v)) if isinstance(v, list) else str(v)))
    return fields


def translate_value(master_field: str, value: str, select_map: dict) -> str:
    """Apply a board's select_map to translate a standard value (R-17).

    select_map shape: { "<master_field>": { "<standard value>": "<board value>" } }
    or a flat { "<standard value>": "<board value>" } applied to any field.
    """
    if not select_map:
        return value
    field_map = select_map.get(master_field)
    if isinstance(field_map, dict) and value in field_map:
        return field_map[value]
    if value in select_map and isinstance(select_map[value], str):
        return select_map[value]
    return value


async def fill_form(page, field_map: dict, select_map: dict, fields: dict[str, Any]) -> list[str]:
    """Fill a form from a board's field_map. Returns the list of filled fields.

    Each field_map entry is either:
      "master_field": "<css selector>"                      -> type text
      "master_field": {"selector": "...", "type": "select"} -> dropdown
      types: fill (default) | select | check | click | richtext

    `richtext` fills a rich-text editor (TinyMCE/CKEditor) whose content lives in
    a contenteditable iframe body, not the hidden backing textarea.

    Unknown master fields or empty values are skipped silently so a partial
    field_map still posts what it can.
    """
    filled: list[str] = []
    failed: list[str] = []
    for master_field, spec in field_map.items():
        value = fields.get(master_field)
        if value in (None, ""):
            continue
        selector = spec if isinstance(spec, str) else spec.get("selector")
        kind = "fill" if isinstance(spec, str) else spec.get("type", "fill")
        if not selector:
            continue

        value = translate_value(master_field, str(value), select_map)
        try:
            if kind == "select":
                await page.select_option(selector, label=value, timeout=FILL_TIMEOUT_MS)
            elif kind == "check":
                await page.check(selector, timeout=FILL_TIMEOUT_MS)
            elif kind == "click":
                await page.click(selector, timeout=FILL_TIMEOUT_MS)
            elif kind == "richtext":
                if "ifr" in selector or "iframe" in selector.lower():
                    await page.frame_locator(selector).locator("body").fill(value, timeout=FILL_TIMEOUT_MS)
                else:  # plain contenteditable element
                    await page.locator(selector).fill(value, timeout=FILL_TIMEOUT_MS)
            elif kind == "react_select":
                # react-select combobox: focus, type to filter, pick the match.
                await page.click(selector, timeout=FILL_TIMEOUT_MS)
                await page.fill(selector, value, timeout=FILL_TIMEOUT_MS)
                await page.wait_for_timeout(500)
                await page.keyboard.press("Enter")
            else:
                await page.fill(selector, value, timeout=FILL_TIMEOUT_MS)
            filled.append(master_field)
        except Exception:  # noqa: BLE001 — collect and keep going; don't abort the whole post
            failed.append(f"{master_field} ({selector})")
    if failed:
        # Visible to the engine via the detail string / logs, but never aborts.
        print(f"[fill_form] could not fill: {', '.join(failed)}")
    return filled
