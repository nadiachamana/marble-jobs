"""Probe a board's post URL (read-only) to classify what it takes to automate.

Reports, per URL: where it ends up after load, whether a login/password field is
present, how many form fields render, and whether a bot-challenge (Cloudflare
Turnstile / reCAPTCHA / hCaptcha) is detected. Nothing is filled or submitted.

Usage:  python -m app.probe <url> [<url> ...]
"""

from __future__ import annotations

import sys


async def probe_one(url: str) -> dict:
    from playwright.async_api import async_playwright

    info = {"url": url}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3500)
            info["final_url"] = page.url
            info["title"] = (await page.title())[:70]

            frame_urls = " ".join(f.url for f in page.frames)
            body = ""
            try:
                body = (await page.inner_text("body"))[:2000].lower()
            except Exception:
                pass

            info["bot_challenge"] = any(
                k in (frame_urls + body)
                for k in ["challenges.cloudflare", "turnstile", "recaptcha", "hcaptcha", "security verification"]
            )
            info["has_password"] = await page.locator("input[type=password]").count() > 0
            info["num_fields"] = await page.locator("input, textarea, select").count()
            info["looks_login"] = info["has_password"] or any(
                k in (info["final_url"].lower() + body) for k in ["login", "sign in", "signin", "log in", "anmelden", "connexion"]
            )
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)[:120]
        finally:
            await browser.close()
    return info


def _classify(info: dict) -> str:
    if info.get("error"):
        return f"ERROR: {info['error']}"
    if info.get("bot_challenge"):
        return "🛑 BOT-CHALLENGE (Cloudflare/CAPTCHA) — not headless-automatable"
    if info.get("has_password") or (info.get("looks_login") and info.get("num_fields", 0) < 6):
        return "🔑 LOGIN required before the post form"
    if info.get("num_fields", 0) >= 4:
        return f"✅ FORM visible ({info['num_fields']} fields)"
    return f"❓ Unclear ({info.get('num_fields', 0)} fields) — inspect manually"


if __name__ == "__main__":
    import anyio

    for u in sys.argv[1:]:
        result = anyio.run(probe_one, u)
        print(f"\n▶ {u}")
        print(f"   ends at: {result.get('final_url', '—')}")
        print(f"   title:   {result.get('title', '—')}")
        print(f"   verdict: {_classify(result)}")
