# Marble Job Board Automation — Project Handoff

**Read this first before touching the project.** It captures context, the
non-obvious gotchas, every error we hit + its fix, current state, and future
work. Written 2026-06 after building v1 + the v2 schema rewrite.

---

## 1. What this is

Automates posting Marble's Ashby jobs to 200+ job boards through a
**webhook-triggered, human-in-the-loop** dashboard. When a job is published in
Ashby, the app fetches it, infers the metadata boards ask for, and shows a
one-screen review form. Nadia confirms fields, picks boards, clicks **Dispatch**;
the posting engine submits to each board via Playwright (simple / authenticated)
or email. Status posts back to Slack, threaded under the job's message.

**Core principle: a board is a row, not a function.** Onboarding a board = filling
a form (or clicking *Auto-map from URL*), never a code change.

- **Local repo:** `/Users/nadia/Documents/dashboard-JB`
- **GitHub:** `https://github.com/nadiachamana/marble-jobs` (branch `main`)
- **Production:** Railway → `https://marble-jobs.up.railway.app` (auto-deploys on push to `main`)
- **User:** Nadia Chamana (nadia@marble.studio), non-engineer — explain clearly, she drives Railway/Slack/Ashby dashboards herself.

---

## 2. Stack

| Layer | Choice |
|---|---|
| Web | FastAPI + Jinja2 templates (no separate frontend) |
| DB | SQLAlchemy — **SQLite locally, Postgres on Railway** (same code, only `DATABASE_URL` differs) |
| Browser automation | Playwright (Python, Chromium) |
| LLM | Anthropic SDK, model **`claude-opus-4-8`** (inference + form classification) |
| Notifications | Slack **bot token** (`chat.postMessage`, threaded) + optional SMTP email |
| Deploy | Railway via **Dockerfile** (`mcr.microsoft.com/playwright/python:v1.49.1-noble` — Chromium preinstalled) |

---

## 3. ⚠️ MANDATORY GOTCHAS (these will bite you)

### 3.1 Two virtualenvs exist
- `.venv` — the primary, has all deps. **Use this for dev.**
- `venv` — the one Nadia activates in her shell; also has deps now.
- Both work. If you add a dependency, install into **both** (`pip install` then repeat for the other) or her shell breaks with `ModuleNotFoundError`.

