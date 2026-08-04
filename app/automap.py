"""Auto-mapper — generate a board's field_map from its post URL (no hand-editing).

Renders the live form (logging in first for authenticated boards), reads every
field's label / name / placeholder / type, and matches each to a master field
using a keyword + input-type scoring table. Produces a ready-to-use field_map
(and a starter select_map for dropdowns), plus flags bot-challenges that force
assisted mode.

CLI:
    python -m app.automap "MIT Orbit"            # propose, print, don't save
    python -m app.automap "MIT Orbit" --save     # save to the board row

Programmatic: automap_board(board) -> dict (used by the /boards/automap route).
"""

from __future__ import annotations

import re
import sys

from app.config import get_settings
from app.db import SessionLocal
from app.models import BoardConfig

settings = get_settings()

# Per-board login flow for authenticated boards (selectors discovered by probing
# each login page). The auto-mapper logs in with these before inspecting.
LOGIN_CONFIG = {
    "KU Leuven - Alumni": {"username": "#email", "password": "#passwordInput", "submit": "button:has-text('Log in')"},
    "Conservation Job Board": {"username": "[name='email']", "password": "#password", "submit": "button:has-text('Sign in')"},
    "KTH": {"username": "#Email", "password": "#PasswordValue", "submit": "#submit-signin-local"},
}

# master_field -> keywords matched against label/name/placeholder/id (lowercased).
# Longer, more specific phrases score higher, so "company name" beats bare "name".
_MASTER_RULES: dict[str, list[str]] = {
    "contact_first_name": ["first name", "prénom", "given name"],
    "contact_last_name": ["last name", "surname", "nom de famille"],
    "contact_name": ["your name", "full name", "contact name", "contact person", "nom du contact",
                     "partner name", "partner_name"],
    "contact_email": ["work email", "contact email", "your email", "email address", "e-mail", "courriel", "email"],
    "contact_phone": ["phone number", "telephone", "téléphone", "mobile", "contact number", "phone", "tel"],
    "company_name": ["company name", "employer name", "trade name", "organisation", "organization",
                     "employer", "société", "entreprise", "company", "co_name", "co name"],
    "title": ["job title", "title of role", "role title", "job role", "position", "headline", "titre", "role", "title"],
    "apply_url": ["application url", "apply url", "apply link", "application link", "job posting link",
                  "job posting url", "url to apply", "url pour postuler", "application website",
                  "website", "url"],
    "city": ["location city", "city", "town", "ville", "location"],
    "country": ["location country", "country", "pays", "nation"],
    "company_description": ["company description", "about the company", "about your company",
                            "description de la société", "describe your company"],
    "description_plain": ["job description", "describe this role", "role description",
                          "description de la mission", "responsibilities", "description", "details", "message"],
    "employment_type": ["employment type", "type of job", "contract type", "job type", "role type",
                        "type de contrat", "contract"],
    "work_mode": ["work mode", "workplace", "work arrangement", "télétravail", "remote"],
    "seniority": ["experience level", "career level", "seniority", "minimum experience", "level", "expérience"],
    "salary_min": ["salary min", "minimum salary", "min salary", "salary from"],
    "salary_max": ["salary max", "maximum salary", "max salary", "salary to"],
    "salary": ["salary", "compensation", "rémunération", "remuneration"],
    "deadline": ["application deadline", "closing date", "deadline", "expiry", "expiration", "date limite"],
    "industry_tags": ["primary category", "industry", "sector", "category", "secteur", "tags", "field"],
    # Common board-specific fields with sensible constant defaults
    # (see build_master_fields): start_date→"ASAP", compensated→"Yes",
    # listing_type→"Free".
    "start_date": ["start date", "starting date", "available from", "date de début", "date début", "start"],
    "compensated": ["is this a paid", "is this compensated", "compensated", "paid position", "paid role"],
    "listing_type": ["listing type", "job ad type", "ad type"],
}

# Strong signals from the input type itself.
_TYPE_HINTS = {"email": "contact_email", "url": "apply_url", "date": "deadline", "tel": "contact_phone"}

_SUBMIT_WORDS = ["submit", "post job", "post your job", "publish", "create", "save", "envoyer", "poster", "déposer"]

# ── multi-page / navigation keywords ──
# Link/button that leads FROM a dashboard or jobs-list page TO the posting form
# (e.g. targetconnect lands on the employer jobs list after login; the real form
# is behind "Add new vacancy").
_POST_ENTRY_WORDS = [
    "add new vacancy", "add a vacancy", "add vacancy", "post a job", "post your job",
    "post new job", "post job", "add a job", "add job post", "add new job", "add job",
    "new vacancy", "create a job", "create job", "publish a job", "submit a job",
    "déposer une offre", "publier une offre",
]
# Wizard step navigation.
_NEXT_WORDS = ["next", "continue", "suivant", "continuer", "proceed"]
# Final wizard submit (checked before Next so the walker NEVER clicks it).
_FINAL_WORDS = ["add vacancy", "post job", "post your job", "post vacancy", "publish",
                "submit", "finish", "envoyer", "publier"]

MAX_WIZARD_PAGES = 6


def _attr_sel(attr: str, val: str) -> str | None:
    """[attr='val'] with a quote style that survives quotes in val; None if both."""
    if "'" not in val:
        return f"[{attr}='{val}']"
    if '"' not in val:
        return f'[{attr}="{val}"]'
    return None


