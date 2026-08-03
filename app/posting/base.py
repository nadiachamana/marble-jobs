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
    held: bool = False                   # hold mode: filled everything, stopped before final submit


async def capture_screenshot(page, attempt_id: str) -> str | None:
    """Full-page screenshot into data/screenshots; never raises."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{attempt_id}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:  # noqa: BLE001
        return None

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

    # Safety-net defaults for keys boards commonly require. Marble always
    # applies via URL, so an "apply online / by URL" choice must have a value —
    # without it the checkbox is never ticked and the (revealed) URL box stays
    # hidden, which is exactly how the first Imperial test lost apply.url.
    for key, default in (("apply.channel", "URL"), ("job.number_of_positions", "1")):
        if fields.get(key) in (None, ""):
            fields[key] = default
    return fields


# ─────────────────── live-posting URL capture (post-submit) ───────────────────
# After a confirmed submit, boards usually show a "View job" / "See job post"
# style link to the live posting. We capture it so the dashboard's "Live post"
# column fills itself instead of Nadia hunting for the URL.

_RESULT_LINK_WORDS = (
    "view job", "view your job", "see job post", "see your job", "view posting",
    "view the posting", "view listing", "view vacancy", "see the post",
    "view post", "your job post", "see listing", "voir l'annonce", "voir l'offre",
    "voir votre annonce",
)
# Path fragments that make a URL look like a job-detail page.
_JOB_PATH_HINTS = ("/job/", "/jobs/", "/vacancy/", "/vacancies/", "/offre/",
                   "/offres/", "/position/", "/listing/", "/posting/")

_ANCHOR_EVAL = (
    "els => els.map(e => ({text: (e.textContent||'').trim().slice(0,80), href: e.href}))"
    ".filter(a => a.href && a.href.startsWith('http'))"
)


def _slug_words(title: str) -> list[str]:
    """Distinctive lowercase words of a job title, for spotting its slug in a URL."""
    import re

    words = re.findall(r"[a-z0-9]{4,}", (title or "").lower())
    return [w for w in words if w not in ("with", "from", "will", "your")][:4]


def _board_host(board) -> str:
    from urllib.parse import urlsplit

    for u in (board.post_url, board.url):
        if u:
            return urlsplit(u).netloc.removeprefix("www.").lower()
    return ""


def _looks_like_job_url(href: str, title: str) -> bool:
    h = href.lower()
    slugged = sum(1 for w in _slug_words(title) if w in h)
    return any(p in h for p in _JOB_PATH_HINTS) or slugged >= 2


def pick_result_url(anchors: list[dict], board, job_title: str, page_url: str = "") -> str | None:
    """Choose the live-posting link from a confirmation page's anchors.

    Pure scoring (no browser) so the async engines, the sync assist tool, and
    tests all share it. `anchors` items: {"text": ..., "href": ...}.
    """
    host = _board_host(board)
    best, best_score = None, 0
    for a in anchors:
        href, text = a.get("href") or "", (a.get("text") or "").lower()
        if not href:
            continue
        from urllib.parse import urlsplit

        if host and host not in urlsplit(href).netloc.removeprefix("www.").lower():
            continue  # only links on the board's own site can be the posting
        score = 0
        if any(w in text for w in _RESULT_LINK_WORDS):
            score += 100
        if any(p in href.lower() for p in _JOB_PATH_HINTS):
            score += 30
        score += 25 * sum(1 for w in _slug_words(job_title) if w in href.lower())
        # A keyword-labelled link, or an unambiguous job-detail URL, qualifies.
        if score > best_score and score >= 75:
            best, best_score = href, score
    if best:
        return best
    # The confirmation may have redirected straight onto the live posting.
    if page_url and host:
        from urllib.parse import urlsplit

        if host in urlsplit(page_url).netloc.removeprefix("www.").lower() and _looks_like_job_url(page_url, job_title):
            return page_url
    return None


def _pick_result_url_claude(anchors: list[dict], job_title: str) -> str | None:
    """Last-resort: ask Claude which confirmation-page link is the live posting."""
    import json

    from app import llm

    if not llm.available() or not anchors:
        return None
    try:
        out = llm.complete_json(
            "A job was just submitted to a job board. From the links on the "
            "confirmation page, identify the URL of the live job posting itself "
            "(the public page showing this job). Return {\"result_url\": \"<url>\"} "
            "or {\"result_url\": null} if none of the links is the posting.",
            json.dumps({"job_title": job_title, "links": anchors[:40]}, ensure_ascii=False),
            max_tokens=300,
        )
        url = out.get("result_url")
        return url if isinstance(url, str) and url.startswith("http") else None
    except Exception:  # noqa: BLE001 — capture is best-effort, never fail the post
        return None


async def find_result_url(page, board, job_title: str, result_url_selector: str | None = None) -> str | None:
    """Async (engine) capture: explicit selector → anchor scan → page URL → Claude."""
    if result_url_selector:
        try:
            href = await page.get_attribute(result_url_selector, "href")
            if href:
                return href
        except Exception:  # noqa: BLE001
            pass
    try:
        anchors = await page.eval_on_selector_all("a[href]", _ANCHOR_EVAL)
    except Exception:  # noqa: BLE001
        anchors = []
    return (
        pick_result_url(anchors, board, job_title, page.url)
        or _pick_result_url_claude(anchors, job_title)
    )


def find_result_url_sync(page, board, job_title: str) -> str | None:
    """Sync capture for assisted mode (playwright.sync_api page)."""
    try:
        anchors = page.eval_on_selector_all("a[href]", _ANCHOR_EVAL)
    except Exception:  # noqa: BLE001
        anchors = []
    return (
        pick_result_url(anchors, board, job_title, page.url)
        or _pick_result_url_claude(anchors, job_title)
    )


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
        kind = "fill" if isinstance(spec, str) else spec.get("type", "fill")
        if kind == "date_parts":
            # A date split across day/month/year <select>s (spec holds one
            # selector per part) — no single "selector" key to check.
            if await _fill_date_parts(page, spec, str(value)):
                filled.append(master_field)
            else:
                failed.append(f"{master_field} (date parts)")
            continue
        selector = spec if isinstance(spec, str) else spec.get("selector")
        if not selector:
            continue

        value = translate_value(master_field, str(value), select_map)
        try:
            if kind == "select":
                await page.select_option(selector, label=value, timeout=FILL_TIMEOUT_MS)
            elif kind == "check":
                await page.check(selector, timeout=FILL_TIMEOUT_MS)
            elif kind == "radio":
                # The value picks which option of the group; when the selector
                # matches several radios, prefer the one whose value= matches.
                loc = page.locator(selector)
                if await loc.count() > 1:
                    target = page.locator(f"{selector}[value='{value}' i]")
                    if await target.count():
                        await target.first.check(timeout=FILL_TIMEOUT_MS)
                    else:
                        await loc.first.check(timeout=FILL_TIMEOUT_MS)
                else:
                    await loc.first.check(timeout=FILL_TIMEOUT_MS)
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


async def _fill_date_parts(page, spec: dict, value: str) -> bool:
    """Fill a day/month/year select trio from an ISO date, tolerating the
    board's label style ('03' vs '3', 'Aug' vs 'August' vs '08')."""
    from datetime import date

    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return False
    candidates = {
        "day": [f"{d.day:02d}", str(d.day)],
        "month": [d.strftime("%b"), d.strftime("%B"), f"{d.month:02d}", str(d.month)],
        "year": [str(d.year)],
    }
    ok = True
    for part, cands in candidates.items():
        sel = spec.get(part)
        if not sel:
            continue
        done = False
        for cand in cands:
            for by in ("label", "value"):
                try:
                    await page.select_option(sel, **{by: cand}, timeout=2000)
                    done = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if done:
                break
        ok = ok and done
    return ok


