"""Email engine (R-10).

Sends a formatted email with the job details to the board's contact address.
Used by boards like Geothermal Rising and International Women in Mining that
post on your behalf from an emailed brief. Optionally attaches a one-page PDF
if one has been generated at data/attachments/{job_id}.pdf.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.config import get_settings
from app.posting.base import ATTACHMENT_DIR, PostResult

settings = get_settings()


def _body(job, fields: dict) -> str:
    lines = [
        "Hello,",
        "",
        "Marble would like to post the following role on your job board:",
        "",
        f"Job title: {job.title}",
        f"Location: {fields.get('city') or ''} {fields.get('country') or ''}".strip(),
        f"Employment type: {job.employment_type or '—'}",
        f"Work mode: {job.work_mode or '—'}",
        f"Application deadline: {fields.get('deadline') or 'Ongoing'}",
        "",
        "How to apply:",
        f"{fields.get('apply_url') or job.apply_url or ''}",
        "",
        "Job description:",
        job.description_plain or "(see apply link)",
        "",
        "About Marble:",
        fields.get("company_description", ""),
        "",
        f"Contact: {settings.marble_contact_name} <{settings.marble_contact_email}>",
    ]
    return "\n".join(lines)


def post(job, board, fields: dict, attempt_id: str) -> PostResult:
    if not board.contact_email:
        return PostResult(success=False, error="Email board has no contact_email configured.")
    if not settings.smtp_host:
        return PostResult(
            success=False,
            error="SMTP is not configured (set SMTP_HOST etc.). Cannot send board email.",
        )

    msg = EmailMessage()
    msg["Subject"] = f"Job posting request from Marble — {job.title}"
    msg["From"] = settings.smtp_from
    msg["To"] = board.contact_email
    msg["Cc"] = settings.marble_contact_email
    msg.set_content(_body(job, fields))

    pdf = ATTACHMENT_DIR / f"{job.id}.pdf"
    if pdf.exists():
        msg.add_attachment(
            pdf.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=f"{job.title}.pdf",
        )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return PostResult(success=False, error=f"Email send failed: {exc}")

    return PostResult(success=True, detail=f"Emailed {board.contact_email}")


# Keep a path import available even if unused, for parity with other engines.
_ = Path
