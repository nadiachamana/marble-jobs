"""Notifications: Slack (threaded) + email (R-05).

A new job posts a parent Slack message ("New job ready to review …") to
#talent-ops and returns its timestamp (ts). The job stores that ts, and every
later status update — dispatch started, per-board results, completion — is
posted as a reply *in that same thread* rather than as a loose channel message.

Threading requires a bot token (chat.postMessage). If only an incoming webhook
is configured, the initial message still posts but updates can't be threaded
(Slack webhooks don't return a message ts), so we skip the channel-noise updates
in that mode and rely on email + the dashboard instead.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import httpx

from app.config import get_settings

settings = get_settings()

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def _review_url(job_id: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/jobs/{job_id}"


# ───────────────────────── Slack ─────────────────────────


def _post_slack(text: str, blocks: list | None = None, thread_ts: str | None = None) -> str | None:
    """Post a Slack message. Returns the message ts (for threading) or None.

    Prefers the bot token (supports threads). Falls back to the incoming webhook
    only for non-threaded messages (webhooks can't thread and return no ts).
    """
    if settings.slack_bot_token:
        payload: dict = {"channel": settings.slack_channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = httpx.post(
                SLACK_POST_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                # Surface misconfig (e.g. not_in_channel, invalid_auth) in logs.
                print(f"[slack] chat.postMessage error: {data.get('error')}")
                return None
            return data.get("ts")
        except httpx.HTTPError as exc:
            print(f"[slack] request failed: {exc}")
            return None

    # Fallback: incoming webhook. Can only post to the channel, never a thread.
    if settings.slack_webhook_url and not thread_ts:
        body: dict = {"text": text}
        if blocks:
            body["blocks"] = blocks
        try:
            httpx.post(settings.slack_webhook_url, json=body, timeout=10)
        except httpx.HTTPError:
            pass
    return None


def _can_thread() -> bool:
    return bool(settings.slack_bot_token)


def notify_new_job(job) -> str | None:
    """Post the parent Slack message + email. Returns the Slack ts to store on
    the job so later updates thread under it."""
    url = _review_url(job.id)
    tags = ", ".join(job.industry_tags or []) or "—"
    summary = (
        f"*{job.title}*\n"
        f"• Seniority: {job.seniority or '—'}   • Function: {job.function_category or '—'}\n"
        f"• Tags: {tags}   • Location: {job.location_city or '—'}, {job.location_country or '—'}\n"
        f"• Deadline: {job.deadline.date() if job.deadline else '—'}"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":briefcase: *New job ready to review*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review & dispatch"},
                    "url": url,
                    "style": "primary",
                }
            ],
        },
    ]
    ts = _post_slack(f"New job ready to review: {job.title} — {url}", blocks)
    send_email(
        subject=f"[Marble Jobs] Review: {job.title}",
        body=f"A new job is ready to review.\n\n{summary}\n\nReview & dispatch: {url}",
    )
    return ts


def notify_dispatch_started(job, board_count: int) -> None:
    """Threaded reply: dispatch has begun. Skipped without a thread to reply to
    (webhook-only mode) to avoid loose channel messages."""
    if not job.slack_ts:
        return
    _post_slack(
        f":rocket: Dispatching to {board_count} board(s)…",
        thread_ts=job.slack_ts,
    )


def notify_dispatch_complete(job, attempts) -> None:
    """Threaded reply summarising every board's outcome (one message, no noise).

    `attempts` is an iterable of PostingAttempt for this job.
    """
    icon = {"success": ":white_check_mark:", "failed": ":x:", "skipped": ":fast_forward:"}
    lines = []
    counts = {"success": 0, "failed": 0, "skipped": 0}
    for a in attempts:
        st = a.status.value
        counts[st] = counts.get(st, 0) + 1
        detail = ""
        if st == "failed" and a.error_message:
            detail = f" — {a.error_message[:120]}"
        elif st == "skipped" and a.error_message:
            detail = f" — {a.error_message[:120]}"
        elif st == "success" and a.result_url:
            detail = f" — <{a.result_url}|live posting>"
        lines.append(f"{icon.get(st, '•')} *{a.board.name}*{detail}")

    header = (
        f":checkered_flag: *Dispatch complete* — "
        f"{counts['success']} posted · {counts['failed']} failed · {counts['skipped']} skipped"
    )
    body = header + "\n" + "\n".join(lines)

    if _can_thread():
        _post_slack(body, thread_ts=job.slack_ts)
    else:
        # No bot token → can't thread; send one consolidated channel message
        # instead of many, and rely on email for detail.
        _post_slack(f"{body}\n{_review_url(job.id)}")

    send_email(
        subject=f"[Marble Jobs] Dispatch complete: {job.title}",
        body=body.replace("*", "") + f"\n\nDashboard: {_review_url(job.id)}",
    )


# ───────────────────────── Email ─────────────────────────


def send_email(subject: str, body: str, to: str | None = None) -> bool:
    """Send a plaintext email via SMTP. No-op if SMTP isn't configured."""
    if not settings.smtp_host:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to or settings.notify_email_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError):
        return False