def _selector(f: dict) -> str | None:
    """Build a *stable, unique* CSS selector for a field, or None if we can't.

    Returning None (and skipping the field) is deliberately preferred over a bare
    tag like "input": an ambiguous selector silently targets the first matching
    element, so several fields would collide on the same box. A field we can't
    address uniquely is left unmapped for the human to fill in assisted mode.
    """
    fid = f.get("id") or ""
    name = f.get("name") or ""
    if fid and re.fullmatch(r"[A-Za-z_][\w-]*", fid):
        return f"#{fid}"
    if fid and (sel := _attr_sel("id", fid)):
        return sel
    if name and (sel := _attr_sel("name", name)):
        return sel
    # React form builders (Fillout, Typeform-style) render inputs with no
    # id/name but a unique aria-label — a perfectly stable selector.
    aria = f.get("aria") or ""
    if aria and (sel := _attr_sel("aria-label", aria)):
        return f"{f['tag']}{sel}"
    # Rich-text editors (Summernote & co) often have no id/name — address by
    # their first class ("div.note-editable"). Page-scoped fills keep it unique.
    cls = f.get("cls") or ""
    if f.get("widget") == "rich_text" and cls and re.fullmatch(r"[A-Za-z_][\w-]*", cls):
        return f"{f['tag']}.{cls}"
    # Typed inputs that are almost always unique on a job-post form.
    itype = (f.get("type") or "").lower()
    if f["tag"] == "input" and itype in ("email", "tel", "url"):
        return f"input[type='{itype}']"
    ph = f.get("ph") or ""
    if ph and not ph.lower().lstrip().startswith("e.g"):
        return f"{f['tag']}[placeholder=\"{ph[:40]}\"]"
    # No id / name / usable placeholder → not uniquely addressable. Skip it.
    return None


def _haystack(field: dict) -> str:
    """Searchable text for a field: label + name + id (+ placeholder unless it's
    an example value). camelCase and snake/kebab are split into words so
    'jobPostingLink' matches 'job posting link'.
    """
    parts = [field.get("label", ""), field.get("name", ""), field.get("id", "")]
    ph = field.get("ph", "")
    # Drop "e.g. ..." example placeholders — they're sample data, not labels,
    # and pollute matching (e.g. an example apply URL containing 'company').
    if ph and not ph.lower().lstrip().startswith("e.g") and "://" not in ph:
        parts.append(ph)
    text = " ".join(parts)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)      # camelCase -> camel Case
    text = re.sub(r"[_\-]+", " ", text)                    # snake/kebab -> spaces
    return re.sub(r"\s+", " ", text).lower().strip()


def _score(field: dict, keywords: list[str]) -> int:
    hay = _haystack(field)
    best = 0
    for kw in keywords:
        if kw in hay:
            best = max(best, len(kw))
    return best


def propose_field_map(fields: list[dict]) -> tuple[dict, dict]:
    """Return (field_map, select_map) proposed from inspected form fields."""
    inputs = [f for f in fields if f["tag"] in ("input", "textarea", "select") and f.get("type") != "hidden"]

    # Score every (field, master) pair, then greedily assign one-to-one.
    pairs: list[tuple[int, int, str]] = []
    for i, f in enumerate(inputs):
        ftype = (f.get("type") or "").lower()
        if ftype in _TYPE_HINTS and _TYPE_HINTS[ftype]:
            pairs.append((50, i, _TYPE_HINTS[ftype]))  # type is a very strong signal
        for master, kws in _MASTER_RULES.items():
            s = _score(f, kws)
            if not s:
                continue
            # On equal keyword length, prefer a text input over a <select> so a
            # field like "Role" maps to the title box, not a "Role Type" dropdown.
            if f["tag"] == "select":
                s -= 1
            pairs.append((s, i, master))

    pairs.sort(reverse=True)
    used_fields: set[int] = set()
    used_masters: set[str] = set()
    field_map: dict = {}
    select_map: dict = {}

    for score, i, master in pairs:
        if score < 3 or i in used_fields or master in used_masters:
            continue
        f = inputs[i]
        selector = _selector(f)
        if selector is None:
            # Not uniquely addressable — skip rather than emit a colliding "input".
            used_fields.add(i)
            continue
        used_fields.add(i)
        used_masters.add(master)
        if f["tag"] == "select":
            field_map[master] = {"selector": selector, "type": "select"}
            opts = [o for o in (f.get("options") or []) if o]
            if opts:
                select_map[master] = {"_options_on_board": opts}  # operator maps standard→board values
        elif f["tag"] == "textarea" and re.fullmatch(r"mce_\d+", f.get("id") or ""):
            # TinyMCE backing textarea (id "mce_N") — fill its contenteditable
            # iframe (#mce_N_ifr) as rich text, not the hidden textarea.
            field_map[master] = {"selector": f"#{f['id']}_ifr", "type": "richtext"}
        else:
            field_map[master] = selector
        # Multi-page wizard: remember which page this control lives on.
        if f.get("page", 1) > 1:
            spec = field_map[master]
            if isinstance(spec, str):
                spec = {"selector": spec, "type": "fill"}
            spec["page"] = f["page"]
            field_map[master] = spec

    # Submit button: prefer an explicit submit, else a button whose text reads like one.
    submit = None
    for f in fields:
        if f["tag"] not in ("button", "input"):
            continue
        text = (f.get("text") or f.get("label") or "").lower()
        if f.get("type") == "submit" or any(w in text for w in _SUBMIT_WORDS):
            if f.get("id") and re.fullmatch(r"[A-Za-z_][\w-]*", f["id"]):
                submit = f"#{f['id']}"
            elif f.get("text"):
                submit = f"{f['tag']}:has-text(\"{f['text'][:30]}\")"
            else:
                submit = _selector(f)
            if "submit" in text or f.get("type") == "submit":
                break  # an explicit submit wins; keep looking only for a better explicit one
    if submit:
        field_map["submit"] = {"selector": submit, "type": "click"}

    return field_map, select_map


# ───────────────────────── browser inspection ─────────────────────────