### 3.2 macOS system proxy is misconfigured → blocks outbound network
Her Mac has a broken system proxy (`127.0.0.1:8080`). **Every** outbound command must bypass it:
- Python/app commands: prefix `HTTPS_PROXY="" HTTP_PROXY="" no_proxy="*"`
- `curl`: add `--noproxy '*'`
- `pip`: add `--proxy ""`
- httpx inside the app is fine at runtime (doesn't read macOS system proxy), so Slack/Ashby/Anthropic calls work in production and locally without special handling — it's only the *shell tools* (pip, curl, and Playwright launching) that need the bypass.

### 3.3 Running locally
- `./run.sh` (handles proxy + seeds if empty) → `http://127.0.0.1:8000`
- Or `HTTPS_PROXY="" no_proxy="*" .venv/bin/python -m app.main`
- The server runs in the **foreground of whatever launched it** — it dies when the terminal/session closes. There is no persistent local daemon; persistence = Railway.
- **Kill stale servers:** macOS `pkill -f` with regex alternation (`\|`) silently fails. Use `lsof -ti:8000 | xargs kill -9` then `pkill -9 -f "app.main"`.

### 3.4 The app launches via `python -m app.main`, NOT `uvicorn --port $PORT`
Railway ran the start command without a shell, so `$PORT` arrived literally and uvicorn 400'd. `app/main.py` has an `if __name__ == "__main__"` block that reads `PORT` from the env in Python and calls `uvicorn.run`. Dockerfile + Procfile both call `python -m app.main`. **Don't revert to a shell `$PORT` start command.**

### 3.5 Playwright in the Railway container needs `--no-sandbox`
Chromium can't launch in the container without it → caused a 500 on every Auto-map. All production launches use `chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])`. If you add a new browser launch, include these args.

### 3.6 Anthropic API intermittently returns 529 (overloaded)
Transient, Anthropic-side. The SDK retries (`max_retries=4`) and inference/automap **fall back to rules** on failure, so it degrades rather than crashes. Don't treat a 529 in testing as a code bug — retry.

### 3.7 Anthropic returns NESTED keys
The schema uses dotted keys (`classification.seniority`). Claude returns them **nested** (`{"classification": {"seniority": ...}}`). `inference._validate_inferred` and the classifier flatten one level. Keep that flattening if you touch the prompts.

---

## 4. Secrets & configuration

### 4.1 What's gitignored (NEVER commit)
`.env`, `secrets/`, `job-boards-airtable.csv` (has board passwords in Username/Password columns), `data/`, `*.db`, `*.pdf`, `.claude/`. A **sanitized** `boards_seed.csv` (no passwords) **is** committed and used to seed deployed environments. `.dockerignore` keeps secrets out of the image.

### 4.2 Environment variables (set in Railway → web service → Variables; locally in `.env`)
| Var | Notes |
|---|---|
| `DATABASE_URL` | Railway: reference Postgres internal — `postgresql://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}`. Local: `sqlite:///./data/marble.db`. Blank → app falls back to SQLite (don't rely on it in prod). |
| `ASHBY_API_KEY` | jobsRead permission (for `jobPosting.info`) |
| `ASHBY_WEBHOOK_SECRET` | HMAC signing secret on the `jobPostingPublish` webhook |
| `ASHBY_JOB_BOARD_BASE` | `https://jobs.ashbyhq.com/marble` |
| `SLACK_BOT_TOKEN` | `xoxb-…` bot **"NadIA"**. Webhooks can't thread — must be a bot token. |
| `SLACK_CHANNEL` | **`C03FBP45BFZ`** = channel **`#_talent-ops`** (note the LEADING UNDERSCORE). Use the ID, not the name. The bot must be invited to the channel (`/invite @NadIA`). |
| `SLACK_WEBHOOK_URL` | legacy fallback, can't thread |
| `ANTHROPIC_API_KEY` | required for Claude inference/classification; without it everything falls back to rules |
| `CLAUDE_MODEL` | defaults to `claude-opus-4-8` |
| `APP_BASE_URL` | `https://marble-jobs.up.railway.app` (used in Slack/email links) |
| `MARBLE_CONTACT_EMAIL/NAME/PHONE` | `hiring@marble.studio` / `Nadia Chamana` / `+33749945048` |
| `SMTP_*`, `NOTIFY_EMAIL_TO` | email is OPTIONAL and currently NOT configured |
| `ASSIST_DATABASE_URL` | **local only** — Railway Postgres PUBLIC url so the local assist tool reads real prod jobs. Format `postgresql://postgres:<pw>@<host>.proxy.rlwy.net:<port>/railway`. The PUBLIC host needs the Postgres TCP proxy enabled (Railway → Postgres → Settings → Networking). Common mistake: putting the proxy *port* where the *host* goes. |
| `BOARD_<REF>_USERNAME` / `BOARD_<REF>_PASSWORD` | per authenticated board (see §6) |

---

## 5. Architecture & flow

```
Ashby jobPostingPublish webhook
   └─ POST /webhooks/ashby  (app/routes/webhook.py)
        verify HMAC (ASHBY_WEBHOOK_SECRET) → 200 fast
        background: jobPosting.info fetch → inference (rule fields + infer_canonical)
        persist JobQueue (status=pending) → Slack PARENT message (store slack_ts)
                                  │
   Review dashboard  GET /jobs/{id}  (app/routes/dashboard.py + templates/review.html)
        confirm 6 fields · select boards (smart defaults) · Dispatch
        PRE-FLIGHT: app/validation.py blocks dispatch if a board is missing a required field
                                  │
   Dispatcher (background)  app/posting/dispatcher.py
        per board: resolve UTM apply URL → engine → live status
          • playwright_simple / playwright_auth / email
          • paid boards → skipped (manual);  bot-protected → skipped (assisted mode)
        Slack THREADED summary under the parent (notify_dispatch_complete)
```

**Posting engines** (`app/posting/`): `fill_form` (in `base.py`) supports field types
`fill | select | check | click | richtext | react_select`. `richtext` fills a
TinyMCE/CKEditor iframe (`#mce_N_ifr`). Engines **refuse to report success without a
confirmed submit selector**. Per-field timeout 8s (so a bad selector skips fast).

**UTM** (`app/posting/utm.py`): resolves the per-board tracked apply URL by scraping the
board's Ashby tracker page (fuzzy title match) with a fallback that constructs
`{board_base}/{jobPostingId}?utm_source={code}`.

---

## 6. Authenticated boards & credentials (IMPORTANT — frequently misunderstood)

- **`credentials_ref`** on a board is a **label** you invent (e.g. `ku-leuven`), NOT a username or password.
- It maps to Railway env vars: `credentials_ref=ku-leuven` → `BOARD_KU_LEUVEN_USERNAME` / `BOARD_KU_LEUVEN_PASSWORD` (`config.board_credentials()`; `board_env_status()` lists the expected names; the board form shows them).
- **Login selectors** (which boxes to type into, which button to click) live in `field_map["login"] = {url, username, password, submit}` — these are **CSS selectors**, edited in the board form's "Login (authenticated boards)" section, OR hardcoded in `automap.LOGIN_CONFIG` for KU Leuven / Conservation / KTH. The auto-mapper reads the board's own login block first, then falls back to `LOGIN_CONFIG`.
- **Workflow to onboard a NEW auth board:** (1) add board, `distribution_type=playwright_auth`, set `credentials_ref`; (2) add `BOARD_<REF>_USERNAME/_PASSWORD` in Railway; (3) fill the Login section selectors; (4) click Auto-map → it logs in then maps.
- Credentials NEVER touch the DB or the form — only the label does. Local creds live in gitignored `secrets/board_credentials.env` (regenerated by `python -m app.seed` from the full CSV).
- **`secrets/board_credentials.env` is loaded into env at startup** by `config._load_board_credentials()`.

---

## 7. The v2 master field schema (the big rewrite — see master_field_schema.md + implementation_brief.md in ~/Downloads)

The v1 schema had ~16 flat fields; boards need ~80. v2 replaces it with a declared registry.

- **`app/schema.py`** — single source of truth. **99 canonical fields, 13 namespaces**
  (`job / classification / location / apply / compensation / dates / company / contact /
  invoice / media / board_config / seo / freetext`), each with `type`, `source`
  (auto/inferred/static/manual/board_config), `controlled_vocab`, `default`, `enum`, `aliases`.
  28 controlled vocabularies. `SCHEMA_VERSION = 2`.
  - **`LEGACY_ALIASES`** bridges old flat keys (`title`) → canonical (`job.title`). Existing
    field_maps keep working. `resolve_key()` accepts either form. **This is why nothing broke.**
  - **`SchemaExtension`** table + `apply_extensions()` (called at startup) merge operator-approved
    new fields into the registry — the schema grows with no code change.
  - Run `python -m app.schema` to self-validate.
- **`app/llm.py`** — Anthropic client wrapper. `claude-opus-4-8`, prompt-cached system, `max_retries=4`, `complete_json()` returns parsed JSON (strips fences). `llm.available()` gates on the API key.
- **`inference.infer_canonical(job)`** — produces the full canonical payload keyed by schema keys:
  - AUTO (from Ashby job fields, with Ashby enum → canonical normalization),
  - STATIC (Marble constants from schema defaults),
  - INFERRED (one Claude call, enum-constrained, nested-key flattened, validated),
  - **rule-based fallback** when no key / on error. Stored on `JobQueue.canonical` at enrich time.
  - The legacy rule functions (`infer_seniority`, `infer_all`, etc.) still exist and are used as the fallback.
- **`app/automap.py`** — discover → classify → propose → report:
  - **discover** (`inspect_form` + `_EVAL` JS): every control with widget type
    (`native_select / react_select / radio / checkbox / file / rich_text / button`),
    options, required-ness. Handles login + bot-challenge detection.
  - **classify** (`classify_with_claude`): sends the whole schema + controls, Claude returns
    `mappings` (selector → canonical key + `value_map`) + `new_field_proposals`.
  - **build** (`build_from_classification`): field_map (canonical keys + fill types), select_map
    (inverted `value_map` = `{canonical_value: board_label}`), coverage report, proposals, required_fields.
  - **Heuristic `propose_field_map`** remains as the fallback when Claude is unavailable.
- **`app/validation.py`** — `missing_required(job, board)` blocks dispatch with a review-screen
  message instead of a failed submit. `board_ready(board)` gates "active" status.
- **`build_master_fields`** (`posting/base.py`) now exposes every value under BOTH flat and
  canonical keys (so legacy and v2 field_maps both fill), normalizes Ashby enums, honors
  `board.name_format` (KTH "First Last" vs FR "Last First").

**New DB columns (auto-migrated by `db._add_missing_columns` — `create_all` won't ALTER existing tables):**
- `BoardConfig`: `required_fields`, `board_config`, `name_format`, `field_notes`, `coverage`, `schema_version`
- `JobQueue`: `slack_ts`, `canonical`
- new table `schema_extension`

**Dashboard v2 UI** (`templates/board_form.html`): coverage banner, new-field proposal cards
(Approve → SchemaExtension / Board-only / Ignore), credentials env-var display, name-format
selector, region/field selectors, login-config section, manual live-posting-URL input.

> **Reconciliation caveat:** `automap` writes field_maps to the **DB**. There's also a curated
> `app/field_maps.py` applied by the seed (so a fresh Railway deploy reproduces maps). After
> auto-mapping boards in the UI, those maps live only in the DB — to make them survive a DB reset,
> export them back into `app/field_maps.py`. **This is unfinished and important.**

---

## 8. Assisted mode (for CAPTCHA / Cloudflare / paid / complex boards)

`app/assist.py` — runs **LOCALLY** (visible browser, can't run headless on Railway). We will **NOT**
build CAPTCHA/Cloudflare bypass (ToS/security). Flow: open browser → human clears CAPTCHA/login
and opens the form → press Enter → auto-fill → human completes + submits → confirm → records
attempt + threaded Slack update. Fetches the full JD live from Ashby if the job lacks one.

- Reads from the prod DB when `ASSIST_DATABASE_URL` is set (so it acts on real jobs).
- `python -m app.assist <job_id>` lists boards; `python -m app.assist <job_id> "Board Name"` runs it.
- **Why it must be local:** Railway has no screen; the human solves the CAPTCHA.

---

## 9. Board status (13 seeded + Imperial in progress)

| Board | Status |
|---|---|
| Innovators Room | ✅ mapped + submit (playwright_simple) |
| MIT Orbit | ✅ mapped + submit (playwright_simple) |
| Climate career Portal | ✅ mapped + submit (playwright_auth; login is a modal) |
| Conservation Job Board | 🔧 login works, 11 fields, TinyMCE description/apply mapped; **needs submit selector confirmed** |
| KTH | 🔧 login works (PowerApps), 7 fields; **needs select_maps + submit + date handling** |
| KU Leuven - Alumni | 🙋 login works but post form has **Google reCAPTCHA** → assisted mode |
| EU Startups | 🙋 **Cloudflare Turnstile** → assisted mode |
| UniAgro network | 🙋 returns **HTTP 403** to bots → assisted mode |
| Tech Uni Munich | 🙋 needs **company-account registration** → assisted mode |
| Climate Job List | 💳 PAID → skipped (manual) |
| Startup & VC | 💳 PAID → skipped |
| International Women in Mining | 💳 PAID + ✉️ email board |
| Geothermal Rising | ✉️ email board (free) |
| Imperial (targetconnect) | 🚧 IN PROGRESS — login selectors known (`input[name="user.username"]`, `input[name="user.password"]`, `button[data-cy="login-submit"]`, login at `https://imperial.targetconnect.net/employer/`). Needs `BOARD_IMPERIAL_*` in Railway. ⚠️ Don't use a post URL with `?execution=…` — that's a temporary Spring WebFlow token that expires. |

Auth login selectors (in `automap.LOGIN_CONFIG` / `login_inspect.LOGIN_CONFIG`):
- KU Leuven: `#email` / `#passwordInput` / `button:has-text('Log in')`
- Conservation: `[name='email']` / `#password` / `button:has-text('Sign in')`
- KTH: `#Email` / `#PasswordValue` / `#submit-signin-local`

---

## 10. Debugging history — errors hit & fixes (DON'T re-discover these)

1. **CSV BOM** — Airtable export has a UTF-8 BOM; read with `encoding="utf-8-sig"`.
2. **Starlette 1.2 TemplateResponse** — signature is `TemplateResponse(request, name, context)`, not `(name, {"request":..})`.
3. **macOS proxy** — see §3.2.
4. **SQLite dir** — `data/` must exist before the engine connects (db.py mkdirs it).
5. **`networkidle` never settles** on analytics-heavy pages → use `domcontentloaded` + `wait_for_selector`.
6. **JS-rendered forms** — most board forms render client-side; static HTML fetch sees 0 inputs. Must use Playwright.
7. **Slack webhooks can't thread** — switched to bot token + `chat.postMessage`, store the parent `ts` on `JobQueue.slack_ts`, post updates with `thread_ts`.
8. **Slack channel** is `#_talent-ops` (leading underscore); `chat.postMessage` needs the channel **ID** `C03FBP45BFZ` and the bot must be invited.
9. **Railway blank `DATABASE_URL`** crashed engine creation → config coerces blank → SQLite.
10. **Railway `$PORT`** literal → launch via `python -m app.main` (reads PORT in Python).
11. **Railway DATABASE_URL reference** — user deleted the Postgres `DATABASE_URL` var; compose from `${{Postgres.PGUSER}}…RAILWAY_PRIVATE_DOMAIN…` instead.
12. **Playwright 500 on Railway** — Chromium needs `--no-sandbox --disable-dev-shm-usage` in the container.
13. **Auto-map route 500s** — now wrapped in try/except → shows the error in the UI + traceback in logs.
14. **`querySelector` SyntaxError** — targetconnect ids contain quotes/brackets (`stringExample['…']`); the discovery JS now `CSS.escape`s the id + try/catch; `_attr_sel` builds field_map selectors with a quote style that survives quotes.
15. **Bare `"input"` selector collisions** — when a field has no id/name the mapper used to emit `"input"`, colliding many fields on the first box; now skips unaddressable fields.
16. **TinyMCE not filling** — rich-text editors are iframes; added `richtext` type filling `#mce_N_ifr` body.
17. **fill_form aborting on first bad field** — now collects failures and continues (so one missing field doesn't block the rest).
18. **Two venvs** — `pydantic`/`anthropic` missing in `venv`; install into both.
19. **Anthropic 529** — retries + rule fallback.
20. **Claude nests dotted keys** — flatten in the validator.
21. **`run_automap` form param shadowed the `automap` module** — aliased the form field.
22. **Form param shadow / pkill regex / port glob** — minor shell/Python footguns documented above.
23. **`ASSIST_DATABASE_URL` host** — the proxy *port* was given where the *host* belongs; needs `<host>.proxy.rlwy.net:<port>`.

---

## 11. NOT built yet / future work (priority order)

1. **Reconcile DB field_maps → `app/field_maps.py`** so UI-auto-mapped boards survive a DB reset / fresh deploy. (Important — see §7 caveat.)
2. **Finish Conservation + KTH for headless go-live:** confirm submit selectors, fill dropdown `select_map` values, KTH date handling. Re-run Auto-map on the auth boards to get v2 coverage.
3. **Imperial (targetconnect):** add `BOARD_IMPERIAL_*` to Railway, use a stable post URL, test login→map→post. Likely needs assisted mode if login is multi-step/CSRF.
4. **One-pager PDF generator** (`media.attachment_pdf`) for email/PRO/FR boards — reusable win, not built. (pdf/docx skills available.)
5. **Canonical-aware email-board renderer** — current `email_engine.py` works but uses flat fields + a fixed template; the v2 design renders from canonical fields.
6. **Multi-step/SSO logins** (KTH PowerApps, KU Leuven JobTeaser, targetconnect) may never headless cleanly → assisted mode.
7. **`on_event` deprecation** — FastAPI wants lifespan handlers instead of `@app.on_event("startup")` (warning only).
8. **Per-board `required_fields`** are only populated when Claude classify runs; legacy boards have `[]` (no blocking). Re-map to populate.

---

## 12. Useful commands (all need the proxy-bypass prefix locally)

```bash
./run.sh                                   # start local server (http://127.0.0.1:8000)
python -m app.seed                         # seed/refresh boards from CSV + apply field_maps.py
python -m app.schema                       # self-validate the canonical schema
python -m app.inspect_form "<url>"         # dump a form's real selectors (renders JS)
python -m app.probe "<url>"                # classify a board URL (login/bot-challenge/open)
python -m app.login_inspect "Board Name"   # log in with stored creds, inspect the post form
python -m app.automap "Board Name" [--save]# auto-map a board (Claude classify + report)
python -m app.assist <job_id> ["Board"]    # assisted (visible-browser) posting; set ASSIST_DATABASE_URL for prod jobs
```

Health/verify production:
```bash
curl -s --noproxy '*' https://marble-jobs.up.railway.app/health   # {"status":"ok"}
```

---

## 13. Key files

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI app, startup (init_db, auto-seed, apply_extensions), `python -m` launcher |
| `app/config.py` | env settings, blank-DB→sqlite coercion, board credential/env helpers |
| `app/db.py` | engine, `init_db`, `_add_missing_columns` auto-migration |
| `app/models.py` | BoardConfig, JobQueue, PostingAttempt, SchemaExtension |
| `app/schema.py` | **v2 canonical schema** + LEGACY_ALIASES + SchemaExtension merge |
| `app/llm.py` | Anthropic client wrapper |
| `app/inference.py` | rule inference + `infer_canonical` (Claude) |
| `app/automap.py` | discover→classify(Claude)→propose→report + heuristic fallback + LOGIN_CONFIG |
| `app/validation.py` | pre-flight required-field checks |
| `app/seed.py` | seed board_config from CSV + `field_maps.py` |
| `app/field_maps.py` | curated per-board maps + login flows + BOT_PROTECTED (applied by seed) |
| `app/assist.py` | assisted (human-in-the-loop) posting |
| `app/notify.py` | threaded Slack (`_post_slack`, `notify_*`) + email |
| `app/posting/base.py` | `build_master_fields`, `fill_form` (all field types) |
| `app/posting/{playwright_simple,playwright_auth,email_engine}.py` | the 3 engines |
| `app/posting/dispatcher.py` | orchestration, paid/assist skip, threaded summary |
| `app/posting/utm.py` | per-board UTM apply-URL resolution |
| `app/routes/{webhook,dashboard,dispatch}.py` | endpoints |
| `app/templates/*.html` | queue, review, boards, board_form |
| `Dockerfile` / `Procfile` / `run.sh` | deploy + local run |
| `boards_seed.csv` | sanitized seed (committed); full `job-boards-airtable.csv` is gitignored |
| `master_field_schema.md` / `implementation_brief.md` | the v2 spec (in ~/Downloads, source of truth for the schema) |

---

## 14. Decisions already made (don't re-litigate)

- Cloud = **Railway** (simpler than AWS for this).
- Inference/classification = **Full Claude** (`claude-opus-4-8`), with rule fallback. Cost is negligible at a few jobs/week.
- Credentials = Railway env vars only, no separate vault (won't hit 50 boards in 2026). Form holds only the `credentials_ref` label.
- Paid boards = warning badge + skipped on dispatch (payment stays manual). ~70-80% of boards are free.
- CAPTCHA/Cloudflare = **assisted mode**, never bypass.
- Build phases locally, review each, push at end (v2 was Phases 1–6).
- UTM = scrape the board tracker page (per the plan) with a construct-from-id fallback.

---

*Latest commit at handoff: `7e1a2b2` (targetconnect quote-in-id fix). Production is live and healthy.*
