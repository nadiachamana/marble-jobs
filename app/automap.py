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
    if fid:
        return f"[id='{fid}']"
    if name:
        return f"[name='{name}']"
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
    let label = '';
    if (e.id) { const l = document.querySelector(`label[for='${e.id}']`); if (l) label = l.textContent.trim(); }
    if (!label && e.closest('label')) label = e.closest('label').textContent.trim();
    if (!label) label = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
    if (!label && e.tagName.toLowerCase()==='button') label = e.textContent.trim();
    const opts = e.tagName.toLowerCase()==='select'
        ? Array.from(e.options).map(o => o.textContent.trim()).filter(Boolean) : [];
    return { tag: e.tagName.toLowerCase(), type: e.getAttribute('type')||'', id: e.id||'',
             name: e.getAttribute('name')||'', ph: e.getAttribute('placeholder')||'',
             text: (e.textContent||'').trim().slice(0,30), label: label.slice(0,80), options: opts };
})"""


async def inspect_form(post_url: str, login: dict | None, creds: tuple) -> dict:
    """Render the form (logging in if needed) and return fields + page flags."""
    from playwright.async_api import async_playwright

    out: dict = {"fields": [], "bot_challenge": False, "needs_login": False, "final_url": ""}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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
            if login and creds[0] and creds[1] and await page.locator(login["password"]).count():
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

            out["final_url"] = page.url
            out["fields"] = await page.eval_on_selector_all("input, textarea, select, button", _EVAL)
        finally:
            await browser.close()
    return out


async def automap_board(board: BoardConfig) -> dict:
    """Full auto-map for a board row. Returns a result summary."""
    login = LOGIN_CONFIG.get(board.name)
    if login:
        login = {**login, "url": board.post_url}
    creds = settings.board_credentials(board.credentials_ref or "")

    insp = await inspect_form(board.post_url, login, creds)
    field_map, select_map = propose_field_map(insp["fields"])
    if login:
        field_map = {"login": login, **field_map}  # auth engine needs the login block first

    result = {
        "board": board.name,
        "field_map": field_map,
        "select_map": select_map,
        "bot_challenge": insp["bot_challenge"],
        "final_url": insp["final_url"],
        "n_fields": len([f for f in insp["fields"] if f["tag"] in ("input", "textarea", "select")]),
        "assist_reason": "reCAPTCHA / Cloudflare" if insp["bot_challenge"] else None,
    }
    return result


_CONTROL_KEYS = {"login", "submit", "_success_selector", "_result_url_selector"}


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