_EVAL = """els => els.map(e => {
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type')||'').toLowerCase();
    const role = (e.getAttribute('role')||'').toLowerCase();
    let label = '';
    // Escape the id (some forms have ids with quotes/brackets) and never let a
    // single bad element throw and abort the whole scan.
    if (e.id) { try { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) label = l.textContent.trim(); } catch (_) {} }
    if (!label && e.closest('label')) { try { label = e.closest('label').textContent.trim(); } catch (_) {} }
    if (!label) label = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
    if (!label && tag==='button') label = e.textContent.trim();
    // Widget classification — drives the fill strategy.
    let widget = 'text';
    if (tag==='select') widget='native_select';
    else if (tag==='textarea') widget='textarea';
    else if (type==='radio') widget='radio';
    else if (type==='checkbox') widget='checkbox';
    else if (type==='file') widget='file';
    else if (role==='combobox' || (e.id||'').includes('react-select')) widget='react_select';
    else if (e.getAttribute('contenteditable')==='true' || (e.id||'').match(/^mce_\\d+$/)) widget='rich_text';
    else if (tag==='button' || type==='submit') widget='button';
    const opts = tag==='select'
        ? Array.from(e.options).map(o => o.textContent.trim()).filter(Boolean) : [];
    const required = !!(e.required || e.getAttribute('aria-required')==='true' || /\\*/.test(label));
    const vis = !!e.getClientRects().length && window.getComputedStyle(e).visibility !== 'hidden';
    const cls = (typeof e.className === 'string' ? e.className : '').trim().split(/\\s+/)[0] || '';
    const checked = (type==='radio' || type==='checkbox') ? !!e.checked : undefined;
    return { tag, type, role, id: e.id||'', name: e.getAttribute('name')||'', ph: e.getAttribute('placeholder')||'',
             aria: (e.getAttribute('aria-label')||'').slice(0,90),
             text: (e.textContent||'').trim().slice(0,40), label: label.slice(0,90),
             widget, options: opts, required, vis, cls, checked };
})"""

_HEADINGS_EVAL = "els => els.map(e => (e.innerText||'').trim()).filter(Boolean).slice(0, 8)"

# ARIA pill/choice groups (Fillout, Typeform & co): DIVs with role=radiogroup /
# role=group whose "options" are clickable [role=radio]/[role=checkbox] children
# — invisible to the input/select scan, which is how Baby VC's Contract/Sector
# questions were never discovered.
_GROUPS_EVAL = """els => els.map(e => {
    if (!e.getClientRects().length) return null;
    const radios = [...e.querySelectorAll('[role=radio]')];
    const checks = [...e.querySelectorAll('[role=checkbox]')];
    const members = radios.length ? radios : checks;
    if (!members.length) return null;
    const optText = m => {
      let t = (m.innerText||'').trim();
      if (!t && m.parentElement) t = (m.parentElement.innerText||'').trim();
      if (!t && m.closest('label')) t = (m.closest('label').innerText||'').trim();
      return t.split('\\n')[0].slice(0, 60);
    };
    let options = [...new Set(members.map(optText).filter(Boolean))];
    if (!options.length) {
      // Some builders (Fillout checkbox pills) keep the text outside the
      // [role=checkbox] element — fall back to the group's own text lines.
      options = [...new Set((e.innerText||'').split('\\n').map(t => t.trim())
                  .filter(t => t && t.length < 60))];
    }
    if (!options.length) return null;
    const checked = members.some(m => m.getAttribute('aria-checked') === 'true');
    return { aria: (e.getAttribute('aria-label')||'').slice(0,90),
             kind: radios.length ? 'aria_radio' : 'aria_check',
             options: options.slice(0, 30), checked };
}).filter(Boolean)"""


async def scan_choice_groups(page) -> list[dict]:
    """Discover ARIA pill groups as pseudo-controls (widget aria_radio/aria_check).

    Only groups with an aria-label are addressable (`[role=radiogroup]
    [aria-label='…']`); unlabeled ones are skipped."""
    try:
        raw = await page.eval_on_selector_all("[role=radiogroup], [role=group]", _GROUPS_EVAL)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for g in raw:
        aria = g.get("aria") or ""
        if not aria:
            continue
        sel = _attr_sel("aria-label", aria)
        if not sel:
            continue
        role = "radiogroup" if g["kind"] == "aria_radio" else "group"
        out.append({
            "tag": "div", "type": "", "id": "", "name": "", "ph": "", "cls": "",
            "label": aria, "aria": aria, "widget": g["kind"], "options": g["options"],
            "required": False, "vis": True, "checked": g.get("checked"),
            "selector": f"[role={role}]{sel}",
        })
    return out


async def harvest_combo_options(page, selector: str) -> list[str]:
    """React-select style dropdowns keep their options OUT of the DOM until
    clicked — click, scrape the revealed [role=option] list, Escape."""
    try:
        await page.locator(selector).first.click(timeout=3000)
        await page.wait_for_timeout(700)
        opts = await page.eval_on_selector_all(
            "[role=option]",
            "els => [...new Set(els.filter(e => e.getClientRects().length)"
            ".map(e => (e.innerText||'').trim()).filter(Boolean))].slice(0, 50)")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
        return opts
    except Exception:  # noqa: BLE001
        return []


async def _headings(page) -> list[str]:
    try:
        return await page.eval_on_selector_all("h1, h2, h3, legend", _HEADINGS_EVAL)
    except Exception:  # noqa: BLE001
        return []


# Visible clickable elements (links included — the field _EVAL skips <a>).
_CLICKABLE_EVAL = """els => els.map(e => {
    const visible = !!e.getClientRects().length;
    const text = ((e.innerText || e.value || e.getAttribute('aria-label') || '')
                  .trim().slice(0, 60)).replace(/\\s+/g, ' ');
    return { tag: e.tagName.toLowerCase(), id: e.id || '', text, visible };
}).filter(x => x.visible && x.text)"""


