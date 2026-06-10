# Marble — Job Board Automation

Automatic job posting from Ashby to any job board, with a human in the loop.

When a job is published in Ashby, this app fetches it, infers the metadata boards
ask for, and presents a one-screen review form. You confirm a few fields, pick
boards (smart-defaulted), and click **Dispatch**. The posting engine then submits
to each board via the right mechanism: Playwright (simple), Playwright
(authenticated), or email. Status updates post back to Slack — threaded under the
job's original message.

**Core principle: a board is a row, not a function.** Onboarding a board is
filling in a form (or clicking *Auto-map from URL*), never a code change.

---

## Architecture

```
Ashby ──jobPostingPublish webhook──▶ /webhooks/ashby
                                        │  verify HMAC signature
                                        │  jobPosting.info fetch
                                        │  inference: seniority/category/tags/salary/city
                                        ▼
                                     Postgres/SQLite  (status: pending)
                                        │  Slack (threaded) + email
                                        ▼
                          Review dashboard  /jobs/{id}
                                        │  confirm fields + select boards → Dispatch
                                        ▼
                          Dispatcher (background)
                            ├─ resolve per-board UTM apply URL
                            ├─ playwright_simple / playwright_auth / email
                            ├─ paid → skipped · bot-protected → assisted mode
                            └─ live per-board status, screenshots, retries
                                        │
                          Slack thread: 🏁 Dispatch complete — N posted · N failed · N skipped
```

| Layer | Files |
|---|---|
| Webhook + inference + persistence | `app/routes/webhook.py`, `app/inference.py`, `app/ashby.py` |
| Data model | `app/models.py` |
| Review dashboard + onboarding UI | `app/routes/dashboard.py`, `app/templates/` |
| Dispatch + posting engines | `app/posting/` |
| Auto-mapper (URL → field_map) | `app/automap.py` |
| Assisted mode (CAPTCHA/Cloudflare) | `app/assist.py` |
| Threaded Slack + email | `app/notify.py` |
| Board seed + derived maps | `app/seed.py`, `app/field_maps.py`, `boards_seed.csv` |

---

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # one-time browser download

cp .env.example .env                 # then fill in secrets (see below)
python -m app.seed                   # load the boards
./run.sh                             # or: uvicorn app.main:app --reload
```

Open <http://localhost:8000> — job queue at `/`, board library at `/boards`.

### Secrets (`.env`, gitignored)

| Variable | What it is |
|---|---|
| `ASHBY_API_KEY` | Ashby API key with **jobsRead** (for `jobPosting.info`) |
| `ASHBY_WEBHOOK_SECRET` | Signing secret on the `jobPostingPublish` webhook |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) with `chat:write` — **required for threaded updates**; invite the bot to the channel |
| `SLACK_CHANNEL` | Channel ID (e.g. `C03FBP45BFZ`) |
| `SMTP_*` | Optional mail server for review alerts + email-type boards |

Board logins for authenticated boards come from env vars keyed by the board's
`credentials_ref`: `BOARD_<REF>_USERNAME` / `BOARD_<REF>_PASSWORD` (e.g.
`BOARD_KU_LEUVEN_USERNAME`). Locally these are generated into
`secrets/board_credentials.env` (gitignored) by `python -m app.seed` from the
full CSV. Passwords are never stored in the database.

> The full `job-boards-airtable.csv` (with passwords) is **gitignored**. A
> sanitized `boards_seed.csv` (no credentials) is committed and used to seed
> deployed environments; credentials there come only from env vars.

---

## Onboarding a board — just a URL

Go to **/boards/new**, enter the name + post URL (+ `credentials_ref` for login
boards), Save, then click **⚡ Auto-map from URL**. The auto-mapper renders the
live form, matches fields to the master schema, finds the submit button, and
fills in the `field_map` for you to review. Curated maps also live in
`app/field_maps.py` and are applied by the seed so fresh deploys reproduce them.

A board without a confirmed `submit` selector is *filled but not submitted* — the
engine never reports success without submitting, so nothing posts blindly.

### Master fields
`title`, `description_html`, `description_plain`, `company_description`,
`company_name`, `apply_url` (UTM-resolved per board), `work_mode`, `country`,
`city`, `employment_type`, `deadline`, `contact_email`, `contact_name`,
`seniority`, `function_category`, `industry_tags`, `salary[_min/_max/_currency]`.

### Bot-protected / paid boards
Boards behind CAPTCHA/Cloudflare (or paid) are flagged and skipped on auto-dispatch.
Post to them with **assisted mode** — a local visible browser that auto-logs-in
and auto-fills, then you solve the CAPTCHA and submit:

```bash
python -m app.assist <job_id> "<Board Name>"
```

---

## Deploy to Railway

1. Connect the GitHub repo to a Railway project. Railway builds the `Dockerfile`
   (Playwright image — Chromium + deps preinstalled).
2. Add the **PostgreSQL** plugin → Railway sets `DATABASE_URL` automatically
   (the app normalizes `postgres://` → `postgresql://`).
3. Set variables: `ASHBY_API_KEY`, `ASHBY_WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`,
   `SLACK_CHANNEL`, `APP_BASE_URL=https://<your-app>.up.railway.app`, plus the
   `BOARD_*_USERNAME` / `BOARD_*_PASSWORD` pairs for authenticated boards.
4. Deploy. Tables auto-create and boards **auto-seed on first boot** (when empty).
5. Point the Ashby `jobPostingPublish` webhook at
   `https://<your-app>.up.railway.app/webhooks/ashby`.

The same code runs on SQLite locally and Postgres in production — only
`DATABASE_URL` differs.

---

## Status

P0 + most P1 implemented: webhook receiver, inference, persistence, threaded
Slack + email, review dashboard, smart board selector, all three posting engines,
UTM resolution, per-board status tracking, retries, failure screenshots,
URL-only auto-mapping, and assisted mode. Submit selectors are confirmed per
board before live posting; paid boards stay manual.
