"""Inference engine (R-03).

Derives seniority, function category, industry tags, salary, and a city
fallback from Ashby job data using static lookup tables. No ML. Every inferred
value is shown to Nadia in the review form and is editable before dispatch, so
these rules only need to be good defaults, not perfect.
"""

from __future__ import annotations

import re

# ───────────────────────── seniority ─────────────────────────
# Checked in order; first match wins. Title keywords, case-insensitive.
_SENIORITY_RULES: list[tuple[str, list[str]]] = [
    ("Intern", ["intern", "internship", "working student", "apprentice"]),
    ("Executive", [
        "ceo", "cto", "coo", "cfo", "cmo", "chief", "co-founder", "cofounder",
        "founder", "founding", "president", "vp ", "vice president", "head of",
        "director", "right hand",
    ]),
    ("Senior", ["senior", "lead", "principal", "staff", "head "]),
    ("Entry", ["junior", "graduate", "intern", "assistant", "trainee", "entry"]),
]


def infer_seniority(title: str) -> str:
    t = f" {title.lower()} "
    for label, keywords in _SENIORITY_RULES:
        if any(k in t for k in keywords):
            return label
    # "Associate" / "Analyst" tend to be early-career but not interns.
    if any(k in t for k in ["associate", "analyst"]):
        return "Entry"
    return "Mid"


# ───────────────────────── function category ─────────────────────────
# Keyword -> canonical function category. Title + department are both scanned.
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("Engineering & Technical", [
        "cto", "engineer", "engineering", "developer", "software", "technical",
        "robotics", "hardware", "mechanical", "electrical", "firmware",
    ]),
    ("Data & AI", ["data", "machine learning", "ml ", "ai ", "ai/", "scientist", "research"]),
    ("Design & Product", ["design", "product", "ux", "ui"]),
    ("Marketing & Communications", ["marketing", "communication", "content", "brand", "pr "]),
    ("Sales & Business Development", ["sales", "business development", "bizdev", "growth", "partnerships"]),
    ("People & Talent", ["talent", "recruit", "people", "hr ", "human resources"]),
    ("Operations", ["operations", "operating", "coo", "programme", "program ", "chief of staff", "right hand"]),
    ("Finance & Investment", ["finance", "cfo", "investment", "investor", "vc ", "venture", "analyst"]),
    ("Leadership / Founder", ["ceo", "co-founder", "cofounder", "founder", "founding", "president"]),
]


def infer_function_category(title: str, department: str | None = None) -> str:
    haystack = f" {title.lower()} {(department or '').lower()} "
    for label, keywords in _CATEGORY_RULES:
        if any(k in haystack for k in keywords):
            return label
    return "Generalist"


# ───────────────────────── industry tags ─────────────────────────
# Normalise free-text bracket content to canonical climate tags where possible.
_TAG_SYNONYMS = {
    "geothermal": "Geothermal",
    "next-gen geothermal": "Geothermal",
    "advanced biofuels": "Advanced Biofuels",
    "biofuels": "Advanced Biofuels",
    "wastewater": "Wastewater",
    "clean cooling": "Clean Cooling",
    "cooling": "Clean Cooling",
    "battery": "Battery",
    "energy systems": "Energy Systems",
    "energy hardware": "Energy Systems",
    "construction robotics": "Construction Robotics",
    "construction operations": "Construction",
    "industrial intelligence": "Industrial Intelligence",
}


def infer_industry_tags(title: str) -> list[str]:
    """Extract tags from a bracket suffix, e.g. 'CTO (Next-Gen Geothermal)'."""
    tags: list[str] = []
    for match in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", title):
        for piece in re.split(r"[,/|&]| and ", match):
            piece = piece.strip()
            if not piece:
                continue
            canonical = _TAG_SYNONYMS.get(piece.lower(), piece)
            if canonical not in tags:
                tags.append(canonical)
    return tags


# ───────────────────────── salary ─────────────────────────
# Marble Residency / Co-Founder in Residence roles carry a €3,000/month allowance.
RESIDENCY_MONTHLY_EUR = 3000


def infer_salary(department: str | None, title: str) -> tuple[int | None, int | None, str | None]:
    """Return (min, max, currency). None when it should be entered manually."""
    hay = f"{(department or '').lower()} {title.lower()}"
    if "residency" in hay or "in residence" in hay or "founder" in hay:
        return RESIDENCY_MONTHLY_EUR, RESIDENCY_MONTHLY_EUR, "EUR"
    return None, None, None