async def _clickables(page) -> list[dict]:
    try:
        return await page.eval_on_selector_all(
            "a, button, input[type=submit], [role=button]", _CLICKABLE_EVAL)
    except Exception:  # noqa: BLE001
        return []


def _match_clickable(clickables: list[dict], words: list[str]) -> str | None:
    """Selector for the first clickable whose text matches one of `words`.

    The length guard stops a paragraph-sized element that merely *contains*
    "post a job" from matching — we want short button/link labels.
    """
    for c in clickables:
        text = c["text"].lower()
        for w in words:
            if w in text and len(text) <= len(w) + 25:
                if c["id"] and re.fullmatch(r"[A-Za-z_][\w-]*", c["id"]):
                    return f"#{c['id']}"
                if c["tag"] == "input":  # :has-text doesn't see an input's value attr
                    return f"input[type='submit'][value*=\"{w}\" i]"
                return f"{c['tag']}:has-text(\"{w}\")"
    return None


def _looks_like_post_form(fields: list[dict]) -> bool:
    """Is this page the posting form itself (vs a jobs list / dashboard)?

    A rich-text editor, or 3+ text inputs matching strong job-post concepts,
    says form. A list page's filter dropdowns are selects, which don't count —
    that's exactly what mis-mapped Imperial's filter bar as job fields.
    """
    strong = 0
    for f in fields:
        if f.get("widget") == "rich_text":
            return True
        if f["tag"] in ("input", "textarea") and (f.get("type") or "text") in (
                "", "text", "url", "email", "number", "date"):
            for master in ("title", "description_plain", "company_name", "apply_url",
                           "contact_email", "salary", "deadline"):
                if _score(f, _MASTER_RULES[master]) >= 5:
                    strong += 1
                    break
    return strong >= 3


def _page_signature(fields: list[dict], headings: list[str] | tuple = ()) -> tuple:
    """Fingerprint of a wizard step — used to tell if 'Next' advanced.

    Headings ("Advertising details" → "Job details") are the primary signal.
    Control ids/names have digit runs stripped: sites like targetconnect stamp
    fresh timestamps into dialog ids on every render, which made every rescan
    look like a new page even when validation kept us on the same step.
    """
    def norm(s: str | None) -> str:
        return re.sub(r"\d+", "", s or "")

    ctl = tuple(sorted({(f["tag"], norm(f.get("id")), norm(f.get("name")),
                         (f.get("label") or "")[:30])
                        for f in fields if f.get("vis", True)}))
    return (tuple(headings), ctl)


def _advanced(sig_before: tuple, sig_after: tuple) -> bool:
    """Did 'Next' really move to a new wizard step?

    When headings exist, THEY must change — filling a form can reveal extra
    controls (an 'apply by URL' checkbox shows the URL box), which changes the
    control set without leaving the step. Control-set change only counts when
    the page has no headings to compare.
    """
    heads_before, ctl_before = sig_before
    heads_after, ctl_after = sig_after
    if heads_before or heads_after:
        return heads_before != heads_after
    return ctl_before != ctl_after


def clean_post_url(url: str) -> str:
    """Strip expiring Spring-WebFlow style tokens (?execution=e1s1) from a post
    URL — targetconnect tokens die after the session, so a stored one 404s."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    if "execution=" not in (parts.query or ""):
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "execution"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


_SAMPLE_TEXT = "Sample data for field mapping — not published"


def _dateish_select(f: dict) -> bool:
    """A select that's one part of a split date widget (day/month/year)."""
    ident = f"{f.get('id') or ''} {f.get('name') or ''} {f.get('label') or ''}".lower()
    if any(w in ident for w in ("date", "month", "year", "day", "expiry")):
        return True
    opts = [o for o in (f.get("options") or []) if o]
    return bool(opts) and all(re.fullmatch(r"\d{1,4}", o) for o in opts[:8])


def _date_part_of(f: dict) -> str:
    """Which part of a split date a select holds: 'month' | 'year' | 'day'."""
    ident = f"{f.get('id') or ''} {f.get('name') or ''}".lower()
    if "month" in ident:
        return "month"
    if "year" in ident:
        return "year"
    if "day" in ident or "date" in ident:
        return "day"
    opts = [o for o in (f.get("options") or []) if o]
    if opts and all(re.fullmatch(r"\d{4}", o) for o in opts[:6]):
        return "year"
    if opts and any(re.match(r"[A-Za-z]{3}", o) for o in opts[:6]):
        return "month"
    return "day"


async def _sample_date_select(page, sel: str, f: dict) -> None:
    """Pick a NEAR-future sample date part (~45 days out) — boards often cap how
    far ahead a closing date may be (Imperial: max 90 days), so 'last option'
    style far-future picks get rejected."""
    from datetime import date, timedelta

    target = date.today() + timedelta(days=45)
    cands = {
        "day": [f"{target.day:02d}", str(target.day)],
        "month": [target.strftime("%b"), target.strftime("%B"), f"{target.month:02d}", str(target.month)],
        "year": [str(target.year)],
    }[_date_part_of(f)]
    for cand in cands:
        for by in ("label", "value"):
            try:
                await page.select_option(sel, **{by: cand}, timeout=1500)
                return
            except Exception:  # noqa: BLE001
                continue
    opts = f.get("options") or []
    if len(opts) > 1:  # fallback: first real option
        await page.select_option(sel, index=1, timeout=2000)


