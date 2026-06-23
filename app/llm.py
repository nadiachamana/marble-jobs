"""Thin Claude (Anthropic SDK) client used by inference and the auto-mapper.

One place to construct the client, hold the model choice, cache the stable
system prompt, and return parsed JSON. Defaults to claude-opus-4-8.
"""

from __future__ import annotations

import json

from app.config import get_settings

settings = get_settings()

_client = None


def available() -> bool:
    return bool(settings.anthropic_api_key)


def _get_client():
    global _client
    if _client is None:
        import anthropic

        # Extra retries smooth over transient 429/529 (the SDK backs off on its own).
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4)
    return _client


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # ```json\n...\n```  → inner
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3]
    return text.strip()


def complete_json(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    model: str | None = None,
) -> dict:
    """Call Claude and return parsed JSON.

    The system prompt is marked cacheable (stable across calls); the per-request
    `user` content is volatile and goes last. Raises on transport errors or if
    the response isn't parseable JSON.
    """
    client = _get_client()
    resp = client.messages.create(
        model=model or settings.claude_model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system + "\n\nReturn ONLY a single JSON object. No prose, no markdown fences.",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    text = _strip_fences(text)
    return json.loads(text)
