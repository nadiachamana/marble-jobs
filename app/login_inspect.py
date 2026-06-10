"""Log into an authenticated board with stored credentials, then inspect the
post form behind the login (read-only — never submits a job).

Login configs are keyed by board name. Credentials come from env/secrets via
the board's credentials_ref, never hardcoded. Single login attempt only, to
avoid locking real accounts.

Usage:  python -m app.login_inspect "KU Leuven - Alumni"
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.db import SessionLocal
from app.models import BoardConfig

settings = get_settings()

# Per-board login flow, discovered by inspecting each login page.
LOGIN_CONFIG = {
    "KU Leuven - Alumni": {
        "username": "#email",
        "password": "#passwordInput",
        "submit": "button:has-text('Log in')",
    },
    "Conservation Job Board": {
        "username": "[name='email']",
        "password": "#password",
        "submit": "button:has-text('Sign in')",
    },
    "KTH": {
        # PowerApps local-account login (not the Azure AD SSO button).
        "username": "#Email",
        "password": "#PasswordValue",
        "submit": "#submit-signin-local",
    },
}


async def login_and_inspect(board_name: str) -> None:
    s = SessionLocal()
    board = s.query(BoardConfig).filter_by(name=board_name).one()
    cfg = LOGIN_CONFIG.get(board_name)
    if not cfg:
        print(f"No login config for {board_name!r}")
        return
    username, password = settings.board_credentials(board.credentials_ref or "")
    if not (username and password):
        print(f"Missing credentials for {board_name!r}")
        return

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Going to the post URL bounces us to the login page.
            await page.goto(board.post_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

            if await page.locator(cfg["password"]).count() == 0:
                print(f"[{board_name}] No password field found on the login page (may be SSO/multi-step).")
                print(f"   landed at: {page.url}")
                return

            await page.fill(cfg["username"], username)
            await page.fill(cfg["password"], password)
            await page.click(cfg["submit"])
            await page.wait_for_timeout(5000)
            print(f"[{board_name}] after login → {page.url}")

            # Try to reach the post form again now that we're authenticated.
            await page.goto(board.post_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3500)

            still_login = await page.locator("input[type=password]").count() > 0
            if still_login:
                print(f"[{board_name}] Still on a login/password page — login likely failed or needs MFA.")
                return

            fields = await page.eval_on_selector_all(
                "input, select, textarea, button",
                """els => els.map(e => ({
                    tag: e.tagName.toLowerCase(),
                    type: e.getAttribute('type') || '',
                    id: e.id || '',
                    name: e.getAttribute('name') || '',
                    ph: e.getAttribute('placeholder') || '',
                    text: (e.textContent||'').trim().slice(0,30),
                })).filter(f => f.type !== 'hidden')""",
            )
            print(f"[{board_name}] POST FORM — {len(fields)} fields:")
            for f in fields:
                sel = f"#{f['id']}" if f["id"] else (f"[name='{f['name']}']" if f["name"] else f["tag"])
                label = f["ph"] or f["text"] or "(no label)"
                print(f"    {label:38} {f['tag']}/{f['type'] or '—':8} → {sel}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{board_name}] ERROR: {exc}")
        finally:
            await browser.close()


if __name__ == "__main__":
    import anyio

    anyio.run(login_and_inspect, sys.argv[1] if len(sys.argv) > 1 else "Conservation Job Board")