# ─────────────────── multi-page form flow (wizard posting) ───────────────────


async def navigate_post_entry(page, post_link_selector: str) -> bool:
    """Click the saved 'Add new vacancy' style link that leads from a landing /
    jobs-list page to the posting form. No-op if the link isn't on this page
    (we may already be on the form)."""
    try:
        loc = page.locator(post_link_selector).first
        if await loc.count():
            await loc.click(timeout=FILL_TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _group_by_page(field_map: dict) -> dict[int, dict]:
    """Split a field_map by wizard page ('page' in the spec; default 1)."""
    pages: dict[int, dict] = {}
    for key, spec in field_map.items():
        p = spec.get("page", 1) if isinstance(spec, dict) else 1
        pages.setdefault(p, {})[key] = spec
    return pages


async def run_form_flow(page, board, fields: dict, attempt_id: str,
                        job_title: str = "", hold: bool = False) -> PostResult:
    """Fill and submit a board's posting form — single page or multi-page wizard.

    Expects `page` to already be at the board's post URL, logged in if needed.
    Handles: nav.post_link (landing page → form), page-by-page fill with
    nav.next clicks, the final submit (nav.final_submit or the classic
    field_map['submit']), success check, and live-post URL capture.

    hold=True (local testing): fill everything, stop BEFORE the final submit,
    and return held=True so the caller can hand over to a human.
    """
    field_map = dict(board.field_map or {})
    field_map.pop("login", None)
    nav = dict(field_map.pop("nav", None) or {})
    submit = field_map.pop("submit", None)
    success_selector = field_map.pop("_success_selector", None)
    result_url_selector = field_map.pop("_result_url_selector", None)

    if not field_map:
        return PostResult(
            success=False,
            error="Board has no field_map yet. Add selectors via the board "
            "onboarding UI before dispatching to this board.",
        )

    # Landing page → posting form (e.g. targetconnect's "Add new vacancy").
    if nav.get("post_link"):
        await navigate_post_entry(page, nav["post_link"])

    pages_map = _group_by_page(field_map)
    # nav.pages can exceed the mapped max — e.g. a final review/confirm step
    # with no fillable fields still needs its Next click to be reached.
    n_pages = max(max(pages_map.keys(), default=1), int(nav.get("pages") or 1))
    select_map = board.select_map or {}

    from app.automap import _advanced, _headings, _page_signature
    from app.posting.smartfill import smart_fill

    async def _step_state(page):
        try:
            raw = await page.eval_on_selector_all("input, textarea, select",
                "els => els.filter(e => e.getClientRects().length).map(e => ({tag: e.tagName.toLowerCase(), id: e.id||'', name: e.getAttribute('name')||'', label: ''}))")
        except Exception:  # noqa: BLE001
            raw = []
        return _page_signature(raw, await _headings(page))

    filled_all: list[str] = []
    for p in range(1, n_pages + 1):
        page_specs = pages_map.get(p, {})
        filled = await fill_form(page, page_specs, select_map, fields)
        filled_all.extend(filled)

        # AI gap pass (prompt-to-post): whatever the mechanical fill left empty
        # — unmatchable select labels, capped dates, fields the mapping never
        # knew — gets one Claude look at the LIVE page + the job data.
        ai_filled = await smart_fill(page, fields)
        filled_all.extend(f"{name} (AI)" for name in ai_filled)

        # If a later page's fields all failed to fill, the Next click probably
        # didn't advance (board-side validation) — fail loudly with a screenshot.
        attempted = [k for k in page_specs if fields.get(k) not in (None, "")]
        if p > 1 and attempted and not filled and not ai_filled:
            shot = await capture_screenshot(page, attempt_id)
            return PostResult(
                success=False,
                error=f"Wizard page {p}/{n_pages}: none of its fields could be filled — "
                f"the previous 'Next' likely didn't advance (validation error on page {p-1}).",
                screenshot_path=shot,
                detail=f"Filled: {', '.join(filled_all)}",
            )

        if p < n_pages:
            next_sel = nav.get("next")
            if not next_sel:
                shot = await capture_screenshot(page, attempt_id)
                return PostResult(
                    success=False,
                    error=f"Multi-page form (page {p}/{n_pages}) but no 'next' button saved "
                    "in field_map.nav — re-run Auto-map.",
                    screenshot_path=shot,
                    detail=f"Filled: {', '.join(filled_all)}",
                )
            # Click Next and VERIFY the wizard advanced. If the board rejected
            # the page, its error text is now on screen — run the AI pass again
            # (it reads the errors, e.g. a max-closing-date rule) and retry.
            advanced = False
            for attempt_no in range(3):
                before = await _step_state(page)
                await page.locator(next_sel).first.click(timeout=FILL_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001 — SPA wizards don't navigate
                    pass
                await page.wait_for_timeout(2500)
                if _advanced(before, await _step_state(page)):
                    advanced = True
                    break
                if attempt_no < 2:
                    fixes = await smart_fill(page, fields)
                    filled_all.extend(f"{name} (AI fix)" for name in fixes)
                    if not fixes:
                        break  # nothing the AI could change — retrying is pointless
            if not advanced:
                shot = await capture_screenshot(page, attempt_id)
                return PostResult(
                    success=False,
                    error=f"Wizard page {p}/{n_pages}: the board rejected 'Next' "
                    "(validation error) and the AI pass couldn't resolve it — see screenshot.",
                    screenshot_path=shot,
                    detail=f"Filled: {', '.join(filled_all)}",
                )

    detail = f"Filled: {', '.join(filled_all)}"
    final_submit = nav.get("final_submit") or (
        submit if isinstance(submit, str) else (submit or {}).get("selector"))

    if not final_submit:
        # Never report success without actually submitting.
        shot = await capture_screenshot(page, attempt_id)
        return PostResult(
            success=False,
            error="Filled the form but no submit selector is configured — "
            "add one to field_map (or use assisted mode) before live posting.",
            screenshot_path=shot,
            detail=detail,
        )

    if hold:
        shot = await capture_screenshot(page, attempt_id)
        return PostResult(success=False, held=True, screenshot_path=shot, detail=detail)

    await page.locator(final_submit).first.click(timeout=FILL_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:  # noqa: BLE001
        await page.wait_for_timeout(3000)

    if success_selector:
        await page.wait_for_selector(success_selector, timeout=15000)

    result_url = await find_result_url(page, board, job_title, result_url_selector)
    return PostResult(success=True, result_url=result_url, detail=detail)
