"""Master Field Schema v2 — the single source of truth for canonical fields.

Replaces the implicit ~16-field list with a declared registry. `inference`,
`automap`, the dashboard validation, and the posting engines all import from
here. Derived from master_field_schema.md (every board's manual mapping).

Three structural ideas the v1 schema lacked:
  1. Field metadata — each field declares type, source, controlled-vocab, default.
  2. Three per-board maps — field_map (selectors), select_map (value translation),
     required_fields (mandatory subset → pre-flight validation).
  3. Board-config vs job-sourced — some "fields" are board-row properties
     (listing type, publish state, point-of-contact), never inferred from Ashby.

Keys are namespaced (e.g. "classification.employment_type"). The current code
uses flat keys ("employment_type"); LEGACY_ALIASES bridges the two so existing
field_maps keep working while consumers migrate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Source(str, Enum):
    AUTO = "auto"                  # straight from Ashby jobPosting.info
    INFERRED = "inferred"          # derived by inference.py (Claude or rules)
    STATIC = "static"              # Marble constant
    MANUAL = "manual"              # Nadia confirms in the review screen
    BOARD_CONFIG = "board_config"  # set on the board row, not per job


@dataclass(frozen=True)
class Field:
    key: str                       # "classification.employment_type"
    type: str                      # text|html|url|email|date|number|enum|multi_enum|bool|file|list
    source: Source
    controlled_vocab: bool = False  # needs a select_map entry per board
    default: object = None
    enum: tuple = ()               # canonical values if controlled_vocab
    aliases: tuple = ()            # label synonyms to help the auto-mapper match
    required_default: bool = False  # boards usually make this mandatory
    notes: str = ""

    @property
    def namespace(self) -> str:
        return self.key.split(".", 1)[0]


# ───────────────────────── controlled vocabularies (§2) ─────────────────────────

EMPLOYMENT_TYPE = (
    "Full-time", "Part-time", "Contract", "Internship", "Temporary",
    "Apprenticeship", "Working-student", "PhD", "PostDoc", "VIE-VIA-VSI",
    "Civic-service", "Other",
)
FUNCTION_CATEGORY = (
    "Software Engineering", "AI/ML Engineering", "Hardware Engineering",
    "Mechanical Engineering", "Chemical Engineering", "Bioengineering",
    "Data Science", "Product", "Design", "Marketing & Comms", "Sales & BizDev",
    "Operations", "People & HR", "Finance & Accounting", "Legal & Compliance",
    "Customer Support", "Quality Assurance", "Administration",
    "Academia / Research", "Executive / Founder", "Other",
)
SENIORITY = ("Intern", "Entry", "Junior", "Mid", "Senior", "Lead", "Executive", "C-level", "Founder")
WORK_MODE = ("Remote", "Hybrid", "On-site")
CONTRACT_BASIS = ("Permanent", "Fixed-term", "Other")
SALARY_PERIOD = ("Hour", "Day", "Week", "Month", "Year")
SALARY_CURRENCY = ("EUR", "USD", "GBP", "SEK", "CHF")
SALARY_DISPLAY_MODE = ("Range", "Single", "Text")
APPLY_CHANNEL = ("URL", "Email", "Both")
APPLY_COLLECT_METHOD = ("External URL", "Board ATS")
WORKING_LANGUAGE = ("EN", "FR", "DE", "SV", "Other")
CREDITS = ("30 hp", "15 hp", "15-30 hp")
LISTING_TYPE = ("Free", "Premium")
PUBLISH_STATE = ("Post", "Draft")
SECTOR = ("VC", "Accelerator", "Incubator", "CVC", "Venture Studio", "Startup", "Other")
# Board-specific buckets translated from a canonical field via select_map:
AVAILABILITY_FR = ("Immédiate", "1 mois", "2 mois", "3 mois et plus")
REGION_CODE_FR = ("Indéfini", "Province", "Paris région parisienne", "Etranger hors UE", "DOM-TOM", "Union Européenne")
LOCATION_BUCKET = ("Africa", "Asia", "Australia", "Canada", "Europe", "Latin America", "Other", "Remote Flexible", "US States")


# ───────────────────────── canonical field catalog (§1) ─────────────────────────

_FIELDS: list[Field] = [
    # ── namespace job ──
    Field("job.title", "text", Source.AUTO, required_default=True,
          aliases=("job title", "title", "titre", "poste", "role", "position", "headline", "role title", "title of role"),
          notes="Single position, sentence case, never ALL CAPS (ClimateJobsList). Strip 'M/F', split 'PM / BizDev'."),
    Field("job.headline", "text", Source.INFERRED, aliases=("headline",), notes="KTH ≤256. Default = title[:256]."),
    Field("job.reference", "text", Source.AUTO, aliases=("reference", "référence", "external reference"), notes="Ashby job id. Optional."),
    Field("job.description_html", "html", Source.AUTO, required_default=True,
          aliases=("description", "job description", "vacancy description", "mission", "describe this role")),
    Field("job.description_plain", "text", Source.INFERRED, aliases=("description",), notes="Stripped from html for plain boards/email."),
    Field("job.description_keyword_rich", "text", Source.INFERRED, notes="KU Leuven + Tech Munich want keyword-dense copy."),
    Field("job.qualifications", "html", Source.INFERRED, aliases=("qualification", "tasks", "qualifications")),
    Field("job.preferred_experience", "html", Source.INFERRED, aliases=("preferred experience",)),
    Field("job.recruitment_process", "html", Source.INFERRED, aliases=("recruitment process",)),
    Field("job.keywords", "list", Source.INFERRED, aliases=("keywords", "tags", "mots-clés", "additional tags")),
    Field("job.disciplines", "multi_enum", Source.INFERRED, controlled_vocab=True, aliases=("disciplines",)),
    Field("job.working_language", "enum", Source.INFERRED, controlled_vocab=True, default="EN",
          enum=WORKING_LANGUAGE, aliases=("language", "langue", "working language")),
    Field("job.working_hours", "text", Source.MANUAL, aliases=("working hours",)),
    Field("job.number_of_positions", "number", Source.AUTO, default=1, aliases=("number of positions", "positions")),
    Field("job.credits", "enum", Source.MANUAL, controlled_vocab=True, enum=CREDITS, aliases=("credits", "hp")),

    # ── namespace classification ──
    Field("classification.employment_type", "enum", Source.AUTO, controlled_vocab=True, enum=EMPLOYMENT_TYPE,
          default="Full-time", required_default=True,
          aliases=("employment type", "job type", "type of job", "contract", "contrat", "type de contrat", "role type")),
    Field("classification.contract_basis", "enum", Source.INFERRED, controlled_vocab=True, enum=CONTRACT_BASIS,
          aliases=("contract type", "contract basis")),
    Field("classification.duration", "enum", Source.INFERRED, controlled_vocab=True, aliases=("duration", "durée"),
          notes="Board-specific buckets (FR CDD/Stage: 6 mois / 1 an / 2 ans…); select_map per board."),
    Field("classification.duration_min", "text", Source.INFERRED, aliases=("minimum duration", "fixed or minimum duration")),
    Field("classification.duration_max", "text", Source.INFERRED, aliases=("maximum duration",)),
    Field("classification.seniority", "enum", Source.INFERRED, controlled_vocab=True, enum=SENIORITY,
          aliases=("seniority", "job level", "level", "niveau")),
    Field("classification.experience_level", "enum", Source.INFERRED, controlled_vocab=True,
          aliases=("experience", "minimum experience", "expérience", "experience level"),
          notes="Board-specific buckets; select_map off seniority."),
    Field("classification.study_level", "enum", Source.INFERRED, controlled_vocab=True, aliases=("study level", "study level needed"),
          notes="Board-specific (KU Leuven study levels); select_map per board."),
    Field("classification.function_category", "enum", Source.INFERRED, controlled_vocab=True, enum=FUNCTION_CATEGORY,
          aliases=("category", "primary category", "function", "catégorie")),
    Field("classification.role_functions", "multi_enum", Source.INFERRED, controlled_vocab=True,
          aliases=("functions", "functions related to this role")),
    Field("classification.industry_tags", "multi_enum", Source.INFERRED, controlled_vocab=True,
          aliases=("industry", "sector", "secteur", "tech stack", "field", "category")),
    Field("classification.sector", "enum", Source.INFERRED, controlled_vocab=True, enum=SECTOR, aliases=("sector",)),
    Field("classification.is_compensated", "bool", Source.INFERRED, aliases=("is this compensated", "paid position", "compensated"),
          notes="Derived = salary present."),

    # ── namespace location ──
    Field("location.work_mode", "enum", Source.AUTO, controlled_vocab=True, enum=WORK_MODE,
          aliases=("remote", "work mode", "télétravail", "workplace")),
    Field("location.country", "text", Source.AUTO, controlled_vocab=True, aliases=("country", "pays", "countries")),
    Field("location.city", "text", Source.INFERRED, aliases=("city", "ville", "location city", "town")),
    Field("location.region", "text", Source.INFERRED, aliases=("region", "province", "state", "province/state")),
    Field("location.location_freetext", "text", Source.INFERRED, aliases=("location", "lieu", "locations"),
          notes="Composed = 'City, Country' or 'Remote'."),
    Field("location.region_code_fr", "enum", Source.INFERRED, controlled_vocab=True, enum=REGION_CODE_FR,
          aliases=("code localisation",)),
    Field("location.location_bucket", "enum", Source.INFERRED, controlled_vocab=True, enum=LOCATION_BUCKET,
          aliases=("location bucket", "continent")),
    Field("location.additional_location_info", "text", Source.MANUAL, aliases=("additional location",)),

    # ── namespace apply ──
    Field("apply.channel", "enum", Source.INFERRED, controlled_vocab=True, enum=APPLY_CHANNEL,
          aliases=("application channel", "how do you want applicants to apply")),
    Field("apply.url", "url", Source.AUTO,
          aliases=("apply url", "application url", "url pour postuler", "lien vers l'offre", "job posting link", "apply link")),
    Field("apply.email", "email", Source.STATIC, default="hiring@marble.studio",
          aliases=("apply email", "application email", "email for job applications")),
    Field("apply.instructions", "text", Source.INFERRED, aliases=("application instructions", "how to apply", "instructions")),
    Field("apply.documents_required", "multi_enum", Source.MANUAL, controlled_vocab=True,
          aliases=("application documents", "documents required")),
    Field("apply.collect_method", "enum", Source.AUTO, controlled_vocab=True, enum=APPLY_COLLECT_METHOD,
          default="External URL", aliases=("collect method",)),

    # ── namespace compensation ──
    Field("compensation.salary_min", "number", Source.INFERRED, aliases=("salary min", "minimum salary", "min")),
    Field("compensation.salary_max", "number", Source.INFERRED, aliases=("salary max", "maximum salary", "max")),
    Field("compensation.salary_currency", "enum", Source.INFERRED, controlled_vocab=True, enum=SALARY_CURRENCY,
          default="EUR", aliases=("currency", "devise")),
    Field("compensation.salary_period", "enum", Source.INFERRED, controlled_vocab=True, enum=SALARY_PERIOD,
          default="Year", aliases=("salary period", "frequency", "per")),
    Field("compensation.salary_display_mode", "enum", Source.INFERRED, controlled_vocab=True, enum=SALARY_DISPLAY_MODE,
          aliases=("salary by", "salary display")),
    Field("compensation.salary_band_fr", "enum", Source.INFERRED, controlled_vocab=True, aliases=("rémunération", "remuneration"),
          notes="FR salary bands (-20 / 20-30 / … / +150 k€); derived from min/max via select_map."),
    Field("compensation.additional_salary_info", "text", Source.MANUAL, aliases=("additional salary",)),
    Field("compensation.benefits", "text", Source.MANUAL, aliases=("benefits", "benefits / salary details")),

    # ── namespace dates ──
    Field("dates.publish_date", "date", Source.AUTO, aliases=("publish date", "publication date")),
    Field("dates.deadline", "date", Source.AUTO, aliases=("deadline", "closing date", "expiry", "date limite", "application deadline"),
          notes="KTH constraint: ≤ 6 months out."),
    Field("dates.is_ongoing", "bool", Source.INFERRED, aliases=("ongoing",), notes="Derived = no deadline."),
    Field("dates.start_date", "date", Source.INFERRED, aliases=("start date", "starting date", "date début", "available from")),
    Field("dates.start_asap", "bool", Source.INFERRED, aliases=("starting asap", "asap")),
    Field("dates.start_date_details", "text", Source.MANUAL, aliases=("start date details",)),
    Field("dates.availability_fr", "enum", Source.INFERRED, controlled_vocab=True, enum=AVAILABILITY_FR, aliases=("disponibilité",)),
    Field("dates.interview_dates", "text", Source.MANUAL, aliases=("interview dates",)),

    # ── namespace company (Marble static; portfolio overrides) ──
    Field("company.name", "text", Source.STATIC, default="Marble", required_default=True,
          aliases=("company", "company name", "employer", "employer name", "organisation", "organization", "société", "co name")),
    Field("company.legal_name", "text", Source.MANUAL, aliases=("legal name",)),
    Field("company.description", "html", Source.STATIC, aliases=("company description", "about the company", "about your company")),
    Field("company.website", "url", Source.STATIC, aliases=("website", "votre site web", "company url")),
    Field("company.logo", "file", Source.STATIC, aliases=("logo",)),
    Field("company.address", "text", Source.STATIC, aliases=("address", "adresse")),
    Field("company.vat", "text", Source.MANUAL, aliases=("vat", "vat number")),
    Field("company.social_links", "list", Source.MANUAL, aliases=("social links", "social media")),
    Field("company.hashtags", "list", Source.MANUAL, aliases=("hashtags",)),

    # ── namespace contact (poster identity — static Marble) ──
    Field("contact.first_name", "text", Source.STATIC, default="Nadia", aliases=("first name", "prénom")),
    Field("contact.last_name", "text", Source.STATIC, default="Chamana", aliases=("last name", "surname", "nom")),
    Field("contact.full_name", "text", Source.STATIC, default="Nadia Chamana",
          aliases=("your name", "contact", "nom du contact", "full name", "contact person"),
          notes="Order varies per board → board.name_format flag."),
    Field("contact.email", "email", Source.STATIC, default="hiring@marble.studio",
          aliases=("contact email", "your work email", "email du contact", "email address")),
    Field("contact.phone", "text", Source.STATIC, default="+33749945048",
          aliases=("phone", "telephone", "téléphone", "mobile", "phone number")),
    Field("contact.linkedin", "url", Source.MANUAL, aliases=("linkedin", "profil linkedin")),
    Field("contact.is_working_at_company", "bool", Source.STATIC, default=False, aliases=("working at company",)),

    # ── namespace invoice ──
    Field("invoice.email", "email", Source.STATIC, default="hiring@marble.studio", aliases=("invoice email",)),
    Field("invoice.address", "text", Source.STATIC, aliases=("invoice address",)),
    Field("invoice.company_details", "text", Source.STATIC, aliases=("company details", "invoice details")),

    # ── namespace media ──
    Field("media.logo", "file", Source.STATIC, aliases=("logo",)),
    Field("media.images", "list", Source.MANUAL, aliases=("images",)),
    Field("media.video", "url", Source.MANUAL, aliases=("video", "pitching video")),
    Field("media.slide", "file", Source.MANUAL, aliases=("slide",)),
    Field("media.attachment_pdf", "file", Source.INFERRED, aliases=("attachment", "pièce jointe", "pdf"),
          notes="Generated one-pager for email/PRO/FR boards."),

    # ── namespace board_config (per board row — NOT job-sourced, NOT inferred) ──
    Field("board_config.listing_type", "enum", Source.BOARD_CONFIG, controlled_vocab=True, enum=LISTING_TYPE, default="Free"),
    Field("board_config.broadcast_targets", "multi_enum", Source.BOARD_CONFIG),
    Field("board_config.notifications", "bool", Source.BOARD_CONFIG, default=True),
    Field("board_config.third_party_posting", "bool", Source.BOARD_CONFIG, default=True),
    Field("board_config.third_party_name", "text", Source.BOARD_CONFIG, default="Marble"),
    Field("board_config.display_third_party", "bool", Source.BOARD_CONFIG, default=True),
    Field("board_config.publish_state", "enum", Source.BOARD_CONFIG, controlled_vocab=True, enum=PUBLISH_STATE, default="Post"),
    Field("board_config.visa_sponsorship", "bool", Source.BOARD_CONFIG, default=False),
    Field("board_config.assignment_type", "enum", Source.BOARD_CONFIG, controlled_vocab=True,
          notes="KTH only (Degree project / Seasonal and part-time); board-specific."),
    Field("board_config.pitch2match", "bool", Source.BOARD_CONFIG, default=False),
    Field("board_config.point_of_contact_email", "email", Source.BOARD_CONFIG,
          notes="Board's inbound contact (e.g. InternationalWIM), distinct from contact.email."),

    # ── namespace seo ──
    Field("seo.title", "text", Source.INFERRED, notes="Default = title."),
    Field("seo.description", "text", Source.INFERRED, notes="Default = first 160 chars plain."),
    Field("seo.tweet", "text", Source.INFERRED),

    # ── namespace freetext ──
    Field("freetext.students_should_know", "text", Source.INFERRED, aliases=("what should students know",)),
    Field("freetext.notes_to_admin", "text", Source.MANUAL, aliases=("anything else we should know",)),
    Field("freetext.notes_to_applicants", "text", Source.MANUAL, aliases=("notes to applicants",)),
]

# Bump when the canonical catalog changes materially; boards store the version
# they were mapped against so the UI can prompt a re-map when stale.
SCHEMA_VERSION = 2

MASTER_SCHEMA: dict[str, Field] = {f.key: f for f in _FIELDS}


def apply_extensions() -> int:
    """Merge operator-approved SchemaExtension rows into MASTER_SCHEMA.

    Lets the auto-mapper grow the schema without a code change. Called once at
    startup (after the DB exists). Returns the number of fields added.
    """
    try:
        from app.db import SessionLocal
        from app.models import SchemaExtension
    except Exception:
        return 0
    added = 0
    try:
        with SessionLocal() as s:
            for ext in s.query(SchemaExtension).filter_by(status="approved").all():
                if ext.key in MASTER_SCHEMA:
                    continue
                try:
                    src = Source(ext.source)
                except ValueError:
                    src = Source.INFERRED
                MASTER_SCHEMA[ext.key] = Field(
                    key=ext.key, type=ext.type or "text", source=src,
                    controlled_vocab=bool(ext.controlled_vocab),
                    enum=tuple(ext.enum or ()), aliases=tuple(ext.aliases or ()),
                    notes=ext.notes or "",
                )
                added += 1
    except Exception:
        return added
    return added


# ───────────────────────── legacy flat-key bridge ─────────────────────────
# The current field_maps.py / build_master_fields use flat keys. Map them to
# canonical keys so both coexist during migration (brief §8 step 1: no behavior
# change yet). resolve_key() accepts either form.

LEGACY_ALIASES: dict[str, str] = {
    "title": "job.title",
    "headline": "job.headline",
    "description_html": "job.description_html",
    "description_plain": "job.description_plain",
    "company_name": "company.name",
    "organization": "company.name",
    "company_description": "company.description",
    "company_website": "company.website",
    "apply_url": "apply.url",
    "apply_email": "apply.email",
    "contact_email": "contact.email",
    "contact_name": "contact.full_name",
    "contact_first_name": "contact.first_name",
    "contact_last_name": "contact.last_name",
    "contact_phone": "contact.phone",
    "phone": "contact.phone",
    "city": "location.city",
    "country": "location.country",
    "region": "location.region",
    "work_mode": "location.work_mode",
    "employment_type": "classification.employment_type",
    "seniority": "classification.seniority",
    "function_category": "classification.function_category",
    "category": "classification.function_category",
    "industry_tags": "classification.industry_tags",
    "sector": "classification.sector",
    "compensated": "classification.is_compensated",
    "salary_min": "compensation.salary_min",
    "salary_max": "compensation.salary_max",
    "salary_currency": "compensation.salary_currency",
    "deadline": "dates.deadline",
    "start_date": "dates.start_date",
    "listing_type": "board_config.listing_type",
}


def resolve_key(key: str) -> str:
    """Return the canonical key for a flat-legacy or canonical key (unchanged if
    already canonical or unknown)."""
    if key in MASTER_SCHEMA:
        return key
    return LEGACY_ALIASES.get(key, key)


def get_field(key: str) -> Field | None:
    return MASTER_SCHEMA.get(resolve_key(key))


def fields_by_source(source: Source) -> list[Field]:
    return [f for f in _FIELDS if f.source == source]


def controlled_vocab_fields() -> list[Field]:
    return [f for f in _FIELDS if f.controlled_vocab]


def namespaces() -> list[str]:
    seen: list[str] = []
    for f in _FIELDS:
        if f.namespace not in seen:
            seen.append(f.namespace)
    return seen


def validate() -> list[str]:
    """Self-checks for the registry + the curated field_maps. Returns problems."""
    problems: list[str] = []
    # Unique keys
    if len(MASTER_SCHEMA) != len(_FIELDS):
        problems.append("duplicate field keys in _FIELDS")
    # CV fields with no canonical enum are fine (board-bucket), but flag plain enums w/o values
    for f in _FIELDS:
        if f.type == "enum" and f.controlled_vocab and not f.enum and not f.notes:
            problems.append(f"{f.key}: enum+controlled_vocab but no canonical values and no note")
    # Legacy aliases must point at real keys
    for legacy, canonical in LEGACY_ALIASES.items():
        if canonical not in MASTER_SCHEMA:
            problems.append(f"LEGACY_ALIASES[{legacy!r}] -> {canonical!r} which is not in the schema")
    return problems


if __name__ == "__main__":
    print(f"Master Field Schema v2 — {len(MASTER_SCHEMA)} fields across {len(namespaces())} namespaces")
    for ns in namespaces():
        keys = [k for k in MASTER_SCHEMA if k.startswith(ns + ".")]
        print(f"  {ns:14} {len(keys):2} fields")
    cv = controlled_vocab_fields()
    print(f"\nControlled-vocabulary fields (need select_map per board): {len(cv)}")
    problems = validate()
    print("\nValidation:", "OK ✓" if not problems else "PROBLEMS:")
    for p in problems:
        print("  -", p)
