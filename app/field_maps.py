"""Derived field maps + login flows, applied by the seed.

These are discovered by inspecting each board's live form (see app/inspect_form
and app/login_inspect). Keeping them here — rather than only in the database —
means seeding a fresh environment (e.g. Railway Postgres) reproduces all the
mapping work, and the maps are reviewable in version control.

Each entry maps master fields (app/posting/base.py) to that board's selectors.
Auth boards include a "login" block consumed by app/posting/playwright_auth.py.

NOTE: a board without a confirmed "submit" selector will be *filled but not
submitted* by the engine. Submit selectors are added per board only once
verified, so nothing is posted blindly.
"""

from __future__ import annotations

FIELD_MAPS: dict[str, dict] = {
    # ── Playwright simple (no login) ──
    "Innovators Room": {
        "company_name": "#form1-co_name0_input-704236701",
        "title": "#form1-job_role0_input-1979932259",
        "city": "#form1-location0_input--76995642",
        "apply_url": "#form1-url0_input--1336999796",
        "contact_email": "#form1-partner_email-893710213",
        "contact_name": "#form1-partner_name-860370306",
        "submit": {"selector": "button:has-text('Submit job for free')", "type": "click"},
    },
    "MIT Orbit": {
        "contact_name": "#Name",
        "contact_email": "#Contact",
        "company_name": "#Company",
        "title": "#Role",
        "company_description": "[id='Company Description']",
        "description_plain": "#Description",
        "apply_url": "#URL",
        "submit": {"selector": "button:has-text('Submit')", "type": "click"},
    },
    # ── Playwright authenticated ──
    "Conservation Job Board": {
        # Login confirmed working with stored credentials.
        "login": {
            "username": "[name='email']",
            "password": "#password",
            "submit": "button:has-text('Sign in')",
        },
        "contact_first_name": "[name='first_name']",
        "contact_last_name": "[name='last_name']",
        "contact_email": "input[type=email]",
        "company_name": "[name='employer_name']",
        "title": "[name='name']",
        "city": "[name='city']",
        "salary_min": "[name='salary_min']",
        "salary_max": "[name='salary_max']",
        "description_plain": "#mce_0",
        # submit: 3 unlabeled buttons on the form — needs confirmation before go-live.
    },
    "KTH": {
        # PowerApps local-account login (confirmed working). Internship/degree-
        # project board only. Selects + date pickers still need select_map +
        # date handling, and the #InsertButton submit, before live posting.
        "login": {
            "username": "#Email",
            "password": "#PasswordValue",
            "submit": "#submit-signin-local",
        },
        "company_name": "#aca_organization_name",
        "contact_name": "#aca_contact_name",
        "title": "#aca_name",
        "city": "#aca_location",
        "contact_email": "#aca_applicationemail",
        "apply_url": "#aca_applicationurl",
        "description_plain": "#aca_description",
        # submit (#InsertButton) intentionally omitted until selects/dates handled.
    },
    "Climate career Portal": {
        # Form fields render pre-login; submit requires a logged-in session
        # (login is a modal — flow to be configured).
        "title": "[name='jobTitle']",
        "company_name": "[name='companyName']",
        "description_plain": "[name='jobDescription']",
        "country": "[name='locationCountry']",
        "city": "[name='locationCity']",
        "apply_url": "[name='jobPostingLink']",
        "submit": {"selector": "button:has-text('Submit job posting')", "type": "click"},
    },
}

SELECT_MAPS: dict[str, dict] = {
    # Filled in per board as dropdown option values are confirmed.
}

# Boards where the *posting* form is protected by a CAPTCHA / bot-challenge and
# therefore cannot be submitted headlessly — surfaced to operators, handled via
# assisted mode or manually. (Login may still succeed; submission is the blocker.)
BOT_PROTECTED = {
    "KU Leuven - Alumni": "Google reCAPTCHA on the job-offer form",
    "EU Startups": "Cloudflare Turnstile on the post-a-job page",
    "UniAgro network": "Server returns HTTP 403 to automated browsers",
    "Tech Uni Munich": "Requires TUM company-account registration before posting",
}
