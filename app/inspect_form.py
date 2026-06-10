"""Form inspector — derive real CSS selectors from a board's live posting form.

Board forms are often JavaScript-rendered, so static HTML fetches see nothing.
This loads the page in a real browser, waits for it to render, and dumps every
input/select/textarea with the attributes needed to build a field_map: id, name,
type, placeholder, and the visible label text associated with each field.

Usage:
    python -m app.inspect_form "https://www.innovatorsroom.com/add-job"

Read-only: it never fills or submits anything.
"""

from __future__ import annotations

import json
import sys


async def inspect(url: str) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Wait for the form to render rather than for the network to go idle
            # (analytics-heavy pages never reach networkidle).
            try:
                await page.wait_for_selector("input, textarea, select", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(2500)  # let late JS widgets settle
            fields = await page.eval_on_selector_all(
                "input, select, textarea, button, [type=submit]",
                """els => els.map(e => {
                    // Find a label: <label for=id>, wrapping <label>, or aria-label.
                    let label = '';
                    if (e.id) {
                        const l = document.querySelector(`label[for='${e.id}']`);
                        if (l) label = l.textContent.trim();
                    }
                    if (!label && e.closest('label')) label = e.closest('label').textContent.trim();
                    if (!label) label = e.getAttribute('aria-label') || e.getAttribute('placeholder') || '';
                    // For buttons, the visible text is the most useful label.
                    if (!label && e.tagName.toLowerCase() === 'button') label = e.textContent.trim();
                    return {
                        tag: e.tagName.toLowerCase(),
                        type: e.getAttribute('type') || '',
                        id: e.id || '',
                        name: e.getAttribute('name') || '',
                        placeholder: e.getAttribute('placeholder') || '',
                        text: (e.textContent || '').trim().slice(0, 40),
                        label: label.slice(0, 80),
                    };
                })""",
            )
            return [f for f in fields if f["type"] not in ("hidden",)]
        finally:
            await browser.close()


def _suggest_selector(f: dict) -> str:
    if f["id"]:
        return f"#{f['id']}"
    if f["name"]:
        return f"[name='{f['name']}']"
    if f["placeholder"]:
        return f"{f['tag']}[placeholder='{f['placeholder']}']"
    return f"{f['tag']}"


if __name__ == "__main__":
    import anyio

    if len(sys.argv) < 2:
        print("usage: python -m app.inspect_form <url>")
        raise SystemExit(1)

    fields = anyio.run(inspect, sys.argv[1])
    print(f"\nFound {len(fields)} visible fields:\n")
    for f in fields:
        sel = _suggest_selector(f)
        label = f["label"] or "(no label)"
        print(f"  {label:42}  {f['tag']}/{f['type'] or '—':10}  →  {sel}")
    print("\nStarter field_map skeleton (assign each master field to a selector):")
    skeleton = {f"FIELD_{i}": _suggest_selector(f) for i, f in enumerate(fields)}
    print(json.dumps(skeleton, indent=2))