async def _fill_sample(page, fields: list[dict], everything: bool = False) -> None:
    """Best-effort placeholder fill so a wizard's Next passes validation.

    Only ever used during discovery, and the walker never clicks the final
    submit — nothing is published. Fills required controls (or all, on retry).
    Never overwrites values already present (e.g. a prefilled publish date)."""
    seen_groups: set[str] = set()
    for f in fields:
        # ARIA pill groups: pick the first option so validation lets Next pass.
        if f.get("widget") in ("aria_radio", "aria_check"):
            if f.get("checked") or not (f.get("required") or everything):
                continue
            try:
                first_opt = (f.get("options") or [""])[0]
                if first_opt and f.get("selector"):
                    await page.locator(f["selector"]).get_by_text(first_opt, exact=False).first.click(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            continue
        if f["tag"] not in ("input", "textarea", "select") or f.get("widget") == "button":
            continue
        if (f.get("type") or "").lower() in ("hidden", "submit", "button", "file", "image"):
            continue
        if not f.get("vis", True):
            continue
        required = f.get("required", False)
        if not everything and not required:
            continue
        sel = _selector(f)
        if not sel:
            continue
        widget = f.get("widget")
        itype = (f.get("type") or "").lower()
        try:
            if widget == "native_select":
                if await page.input_value(sel, timeout=1200):
                    continue  # keep prefilled values (publish date etc.)
                if _dateish_select(f):
                    await _sample_date_select(page, sel, f)
                else:
                    opts = f.get("options") or []
                    await page.select_option(sel, index=1 if len(opts) > 1 else 0, timeout=2500)
            elif widget in ("radio", "checkbox"):
                group = f.get("name") or sel
                if group in seen_groups:
                    continue
                seen_groups.add(group)
                # Leave groups that already have a selection (defaults) alone.
                already = any(g.get("checked") for g in fields
                              if (g.get("name") or _selector(g)) == group)
                if not already and (required or everything):
                    await page.check(sel, timeout=2500)
            elif widget == "rich_text":
                if re.fullmatch(r"mce_\d+", f.get("id") or ""):
                    await page.frame_locator(f"#{f['id']}_ifr").locator("body").fill(_SAMPLE_TEXT, timeout=2500)
                else:
                    await page.locator(sel).first.fill(_SAMPLE_TEXT, timeout=2500)
            elif itype == "email":
                await page.fill(sel, "hiring@marble.studio", timeout=2500)
            elif itype == "url":
                await page.fill(sel, "https://marble.studio", timeout=2500)
            elif itype == "tel":
                await page.fill(sel, "+33700000000", timeout=2500)
            elif itype == "number":
                await page.fill(sel, "10", timeout=2500)
            elif itype == "date":
                await page.fill(sel, "2030-01-31", timeout=2500)
            else:
                # Don't overwrite anything already there.
                if not await page.input_value(sel, timeout=1500):
                    # Type by NAME hints too — a text input called
                    # "applicationURL" fails server validation on plain text.
                    ident = f"{f.get('id') or ''} {f.get('name') or ''} {f.get('label') or ''}".lower()
                    if "url" in ident or "link" in ident or "website" in ident:
                        value = "https://marble.studio"
                    elif "mail" in ident:
                        value = "hiring@marble.studio"
                    elif "phone" in ident or "tel" in ident:
                        value = "+33700000000"
                    elif "salary" in ident or "number" in ident:
                        value = "10"
                    else:
                        value = _SAMPLE_TEXT
                    await page.fill(sel, value, timeout=2500)
        except Exception:  # noqa: BLE001 — sample fill is opportunistic
            continue

    # Rich-text editors with no id/name (Summernote's div.note-editable etc.)
    # are invisible to the per-field loop — fill any empty visible ones directly.
    try:
        editors = page.locator("[contenteditable=true]")
        for i in range(min(await editors.count(), 4)):
            ed = editors.nth(i)
            if await ed.is_visible() and not (await ed.inner_text()).strip():
                await ed.fill(_SAMPLE_TEXT, timeout=2000)
    except Exception:  # noqa: BLE001
        pass


async def inspect_form(post_url: str, login: dict | None, creds: tuple,
                       form_layout: str = "auto") -> dict:
    """Render the form (logging in if needed) and return fields + page flags."""
    from playwright.async_api import async_playwright

    post_url = clean_post_url(post_url)

    async def _scan(page) -> list[dict]:
        raw = await page.eval_on_selector_all(
            "input, textarea, select, button, [role=combobox], [contenteditable=true]", _EVAL)
        out_fields = []
        for f in raw:
            # type=hidden server fields can't be filled by Playwright anyway.
            if (f.get("type") or "").lower() == "hidden":
                continue
            # Invisible controls with timestamped ids are template/dialog junk
            # (targetconnect renders hundreds) — they poison classification.
            if not f.get("vis", True) and re.search(r"\d{6,}", f.get("id") or ""):
                continue
            out_fields.append(f)
        # ARIA pill groups (role=radiogroup/group) — not inputs, scanned apart.
        out_fields.extend(await scan_choice_groups(page))
        # React-select dropdowns hide their options until clicked — harvest
        # them so classification can build a value_map (cap the clicking).
        harvested = 0
        for f in out_fields:
            if f.get("widget") == "react_select" and not f.get("options") and harvested < 6:
                sel = _selector(f)
                if not sel:
                    continue
                opts = await harvest_combo_options(page, sel)
                if opts:
                    f["options"] = opts
                harvested += 1
        return out_fields

    out: dict = {"fields": [], "bot_challenge": False, "needs_login": False,
                 "final_url": "", "nav": {}, "pages": 1, "nav_blocked": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await (await browser.new_context()).new_page()
        try:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            # Heavy React/SPA forms render after load — wait for a field to appear.
            try:
                await page.wait_for_selector("input, textarea, select", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            frames = " ".join(f.url for f in page.frames)
            body = ""
            try:
                body = (await page.inner_text("body"))[:1500].lower()
            except Exception:
                pass
            if any(k in (frames + body) for k in ["challenges.cloudflare", "turnstile", "recaptcha",
                                                  "hcaptcha", "security verification"]):
                out["bot_challenge"] = True

            # Log in if a login config + creds are available and a password box shows.
            if (login and login.get("password") and login.get("username") and login.get("submit")
                    and creds[0] and creds[1] and await page.locator(login["password"]).count()):
                out["needs_login"] = True
                try:
                    await page.fill(login["username"], creds[0])
                    await page.fill(login["password"], creds[1])
                    await page.click(login["submit"])
                    await page.wait_for_timeout(5000)
                    await page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Re-check for a CAPTCHA on the (now authenticated) post form.
            if await page.locator("[id*=recaptcha], .g-recaptcha, iframe[src*=recaptcha]").count():
                out["bot_challenge"] = True

            fields = await _scan(page)

            # ── smart navigation: are we on the posting form, or a jobs list /
            # dashboard the post_url redirected to after login? If a "post a
            # job"-style link exists and the page doesn't look like the form,
            # follow the link (and remember it so posting can do the same).
            if not out["bot_challenge"]:
                clickables = await _clickables(page)
                entry = _match_clickable(clickables, _POST_ENTRY_WORDS)
                if entry and (form_layout == "multi" or not _looks_like_post_form(fields)):
                    try:
                        await page.locator(entry).first.click(timeout=8000)
                        await page.wait_for_load_state("domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(3000)
                        out["nav"]["post_link"] = entry
                        fields = await _scan(page)
                    except Exception:  # noqa: BLE001 — stay where we are
                        pass

            # ── wizard walk: map every page of a multi-page form. Sample data
            # unlocks each Next; the FINAL submit is detected but NEVER clicked.
            for f in fields:
                f["page"] = 1
            all_fields = list(fields)
            if not out["bot_challenge"] and form_layout != "single":
                page_no = 1
                while page_no < MAX_WIZARD_PAGES:
                    clickables = await _clickables(page)
                    final_sel = _match_clickable(clickables, _FINAL_WORDS)
                    next_sel = _match_clickable(clickables, _NEXT_WORDS)
                    if final_sel and not next_sel:
                        out["nav"]["final_submit"] = final_sel
                        break
                    if not next_sel:
                        break  # single-page form; the classic submit finder applies
                    out["nav"]["next"] = next_sel
                    sig = _page_signature(fields, await _headings(page))
                    advanced = False
                    for attempt_all in (False, True):  # required-only first, then everything
                        await _fill_sample(page, fields, everything=attempt_all)
                        if attempt_all:
                            # Filling can reveal new controls (e.g. an "apply by
                            # URL" checkbox shows the URL box) — pick those up too.
                            fields = await _scan(page)
                            await _fill_sample(page, fields, everything=True)
                        try:
                            await page.locator(next_sel).first.click(timeout=8000)
                            await page.wait_for_timeout(3000)
                        except Exception:  # noqa: BLE001
                            break
                        fields = await _scan(page)
                        if _advanced(sig, _page_signature(fields, await _headings(page))):
                            advanced = True
                            break
                    if not advanced:
                        out["nav_blocked"] = (
                            f"page {page_no}: 'Next' did not advance (validation likely "
                            f"blocked on a field the sample filler couldn't complete)")
                        break
                    page_no += 1
                    for f in fields:
                        f["page"] = page_no
                    all_fields.extend(fields)
                out["pages"] = page_no

            out["final_url"] = page.url
            out["fields"] = all_fields
        finally:
            await browser.close()
    return out


# ───────────────────────── Claude classification (discover→classify→propose→report) ─────────────────────────

# widget → field_map fill type understood by the posting engines.
_WIDGET_TYPE = {
    "native_select": "select",
    "rich_text": "richtext",
    "react_select": "react_select",
    "checkbox": "check",
    "radio": "radio",
    "aria_radio": "aria_radio",
    "aria_check": "aria_check",
}


def _find_submit(fields: list[dict]) -> str | None:
    """Best submit-button selector, or None (operator confirms before go-live)."""
    for f in fields:
        if f.get("tag") not in ("button", "input"):
            continue
        text = (f.get("text") or f.get("label") or "").lower()
        if f.get("type") == "submit" or any(w in text for w in _SUBMIT_WORDS):
            if f.get("id") and re.fullmatch(r"[A-Za-z_][\w-]*", f["id"]):
                return f"#{f['id']}"
            if f.get("text"):
                return f"{f.get('tag', 'button')}:has-text(\"{f['text'][:30]}\")"
            return _selector(f)
    return None


def _schema_dump() -> str:
    """Compact schema description for the classifier: key, type, aliases, enum."""
    import app.schema as S

    lines = []
    for key, f in S.MASTER_SCHEMA.items():
        if f.source == S.Source.BOARD_CONFIG:
            continue  # board-row props aren't mapped from the form's job fields
        bits = [f"{key} ({f.type})"]
        if f.aliases:
            bits.append("aka " + ", ".join(f.aliases[:5]))
        if f.enum:
            bits.append("values: " + ", ".join(f.enum))
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


_CLASSIFY_SYSTEM = (
    "You map a job-board form's controls to Marble's canonical field schema. "
    "Return ONLY JSON of shape:\n"
    '{ "mappings": [ {"selector": "<as given>", "schema_key": "<canonical key or UNMAPPED>", '
    '"confidence": 0-1, "value_map": {"<board option label>": "<canonical value>"}, '
    '"part": "day|month|year (only for split date selects)" } ],\n'
    '  "new_field_proposals": [ {"selector": "...", "label": "...", "suggested_key": "namespace.x", '
    '"type": "text|enum|...", "required": true, "reason": "..."} ] }\n'
    "Rules: map each control to the single best canonical key, or UNMAPPED if none fits. "
    "For enum/select/radio controls, build value_map from the control's options to the canonical "
    "values listed for that key. Make value_map COMPLETE: every canonical enum value that has any "
    "plausible board option must appear (e.g. Full-time must map to the board's closest full-time/"
    "graduate-position option, even if the wording differs a lot); only omit a canonical value when "
    "truly nothing on the board corresponds. When ONE date is split "
    "across several <select> controls (separate day / month / year dropdowns), map EACH select to "
    "the SAME date schema key and set \"part\" accordingly — this is the only case where a key may "
    "repeat. Controls carry a \"page\" number when the form is a multi-page wizard; keep mapping "
    "them all. When a visible control and a hidden "
    "(visible:false) control represent the same concept, map the VISIBLE one — hidden inputs are "
    "usually JS-managed backing fields that cannot be typed into. Every REQUIRED control must "
    "either map to a key or appear in new_field_proposals. Propose a new field only for a genuinely "
    "new concept not in the schema.\n\nCanonical schema:\n"
)


def _classify_prompt() -> str:
    return _CLASSIFY_SYSTEM + _schema_dump()


def classify_with_claude(controls: list[dict]) -> dict:
    """Ask Claude to map discovered controls → schema. Returns mappings + proposals."""
    import json

    from app import llm

    payload = [
        {
            "selector": c["selector"], "label": c.get("label") or c.get("ph") or "",
            "name": c.get("name") or "", "widget": c.get("widget"),
            "required": c.get("required", False), "options": (c.get("options") or [])[:40],
            "page": c.get("page", 1), "visible": c.get("vis", True),
        }
        for c in controls
    ]
    user = "Controls:\n" + json.dumps(payload, ensure_ascii=False)
    return llm.complete_json(_classify_prompt(), user, max_tokens=4096)


def build_from_classification(controls: list[dict], classification: dict) -> dict:
    """Turn Claude's classification into field_map / select_map / coverage / proposals."""
    import app.schema as S

    by_selector = {c["selector"]: c for c in controls}
    field_map: dict = {}
    select_map: dict = {}
    field_notes: dict = {}
    required_fields: list[str] = []
    unresolved_required: list[str] = []
    used_keys: set[str] = set()
    # Split date widgets (day/month/year selects mapped to one date key) are
    # assembled into a single {"type": "date_parts", "day": …, "month": …} spec.
    date_parts: dict[str, dict] = {}

    for m in classification.get("mappings", []):
        key = m.get("schema_key")
        sel = m.get("selector")
        conf = m.get("confidence", 0)
        ctrl = by_selector.get(sel)
        if not ctrl:
            continue
        pageno = ctrl.get("page", 1)
        part = (m.get("part") or "").lower()
        if (part in ("day", "month", "year") and key and key in S.MASTER_SCHEMA and conf >= 0.5):
            d = date_parts.setdefault(key, {"type": "date_parts"})
            d[part] = sel
            if pageno > 1:
                d["page"] = pageno
            if ctrl.get("required") and key not in required_fields:
                required_fields.append(key)
            continue
        # A bare tag ("input") means the control had no stable selector — using
        # it would type several fields into the first box on the page. Skip it,
        # same as the heuristic mapper does.
        if not key or key == "UNMAPPED" or key not in S.MASTER_SCHEMA or conf < 0.5 or key in used_keys \
                or sel in ("input", "textarea", "select", "button"):
            if ctrl.get("required"):
                unresolved_required.append(f"{ctrl.get('label') or sel}")
            continue
        used_keys.add(key)
        ftype = _WIDGET_TYPE.get(ctrl.get("widget"), "fill")
        if ctrl.get("widget") == "rich_text" and ctrl.get("id", "").startswith("mce_"):
            spec: dict | str = {"selector": f"#{ctrl['id']}_ifr", "type": "richtext"}
        elif ftype == "fill" and pageno == 1:
            spec = sel  # legacy-compatible plain selector
        else:
            spec = {"selector": sel, "type": ftype}
        if pageno > 1:
            if isinstance(spec, str):
                spec = {"selector": spec, "type": "fill"}
            spec["page"] = pageno
        field_map[key] = spec
        # value translation: invert {board_label: canonical_value} → {canonical_value: board_label}
        vmap = m.get("value_map") or {}
        if vmap:
            inv = {}
            for board_label, canon in vmap.items():
                if canon:
                    inv[str(canon)] = board_label
            if inv:
                select_map[key] = inv
        if ctrl.get("required"):
            required_fields.append(key)
        if ctrl.get("widget") in ("react_select", "radio"):
            field_notes[key] = f"{ctrl['widget']} widget — verify fill behavior"

    for key, d in date_parts.items():
        field_map.setdefault(key, d)

    submit = _find_submit(controls)
    if submit:
        field_map["submit"] = {"selector": submit, "type": "click"}

    proposals = classification.get("new_field_proposals", []) or []
    coverage = {
        "controls": len([c for c in controls if c.get("widget") not in ("button",)]),
        "mapped": len([k for k in field_map if k not in _CONTROL_KEYS]),
        "select_maps": len(select_map),
        "new_field_proposals": len(proposals),
        "unresolved_required": len(unresolved_required),
        "unresolved_required_labels": unresolved_required[:10],
        "submit_confirmed": "submit" in field_map,
        "schema_version": S.SCHEMA_VERSION,
    }
    return {
        "field_map": field_map, "select_map": select_map, "coverage": coverage,
        "proposals": proposals, "field_notes": field_notes, "required_fields": required_fields,
    }


async def automap_board(board: BoardConfig) -> dict:
    """Full auto-map for a board row: discover → classify → propose → report.

    Uses Claude classification when an API key is configured; otherwise falls back
    to the heuristic alias matcher.
    """
    from app import llm

    # Prefer the board's own login config (set in the form), else the built-in
    # selectors for known boards. Login here is CSS selectors; the actual
    # username/password come from Railway env vars via credentials_ref.
    login = (board.field_map or {}).get("login") or LOGIN_CONFIG.get(board.name)
    if login:
        login = {**login, "url": login.get("url") or board.post_url}
    creds = settings.board_credentials(board.credentials_ref or "")

    # "auto" (default) detects wizards by finding a Next button; the board form
    # can force "single" (never walk) or "multi" (always follow the post link).
    form_layout = ((board.meta or {}).get("form_layout") or "auto").lower()

    insp = await inspect_form(board.post_url, login, creds, form_layout=form_layout)
    # Choice groups arrive with a pre-built group selector — keep it.
    controls = [dict(f, selector=f.get("selector") or _selector(f) or f.get("tag"))
                for f in insp["fields"]]

    coverage: dict = {}
    proposals: list = []
    field_notes: dict = {}
    required_fields: list = []

    used_claude = False
    if llm.available() and not insp["bot_challenge"]:
        try:
            classification = classify_with_claude([c for c in controls if c.get("widget") != "button"])
            built = build_from_classification(controls, classification)
            field_map, select_map = built["field_map"], built["select_map"]
            coverage, proposals = built["coverage"], built["proposals"]
            field_notes, required_fields = built["field_notes"], built["required_fields"]
            used_claude = True
        except Exception as exc:  # noqa: BLE001 — fall back to heuristic
            print(f"[automap] Claude classify failed ({str(exc)[:80]}); using heuristic")

    if not used_claude:
        field_map, select_map = propose_field_map(insp["fields"])

    # Multi-page navigation: the wizard's real final button beats whatever the
    # generic submit finder picked (which may have been a step's Next button).
    nav = dict(insp.get("nav") or {})
    if insp.get("pages", 1) > 1:
        nav["pages"] = insp["pages"]
    if nav.get("final_submit"):
        field_map["submit"] = {"selector": nav["final_submit"], "type": "click"}
    if nav:
        field_map["nav"] = nav

    if login:
        field_map = {"login": login, **field_map}

    if coverage:
        coverage["pages"] = insp.get("pages", 1)

    return {
        "board": board.name,
        "field_map": field_map,
        "select_map": select_map,
        "coverage": coverage,
        "proposals": proposals,
        "field_notes": field_notes,
        "required_fields": required_fields,
        "used_claude": used_claude,
        "bot_challenge": insp["bot_challenge"],
        "final_url": insp["final_url"],
        "pages": insp.get("pages", 1),
        "nav_blocked": insp.get("nav_blocked"),
        "n_fields": len([f for f in insp["fields"] if f["tag"] in ("input", "textarea", "select")]),
        "assist_reason": "reCAPTCHA / Cloudflare" if insp["bot_challenge"] else None,
    }


_CONTROL_KEYS = {"login", "submit", "nav", "_success_selector", "_result_url_selector"}


def real_field_count(field_map: dict) -> int:
    return len([k for k in field_map if k not in _CONTROL_KEYS])


def apply_result(board: BoardConfig, result: dict, session) -> bool:
    """Save the auto-mapped config. Refuses to overwrite a good map with an
    almost-empty one (e.g. when a SPA failed to render). Returns True if saved.
    """
    if real_field_count(result["field_map"]) < 2 and real_field_count(board.field_map or {}) >= 2:
        return False
    board.field_map = result["field_map"]
    if result["select_map"]:
        board.select_map = result["select_map"]
    # v2 metadata
    cov = dict(result.get("coverage") or {})
    if result.get("proposals"):
        cov["proposals"] = result["proposals"]
    board.coverage = cov
    if result.get("field_notes"):
        board.field_notes = result["field_notes"]
    if result.get("required_fields"):
        board.required_fields = result["required_fields"]
    if cov.get("schema_version"):
        board.schema_version = cov["schema_version"]
    if result["bot_challenge"]:
        board.requires_assist = True
        board.assist_reason = result["assist_reason"]
    session.commit()
    return True


if __name__ == "__main__":
    import anyio

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    save = "--save" in sys.argv
    if not args:
        print('Usage: python -m app.automap "<board name>" [--save]')
        raise SystemExit(1)

    s = SessionLocal()
    b = s.query(BoardConfig).filter_by(name=" ".join(args)).one_or_none()
    if not b:
        print(f"Board not found: {' '.join(args)!r}")
        raise SystemExit(1)

    res = anyio.run(automap_board, b)
    print(f"\n▶ {res['board']}  ({res['n_fields']} form fields at {res['final_url']})")
    if res.get("pages", 1) > 1:
        print(f"   📄 multi-page wizard: {res['pages']} pages walked (final submit never clicked)")
    if res.get("nav_blocked"):
        print(f"   ⚠ wizard walk stopped early: {res['nav_blocked']}")
    if res["bot_challenge"]:
        print(f"   🛑 bot-challenge detected → requires assisted mode ({res['assist_reason']})")
    print("\n   Proposed field_map:")
    import json

    print(json.dumps(res["field_map"], indent=2))
    if res["select_map"]:
        print("\n   Dropdowns needing a select_map (board option labels shown):")
        print(json.dumps(res["select_map"], indent=2))
    if save:
        if apply_result(b, res, s):
            print("\n   ✓ saved to the board row.")
        else:
            print("\n   ⚠ NOT saved — too few fields found (form likely didn't render or needs login). "
                  "Existing map kept.")
    else:
        print("\n   (dry run — add --save to write it to the board)")
