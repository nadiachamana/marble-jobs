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
