"""Smart fill — the AI layer at POSTING time (prompt-to-post).

The mechanical `fill_form` pass can only fill what the field_map anticipated,
with exact option labels. Real boards have board-specific rules the mapping
can't know: a closing date capped at 90 days, an employment-type list whose
"full-time" is called "Graduate position (3 years+ after graduating)", a
mandatory salary-range select the job has no data for.

After each page's mechanical fill, `smart_fill(page, fields)` reads the LIVE
page — every still-empty control with its exact options, plus the page's
visible text (labels, hints, validation errors like "cannot be more than 90
days in the future" / "Set to the maximum date: 01-Nov-2026") — and asks
Claude what to enter, then applies it. When "Next" is rejected, the same pass
runs again: the error banner is now part of the page text, so the model can
correct exactly what the board complained about.

Degrades to a no-op without an API key (same policy as inference/automap).
"""

from __future__ import annotations

import json

# Empty, visible, enabled controls only — with current value and exact options.
_GAP_EVAL = """els => els.map(e => {
    const st = window.getComputedStyle(e);
    const vis = !!e.getClientRects().length && st.visibility !== 'hidden' && !e.disabled;
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type')||'').toLowerCase();
    const ce = e.isContentEditable === true;
    let value = '';
    if (tag === 'select') value = e.value ? ((e.selectedOptions[0]||{}).textContent||'').trim() : '';
    else if (type === 'checkbox' || type === 'radio') value = e.checked ? 'checked' : '';
    else if (ce) value = (e.innerText||'').trim();
    else value = (e.value||'').trim();
    let label = '';
    if (e.id) { try { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) label = l.textContent.trim(); } catch (_) {} }
    if (!label && e.closest('label')) { try { label = e.closest('label').textContent.trim(); } catch (_) {} }
    if (!label) label = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
    const opts = tag === 'select'
        ? Array.from(e.options).map(o => o.textContent.trim()).filter(Boolean).slice(0, 60) : [];
    const required = !!(e.required || e.getAttribute('aria-required')==='true' || /\\*/.test(label));
    const cls = (typeof e.className === 'string' ? e.className : '').trim().split(/\\s+/)[0] || '';
    return { tag, type, id: e.id||'', name: e.getAttribute('name')||'', label: label.slice(0,90),
             aria: (e.getAttribute('aria-label')||'').slice(0,90),
             value, options: opts, required, vis, cls,
             widget: ce ? 'rich_text' : (tag==='select' ? 'native_select' : type || 'text') };
})"""

_SYSTEM = (
    "You complete a job-posting web form for Marble (a climate tech venture studio). "
    "You get: the job's data, the form's still-EMPTY controls (with their EXACT dropdown "
    "options), and the page's visible text (which may contain validation errors, hints, "
    "and constraints). Decide what to enter in each control. Return ONLY JSON:\n"
    '{"actions": [{"selector": "<as given>", "action": "select|fill|check|skip", '
    '"value": "<text, or EXACT option label, or [array of labels] for multi-selects>"}]}\n'
    "Rules:\n"
    "- select/check controls: value MUST be copied verbatim from that control's options list.\n"
    "- Fill required AND optional controls whenever the job data supports a sensible value; "
    "skip only when nothing sensible exists.\n"
    "- Closing/expiry date parts (day/month/year selects): obey any maximum-date constraint in "
    "the page text (e.g. 'cannot be more than 90 days in the future', 'Set to the maximum "
    "date: 01-Nov-2026'). When a cap exists, choose the LATEST allowed date. Otherwise use the "
    "job's deadline; with no deadline, about 60 days from today.\n"
    "- Employment type: pick the option closest in meaning (a full-time role maps to a "
    "'graduate position'/'full-time' style option, never an internship/voluntary one).\n"
    "- Sector/category/occupation selects: pick the option(s) closest to the job's "
    "industry/function; for multi-selects return 1-3 labels as an array.\n"
    "- Number of positions/vacancies: 1 unless the job says otherwise.\n"
    "- Start date: job's start date, else 'ASAP/Immediate'-style options, else ~60 days out.\n"
    "- REQUIRED select with no matching job data: choose the most neutral option "
    "('Competitive', 'Not specified', 'Prefer not to say', 'Other').\n"
    "- NEVER invent specific salaries, contact details, emails, phone numbers, or URLs that "
    "are not in the job data.\n"
    "- JOB DESCRIPTION fields must contain the FULL description, never a summary: return "
    '{"action": "fill", "value": "__FULL_DESCRIPTION__"} and, if the page states a character '
    'limit (e.g. "max 4000 characters"), add "max_chars": <the limit>. The engine inserts the '
    "complete description text, trimmed to that limit.\n"
    "- Other rich-text/instructions controls (e.g. 'how to apply'): write 1-3 short factual "
    "sentences from the job data (e.g. how to apply via the application URL).\n"
    "- One action per radio GROUP (pick the one radio to check).\n"
)