# ───────────────────────── city fallback ─────────────────────────
# Country -> representative city when Ashby provides only a country.
_CITY_FALLBACK = {
    "uk": "London",
    "united kingdom": "London",
    "gb": "London",
    "france": "Paris",
    "fr": "Paris",
    "usa": "San Francisco",
    "united states": "San Francisco",
    "us": "San Francisco",
    "germany": "Berlin",
    "de": "Berlin",
    "netherlands": "Amsterdam",
    "spain": "Madrid",
    "sweden": "Stockholm",
    "belgium": "Brussels",
    "switzerland": "Zurich",
}


def infer_city(country: str | None, is_remote: bool = False) -> str | None:
    if is_remote:
        return "Remote"
    if not country:
        return None
    return _CITY_FALLBACK.get(country.strip().lower())


# ───────────────────────── orchestrator ─────────────────────────


def infer_all(
    *,
    title: str,
    department: str | None,
    country: str | None,
    is_remote: bool = False,
) -> dict:
    """Run every inference rule. Returned dict maps straight onto JobQueue fields."""
    smin, smax, currency = infer_salary(department, title)
    return {
        "seniority": infer_seniority(title),
        "function_category": infer_function_category(title, department),
        "industry_tags": infer_industry_tags(title),
        "salary_min": smin,
        "salary_max": smax,
        "salary_currency": currency,
        "location_city": infer_city(country, is_remote),
    }


# ═══════════════════════ v2: canonical payload (schema-keyed) ═══════════════════════
# Produces a dict keyed by canonical schema keys (app/schema.py), drawing from
# three sources per the brief: AUTO (Ashby), STATIC (Marble constants), and
# INFERRED (Claude, constrained to the canonical controlled vocabularies, with a
# rule-based fallback when no API key / on error).

from app import llm  # noqa: E402
from app import schema as S  # noqa: E402

# Canonical fields Claude infers from the JD. Kept to a high-value subset; every
# CV field is validated against its schema enum after the call.
_INFERRED_KEYS = [
    "classification.seniority",
    "classification.function_category",
    "classification.role_functions",
    "classification.industry_tags",
    "classification.sector",
    "classification.is_compensated",
    "location.city",
    "job.headline",
    "job.keywords",
    "job.working_language",
    "compensation.salary_min",
    "compensation.salary_max",
    "compensation.salary_currency",
    "seo.title",
    "seo.description",
    "freetext.students_should_know",
]


def _schema_slice_text() -> str:
    lines = []
    for key in _INFERRED_KEYS:
        f = S.MASTER_SCHEMA[key]
        allowed = f"  [choose from: {', '.join(f.enum)}]" if f.enum else ""
        note = f" — {f.notes}" if f.notes else ""
        lines.append(f"- {key} ({f.type}){allowed}{note}")
    return "\n".join(lines)


_INFER_SYSTEM = (
    "You classify a job posting into a fixed schema for Marble, a climate-tech "
    "venture studio. For each field choose the best value; for fields with an "
    "allowed list you MUST pick from it (use the closest match, or \"Other\" if "
    "present and nothing fits). Use arrays for list/multi_enum fields. Use null "
    "when the posting genuinely doesn't say. Keep headline ≤256 chars; seo.title "
    "≤70; seo.description ≤160.\n\nFields:\n" + _schema_slice_text()
)


def _validate_inferred(raw: dict) -> dict:
    """Keep only known keys; coerce CV values to canonical enum members.

    Accepts both full dotted keys ('classification.seniority') and the leaf name
    ('seniority') in case the model shortens them.
    """
    # Claude often nests dotted keys as {"classification": {"seniority": ...}};
    # flatten one level so both nested and flat forms resolve.
    flat: dict = {}
    for k, v in raw.items():
        if isinstance(v, dict) and k not in _INFERRED_KEYS:
            for subk, subv in v.items():
                flat[f"{k}.{subk}"] = subv
        else:
            flat[k] = v

    leaf_to_key = {k.rsplit(".", 1)[-1]: k for k in _INFERRED_KEYS}
    resolved: dict = {}
    for rawk, v in flat.items():
        key = rawk if rawk in _INFERRED_KEYS else leaf_to_key.get(rawk.rsplit(".", 1)[-1])
        if key:
            resolved[key] = v

    out: dict = {}
    for key in _INFERRED_KEYS:
        if key not in resolved or resolved[key] in (None, "", []):
            continue
        f = S.MASTER_SCHEMA[key]
        val = resolved[key]
        if f.enum:
            allowed = {e.lower(): e for e in f.enum}
            if f.type in ("multi_enum", "list"):
                vals = val if isinstance(val, list) else [val]
                coerced = [allowed[str(v).lower()] for v in vals if str(v).lower() in allowed]
                if coerced:
                    out[key] = coerced
            else:
                m = allowed.get(str(val).lower())
                if m:
                    out[key] = m
        else:
            out[key] = val
    return out


