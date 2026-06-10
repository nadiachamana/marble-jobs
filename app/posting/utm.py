"""UTM apply-URL resolution (R-11).

Per the plan, at dispatch the engine opens the board's UTM-tagged Ashby tracker
page, finds the matching job posting, and extracts the job-specific URL (which
carries the utm_source). That tracked URL becomes the `apply_url` filled into
the board's form.

Title text on the public board can differ slightly from the Ashby title (e.g.
"Co-Founder, CTO (Next-Gen Geothermal)" vs "...CTO, Next-Gen Geothermal"), so
matching is fuzzy. If scraping can't find the job, we fall back to constructing
the URL directly from the known job-posting id, which is robust.
"""

from __future__ import annotations

import re

from app.config import get_settings

settings = get_settings()


def _normalize(text: str) -> str:
    """Lowercase, drop bracketed/parenthetical bits and punctuation for matching."""
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    """Token-overlap (Jaccard) similarity between two normalized titles."""
    ta, tb = set(_normalize(a).split()), set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def construct_apply_url(job, utm_source: str | None) -> str:
    """Public sync helper: build the UTM apply URL without a browser.

    Used by assisted mode (a human is present to verify) and as the scrape
    fallback.
    """
    return _construct_url(job, utm_source)


def _construct_url(job, utm_source: str | None) -> str:
    """Direct fallback: {board_base}/{jobPostingId}?utm_source={code}."""
    base = settings.ashby_job_board_base.rstrip("/")
    url = f"{base}/{job.ashby_job_posting_id}" if job.ashby_job_posting_id else (job.apply_url or base)
    if utm_source:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}utm_source={utm_source}"
    return url


async def resolve_apply_url(job, board) -> str:
    """Return the per-board UTM-tracked apply URL for `job`.

    Tries to scrape the board's tracker page; falls back to direct construction.
    """
    tracker = board.ashby_tracker_url
    utm = board.utm_source

    if not tracker:
        return _construct_url(job, utm)

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(tracker, wait_until="networkidle", timeout=30000)
                anchors = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: e.textContent.trim()}))",
                )
            finally:
                await browser.close()

        best, best_score = None, 0.0
        for a in anchors:
            text = a.get("text") or ""
            href = a.get("href") or ""
            # Ashby job links look like .../marble/<uuid>
            if not text:
                continue
            score = _similarity(job.title, text)
            if job.ashby_job_posting_id and job.ashby_job_posting_id in href:
                score = 1.0  # exact id match in the URL wins outright
            if score > best_score:
                best, best_score = href, score

        if best and best_score >= 0.5:
            if utm and "utm_source=" not in best:
                sep = "&" if "?" in best else "?"
                best = f"{best}{sep}utm_source={utm}"
            return best
    except Exception:
        # Any scraping failure -> robust fallback rather than blocking dispatch.
        pass

    return _construct_url(job, utm)