_FILL_TIMEOUT = 6000


def _control_selector(c: dict) -> str | None:
    from app.automap import _selector

    return _selector({**c, "ph": c.get("label", "")})


async def _collect_gaps(page) -> list[dict]:
    controls = await page.eval_on_selector_all(
        "input, textarea, select, [contenteditable=true]", _GAP_EVAL)
    checked_groups = {c.get("name") for c in controls
                      if c.get("type") in ("radio", "checkbox") and c.get("value") == "checked"}
    gaps, seen_groups = [], set()
    for c in controls:
        if not c.get("vis") or c.get("value"):
            continue
        if (c.get("type") or "") in ("hidden", "submit", "button", "file", "image", "search", "password"):
            continue
        if c.get("type") in ("radio", "checkbox"):
            group = c.get("name") or c.get("id")
            if group in checked_groups:
                continue  # group already has a selection (defaults)
        sel = _control_selector(c)
        if not sel:
            continue
        c["selector"] = sel
        gaps.append(c)
    # Cap payload size; required first so they never fall off the end.
    gaps.sort(key=lambda c: not c.get("required"))
    return gaps[:40]


def _job_payload(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if v in (None, ""):
            continue
        s = str(v)
        if k in ("description_html", "job.description_html"):
            continue  # html blob; the plain version is enough context
        # Description stays substantial so the model knows what it covers; the
        # full text is inserted locally via __FULL_DESCRIPTION__, never retyped.
        limit = 3000 if k in ("description_plain", "job.description_plain") else 600
        out[k] = s[:limit]
    return out


def _full_description(fields: dict, max_chars: int | None) -> str:
    text = str(fields.get("description_plain") or fields.get("job.description_plain")
               or fields.get("description_html") or "")
    if max_chars and len(text) > max_chars:
        # Trim at a word boundary just under the board's limit.
        cut = text[: max_chars - 1]
        text = cut[: cut.rfind(" ")] if " " in cut else cut
    return text


async def smart_fill(page, fields: dict) -> list[str]:
    """One AI gap pass over the current page. Returns labels of controls filled."""
    from app import llm

    if not llm.available():
        return []
    try:
        gaps = await _collect_gaps(page)
    except Exception:  # noqa: BLE001
        return []
    if not gaps:
        return []

    try:
        body = (await page.inner_text("body"))[:3500]
    except Exception:  # noqa: BLE001
        body = ""
    from datetime import date

    payload = {
        "today": date.today().isoformat(),
        "page_text": body,
        "job": _job_payload(fields),
        "controls": [
            {"selector": c["selector"], "label": c.get("label") or c.get("name") or c.get("id"),
             "widget": c.get("widget"), "type": c.get("type"), "required": c.get("required"),
             "options": c.get("options") or []}
            for c in gaps
        ],
    }
    try:
        out = llm.complete_json(_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=3000)
    except Exception as exc:  # noqa: BLE001 — AI layer is best-effort
        print(f"[smartfill] Claude call failed ({str(exc)[:80]}); leaving gaps for the human")
        return []

    by_selector = {c["selector"]: c for c in gaps}
    filled: list[str] = []
    for a in out.get("actions", []):
        sel = a.get("selector")
        action = (a.get("action") or "").lower()
        value = a.get("value")
        ctrl = by_selector.get(sel)
        if not ctrl or action in ("skip", "") or value in (None, "", []):
            continue
        label = ctrl.get("label") or ctrl.get("name") or sel
        try:
            if action == "select":
                values = value if isinstance(value, list) else [value]
                # Labels must exist on the control — refuse hallucinated options.
                legit = [v for v in values if v in (ctrl.get("options") or [])]
                if not legit:
                    continue
                await page.select_option(sel, label=legit, timeout=_FILL_TIMEOUT)
            elif action == "check":
                await page.locator(sel).first.check(timeout=_FILL_TIMEOUT)
            elif action == "fill":
                text = str(value)
                if text.strip() == "__FULL_DESCRIPTION__":
                    try:
                        max_chars = int(a.get("max_chars") or 0) or None
                    except (TypeError, ValueError):
                        max_chars = None
                    text = _full_description(fields, max_chars)
                    if not text:
                        continue
                if ctrl.get("widget") == "rich_text":
                    await page.locator(sel).first.fill(text, timeout=_FILL_TIMEOUT)
                else:
                    await page.fill(sel, text, timeout=_FILL_TIMEOUT)
            else:
                continue
            filled.append(label)
        except Exception:  # noqa: BLE001 — a control it can't reach stays for the human
            continue
    if filled:
        print(f"[smartfill] AI filled: {', '.join(filled)}")
    return filled


def smart_fill_sync(page, fields: dict) -> list[str]:
    """Sync twin of smart_fill for assisted mode (playwright.sync_api page):
    same gap collection, same prompt, same guardrails."""
    from app import llm

    if not llm.available():
        return []
    try:
        controls = page.eval_on_selector_all(
            "input, textarea, select, [contenteditable=true]", _GAP_EVAL)
    except Exception:  # noqa: BLE001
        return []
    checked_groups = {c.get("name") for c in controls
                      if c.get("type") in ("radio", "checkbox") and c.get("value") == "checked"}
    gaps = []
    for c in controls:
        if not c.get("vis") or c.get("value"):
            continue
        if (c.get("type") or "") in ("hidden", "submit", "button", "file", "image", "search", "password"):
            continue
        if c.get("type") in ("radio", "checkbox") and (c.get("name") or c.get("id")) in checked_groups:
            continue
        sel = _control_selector(c)
        if not sel:
            continue
        c["selector"] = sel
        gaps.append(c)
    gaps.sort(key=lambda c: not c.get("required"))
    gaps = gaps[:40]
    if not gaps:
        return []

    try:
        body = page.inner_text("body")[:3500]
    except Exception:  # noqa: BLE001
        body = ""
    from datetime import date

    payload = {
        "today": date.today().isoformat(),
        "page_text": body,
        "job": _job_payload(fields),
        "controls": [
            {"selector": c["selector"], "label": c.get("label") or c.get("name") or c.get("id"),
             "widget": c.get("widget"), "type": c.get("type"), "required": c.get("required"),
             "options": c.get("options") or []}
            for c in gaps
        ],
    }
    try:
        out = llm.complete_json(_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=3000)
    except Exception as exc:  # noqa: BLE001
        print(f"[smartfill] Claude call failed ({str(exc)[:80]}); leaving gaps for the human")
        return []

    by_selector = {c["selector"]: c for c in gaps}
    filled: list[str] = []
    for a in out.get("actions", []):
        sel, action, value = a.get("selector"), (a.get("action") or "").lower(), a.get("value")
        ctrl = by_selector.get(sel)
        if not ctrl or action in ("skip", "") or value in (None, "", []):
            continue
        label = ctrl.get("label") or ctrl.get("name") or sel
        try:
            if action == "select":
                values = value if isinstance(value, list) else [value]
                legit = [v for v in values if v in (ctrl.get("options") or [])]
                if not legit:
                    continue
                page.select_option(sel, label=legit, timeout=_FILL_TIMEOUT)
            elif action == "check":
                page.locator(sel).first.check(timeout=_FILL_TIMEOUT)
            elif action == "fill":
                text = str(value)
                if text.strip() == "__FULL_DESCRIPTION__":
                    try:
                        max_chars = int(a.get("max_chars") or 0) or None
                    except (TypeError, ValueError):
                        max_chars = None
                    text = _full_description(fields, max_chars)
                    if not text:
                        continue
                page.locator(sel).first.fill(text, timeout=_FILL_TIMEOUT)
            else:
                continue
            filled.append(label)
        except Exception:  # noqa: BLE001
            continue
    if filled:
        print(f"[smartfill] AI filled: {', '.join(filled)}")
    return filled