# Ashby raw enum → canonical schema value, so per-board select_maps line up.
_EMPLOYMENT_NORM = {
    "fulltime": "Full-time", "full-time": "Full-time", "parttime": "Part-time",
    "part-time": "Part-time", "intern": "Internship", "internship": "Internship",
    "contract": "Contract", "contractor": "Contract", "temporary": "Temporary",
}
_WORKMODE_NORM = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "on-site": "On-site"}


def _auto_from_job(job) -> dict:
    g = lambda a: getattr(job, a, None)  # noqa: E731
    emp = g("employment_type")
    emp = _EMPLOYMENT_NORM.get((emp or "").lower(), emp) if emp else None
    wm = g("work_mode")
    wm = _WORKMODE_NORM.get((wm or "").lower(), wm) if wm else None
    out = {
        "job.title": g("title"),
        "job.description_html": g("description_html"),
        "job.description_plain": g("description_plain"),
        "classification.employment_type": emp,
        "location.work_mode": wm,
        "location.country": g("location_country"),
        "apply.url": g("apply_url"),
        "job.reference": g("ashby_job_id"),
        "job.number_of_positions": 1,
    }
    if g("location_city"):
        out["location.city"] = g("location_city")
    if g("deadline"):
        out["dates.deadline"] = job.deadline.date().isoformat()
    if g("salary_min"):
        out["compensation.salary_min"] = g("salary_min")
        out["compensation.salary_max"] = g("salary_max")
        out["compensation.salary_currency"] = g("salary_currency") or "EUR"
    return {k: v for k, v in out.items() if v not in (None, "")}


def _static_fields() -> dict:
    from app.posting.base import MARBLE_BOILERPLATE

    out = {}
    for key, f in S.MASTER_SCHEMA.items():
        if f.source == S.Source.STATIC and f.default is not None:
            out[key] = f.default
    out["company.description"] = MARBLE_BOILERPLATE
    return out


def _rule_inferred(job) -> dict:
    """Fallback when Claude is unavailable: reuse the rule-based functions."""
    title = getattr(job, "title", "") or ""
    dept = getattr(job, "department", None)
    country = getattr(job, "location_country", None)
    is_remote = bool(getattr(job, "work_mode", "") and "remote" in (job.work_mode or "").lower())
    smin, smax, currency = infer_salary(dept, title)
    out = {
        "classification.seniority": infer_seniority(title),
        "classification.function_category": infer_function_category(title, dept),
        "classification.industry_tags": infer_industry_tags(title),
    }
    city = infer_city(country, is_remote)
    if city:
        out["location.city"] = city
    if smin:
        out["compensation.salary_min"] = smin
        out["compensation.salary_max"] = smax
        out["compensation.salary_currency"] = currency
    return out


def infer_canonical(job) -> dict:
    """Full canonical payload (schema-keyed) for a job: AUTO + STATIC + INFERRED.

    INFERRED uses Claude when an API key is configured, else rule-based fallback.
    Returns {canonical_key: value}; the dispatcher walks each board's
    field_map/select_map over this dict.
    """
    canonical = _static_fields()
    canonical.update(_auto_from_job(job))

    inferred: dict = {}
    if llm.available():
        try:
            user = (
                f"title: {getattr(job, 'title', '')!r}\n"
                f"department: {getattr(job, 'department', None)!r}\n"
                f"country: {getattr(job, 'location_country', None)!r}\n"
                f"work_mode: {getattr(job, 'work_mode', None)!r}\n"
                f"description:\n{(getattr(job, 'description_plain', '') or '')[:6000]}"
            )
            inferred = _validate_inferred(llm.complete_json(_INFER_SYSTEM, user))
        except Exception as exc:  # noqa: BLE001 — degrade to rules, never crash
            print(f"[inference] Claude call failed ({str(exc)[:80]}); using rules")
            inferred = _rule_inferred(job)
    else:
        inferred = _rule_inferred(job)

    # AUTO/STATIC win over INFERRED only where they actually have a value.
    for k, v in inferred.items():
        canonical.setdefault(k, v)
    return canonical
