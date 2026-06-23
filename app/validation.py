"""Pre-flight required-field validation (brief §5).

Surfaces missing required values at review time and blocks dispatch, instead of
letting the board reject the submission. A board declares its mandatory canonical
fields in `required_fields` (set by the auto-mapper from the form's required
controls); we resolve each against the job's values and report what's empty.
"""

from __future__ import annotations

from app.schema import get_field, resolve_key


def missing_required(job, board) -> list[str]:
    """Canonical keys this board requires but the job has no value for."""
    required = list(board.required_fields or [])
    if not required:
        return []
    from app.posting.base import build_master_fields

    values = build_master_fields(job, board, getattr(job, "apply_url", None))
    missing = []
    for key in required:
        v = values.get(key)
        if v in (None, ""):
            v = values.get(resolve_key(key))
        if v in (None, ""):
            missing.append(key)
    return missing


def field_label(key: str) -> str:
    """Human label for a canonical key (last segment, prettified)."""
    f = get_field(key)
    leaf = (f.key if f else key).rsplit(".", 1)[-1]
    return leaf.replace("_", " ").title()


def board_ready(board) -> tuple[bool, str]:
    """Whether a board may be set 'active': field_map present, submit confirmed,
    no unresolved-required from the last auto-map. Returns (ok, reason)."""
    cov = board.coverage or {}
    if not (board.field_map or {}):
        return False, "no field_map yet"
    if cov.get("unresolved_required", 0):
        return False, f"{cov['unresolved_required']} unresolved required field(s)"
    if "submit" not in (board.field_map or {}) and not board.requires_assist:
        return False, "submit selector not confirmed"
    return True, "ready"
