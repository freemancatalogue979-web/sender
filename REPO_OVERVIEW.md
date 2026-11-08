# FV-S — Repository Overview

*Extracted from `Fvsx.zip` (the repo's only tracked file). This document explains what the codebase is, how it's wired, and what to watch out for.*

---

## 1. What this is

**FV-S** is a self-hosted, single-tenant-per-process **cold email / bulk sending platform** with three bundled products behind one FastAPI backend:

| Product | Nav section | What it does |
|---|---|---|
| **Bulk sender** | Dashboard, SMTP, Manager, Emails, Letters, Send, Monitor, Logs | Imports SMTP/API sender credentials, imports recipient lists, runs multi-threaded campaigns with rotation, personalization, unsubscribe links, retries |
| **Inbox / IMAP client** | Mailbox, Inbox | Adds IMAP mailboxes, pools connections, syncs folders into SQLite, live push over WebSocket, webhooks |
| **List validator** | Validator | Email + phone validation via render-scraping of ZeroBounce/Skrapp through Decodo or Bright Data proxies (Playwright) |

Two frontends are static HTML files: `Admin.html` (~1.5 MB, the whole admin SPA, 15 nav sections) and `user.html` (54 KB, a slim user-facing client). No build step, no framework — plain JS + inline CSS, Font Awesome, Chart.js from CDNs.

It is clearly a **private operational tool** (Windows dev box, `C:\Users\Administrator\...`, an IP:port `SERVER_URL` hardcoded, `Users/` containing real credentials). Not a library, not a SaaS, no auth hardening.

---

## 2. Repo shape (post-extraction)

```
sender/
├── main.py              305 lines   entrypoint: auto-pip, FastAPI app, lifespan wiring, port juggling
├── config.py            453         pydantic settings, secret persistence, Fernet cred encryption, provider maps
├── api.py             7,407         ALL REST routes (198 endpoints) + campaign executor thread
├── functions.py      13,506         the god-module: IMAP, SMTP, campaigns, validators, users, storage
├── database.py          287         SQLAlchemy models (mailboxes, folders, emails, attachments, sync state)
├── db_helpers.py        873         query helpers over the above
├── email_cache.py     1,009         per-user in-memory + SQLite email cache
├── imap_manager.py      997         async IMAP connection pool (TTL, health checks, cleanup)
├── async_imap.py        579         thin async IMAP protocol wrapper
├── workers.py           677         per-mailbox sync workers + IDLE monitoring
├── websocket_manager.py 448         WS fanout for live inbox events
├── redis_manager.py     626         optional Redis pub/sub event bridge (multi-instance)
├── webhook_manager.py   572         outbound webhook registration + delivery w/ retries
├── email_providers.py   719         send adapters: SMTP, Gmail API, Graph, SendGrid, Mailgun, Mailjet, Postmark, SMTP2GO, SES
├── Admin.html         ~1.5 MB       admin SPA
├── user.html          54 KB         user SPA
├── migrate_email_lists.py            one-off migration to the SQLite list index
├── read_all_files.py                 dev utility: dumps all text files to one summary
├── test_imap_lookup.py, test_imap_web_lookup.py   ad-hoc manual scripts
├── tests/               5 files     pytest: config/secret, helpers, providers (thin coverage)
├── imap_servers.txt                  static provider → server table
├── data/email_lists_index.db         SQLite index of email lists (13 rows) — was gitignored, shipped anyway
├── data/secret.key                   **the live JWT/encryption secret**
└── users/                            **live production data for 12 accounts**
```

**Total Python: ~29,400 lines across 20 modules.**

---

## 3. Runtime architecture

```
Browser (Admin.html / user.html)
        │  fetch(SERVER_URL + "/...")  +  Bearer JWT
        ▼
   FastAPI (main.py)  ── 8 routers, 198 routes
        │
        ├── api.py ── campaign executor
        │        └── ThreadPoolExecutor (batch_size concurrent sends)
        │              └── email_providers.send_email_via_provider()
        │                     ├─ SMTP (aiosmtplib / smtplib, optional proxy)
        │                     ├─ Gmail API / MS Graph (OAuth2 tokens)
        │                     └─ HTTP APIs (SendGrid, Mailgun, Mailjet, Postmark, SMTP2GO, SES SigV4)
        │
        ├── imap_manager (pool) ── async_imap (aioimaplib)
        │        └── workers.py ── sync folders → SQLite (database.py/db_helpers.py)
        │              └── websocket_manager ── WS /ws/inbox
        │              └── redis_manager (optional) ── cross-instance pub/sub
        │              └── webhook_manager ── outbound HTTP callbacks
        │
        └── validators (Playwright) ── Bright Data browser or Decodo scrape API
```

### Startup (`lifespan`)
`DatabaseManager.init()` → seed default admin → recover interrupted campaign sessions → IMAP pool → sync workers → WS manager → Redis (optional; degrades gracefully) → webhook manager → email cache. Shutdown reverses it.

`main.py` additionally **pip-installs missing packages at import time** and on Windows kills whatever holds port 8000 (`netstat`/`taskkill`, with an interactive `input()` prompt).

### Storage model — two parallel systems
1. **Filesystem JSON** (`users/fv-s-<user_id>/*.json`) — the original design. Users, SMTP creds, API creds, campaigns, sessions, sent logs, access logs, subjects, from-names, letters, email lists, supplier/proxy configs. Everything user-facing lives here.
2. **SQLite** (`data/*.db`) — added later for the IMAP inbox feature (`mailboxes`, `folders`, `emails`, `attachments`, `sync_state`) plus an `email_lists_index`.

That split is the main source of duplication and drift (`functions.py` reads JSON lists, `email_cache.py` reads SQLite rows, and there are converters between the two).

### Auth
- JWT (`python-jose`, HS256) signed with `FV_S_SECRET_KEY` → `data/secret.key` → auto-generated.
- Passwords hashed with `passlib`/bcrypt, with a **legacy SHA-256** path (`LEGACY_SECRET_KEY = "fv-s-secret-key-change-in-production"`) so old hashes still verify.
- `Depends(get_current_user)` on protected routes — **but many routes are unauthenticated** (`/users/login`, `/users` CRUD, `/auth/register`, inbox `/system/*`, several validator endpoints).
- CORS is wide open (`allow_origins=["*"]`), plus hand-written `@router.options(...)` preflight handlers returning `Access-Control-Allow-Origin: *`.

### Config secrets
`config.py` was scrubbed so proxy host/user/pass, Decodo API key and Bright Data URL come from env vars (`FV_S_PROXY_*`, `FV_S_DECODO_API_KEY`, `FV_S_BROWSER_URL`). `tests/test_config.py::test_hardcoded_secrets_removed` enforces that they stay empty. Stored mailbox/API passwords are Fernet-encrypted via `encrypt_secret` / `decrypt_secret`, with plaintext passthrough for legacy rows.

---

## 4. The campaign engine (the core value)

`POST /send/start` (`api.py:3458`) resolves a unified sender list:

```json
{"senders": [{"type":"smtp","id":"..."}, {"type":"api","id":"..."},
             {"type":"google"}, {"type":"outlook"}, {"type":"any_smtp"}]}
```

`any_smtp` expands to every active SMTP credential (passwords decrypted). Then:

- **Sender pool with failover** — on `auth`/`connection` errors the sender is invalidated and the next one is used; transient errors retry with exponential backoff (capped 10 s).
- **Rotation** — `smtp_rotate_after` emails per sender; from-names and subjects round-robin per recipient.
- **Concurrency** — tasks dispatched through a `ThreadPoolExecutor` sized to `batch_size` (default 3).
- **Rate limiting** — sliding 60 s window, `rate_limit` emails/min (default 20).
- **Pacing** — `random_delay` sleep between batches.
- **Personalization** — `{Email}`, `{E-Name}`, `*|EMAIL|*`, `*|UNSUBSCRIBE|*` → signed per-recipient unsubscribe URL.
- **Proxies** — per-batch rotation via `get_proxy_for_sending` / `send_smtp_email_via_proxy`.
- **Pause/resume/stop** — a JSON session file polled inside the send loop; `recover_interrupted_sessions()` flips stale `running` sessions on restart.
- **Logging** — every attempt goes to `sent.json` + `access_logs.json`, with a truncated 200-entry per-campaign log file.

Campaigns run as a **daemon thread per campaign** (`threading.Thread` → `asyncio.run`) inside the API process. No queue, no persistence of in-flight work, no multi-worker safety — restarting the server stops sending.

---

## 5. Validator subsystem (`functions.py` ~lines 9,500–13,500)

- **Email:** MX/DNS + syntax checks (`dnspython`), then **browser scraping of ZeroBounce** (`validate_email_batch_browser`, Playwright with pre-baked consent cookies, ~1,000 lines) or **Decodo scrape API** (`validate_email_decodo_api`), batched ~10 per request, results bucketed valid/invalid/catch-all/unknown with confidence scores.
- **Phone:** same approach against a phone validator (`validate_phone_browser`, `process_phone_validation`, phone list CRUD, E.164-ish `format_phone_number`).
- **Sessions:** `create_email_validation_session` / `create_phone_validation_session`, progress in `validator-sessions/*.json`, pause/resume/stop per session, `calculate_verified_per_minute`, `finalize_validation_session`, `cleanup_old_sessions`.
- Reads an optional `validator-config/zerobounce.json` for credentials, otherwise scrapes the public validator UI anonymously — fragile by design.

---

## 6. Inbox subsystem (cleanest part of the codebase)

Well-factored and the only part with tests-friendly seams: `imap_manager` (pooled `aioimaplib` clients, TTL, max-per-mailbox, health checks) → `workers` (per-mailbox sync + IMAP IDLE) → `database`/`db_helpers` (SQLite) → `email_cache` (hot cache) → `websocket_manager` (fanout) + `redis_manager` (optional cross-instance) + `webhook_manager` (outbound, retried, with delivery stats). Supports folder listing, search, mark read/unread, move, delete, attachments, batch sync, import/export.

---

## 7. Findings & risks

**Critical**
1. **Live secrets and customer data are committed.** The zip contains `data/secret.key` (the JWT + Fernet key), 12 real accounts with bcrypt hashes, **43 plaintext SMTP passwords** (`users/fv-s-e4273016-.../smtp.json`, unencrypted despite `encrypt_secret` existing), ~173 validated recipient addresses, 3,187 send records, `audit.log`, session tokens and proxy/Bright Data config. `.gitignore` lists `users/`, `data/`, `*.db`, `secret.key` — the archive simply ignored it.
2. **Rotating `secret.key` is mandatory** if this ever ships; anyone with the repo can forge JWTs for any `user_id` and decrypt stored credentials.
3. **Unauthenticated admin surface** — `/users`, `/users/login`, `/auth/register` and parts of `/inbox/system/*` skip `Depends(get_current_user)`. User IDs are surfaced by `/users`, so "login by user_id" is effectively account enumeration + brute force against a guessable default (`Defaults.PASSWORD = "admin"`).

**High**
4. **`api.py` (7.4k lines) and `functions.py` (13.5k lines)** — no service layer; storage, transport, HTTP concerns interleaved. `functions.py` contains ~15 copy-pasted `except Exception: pass` blocks (some literally duplicated `pass` lines).
5. **Campaign state is in-process.** Daemon threads + JSON session polls ⇒ no horizontal scaling, no graceful shutdown, no resume of partially sent batches; duplicate sends are possible after crash.
6. **Sync I/O inside the async loop.** `send_emails()` is async but uses `time.sleep`, blocking `smtplib`, and a `ThreadPoolExecutor`; it will stall every other coroutine while a campaign runs.
7. **`main.py` pip-installs at import and kills processes on port 8000** (`input()` prompt breaks non-interactive deploys).

**Medium**
8. **Two storage systems** (JSON files vs SQLite) with no single source of truth; `email_lists_index.db` + the `emails/elist_*.json` files already drift.
9. **Frontend is hand-maintained monoliths.** `Admin.html` is 1.5 MB of inline CSS/JS with a hardcoded `SERVER_URL = 'http://83.229.83.19:8000'`; `user.html` points at a dead Cloudflare quick tunnel. Nothing in `main.py` actually serves these files (StaticFiles/FileResponse are imported but unused) — you open them directly.
10. **Scraping-based validation** breaks whenever ZeroBounce/Skrapp changes markup, and it's ToS-adjacent; unknown-proxy credentials fail silently.
11. **Test coverage is thin** — 5 files covering config/helpers/providers; zero tests for campaigns, IMAP pooling, or the API layer.
12. **Repo hygiene** — the source arrived only as a 19 MB `Fvsx.zip`; `fvsx_all_text_files_summary.txt` is a 16 MB dump of the tree (now removed here); `logo.png` and `server-icon.png` are the same 2.5 MB file.

**Unverified**
13. `functions.py` has **two definitions of `get_brightdata_browser_url`** (line 12031 and 13315) and `format_phone_number` (12660 and 13153) — the later ones win at import.
14. `get_user_folder` hardcodes `base_path / f"fv-s-{user_id}"` instead of `UserFolderPrefix.generate()`, so the prefix constant isn't authoritative.

---

## 8. What I did in this branch

- Extracted `Fvsx.zip` into the working tree so the code is browsable/diffable.
- **Excluded** the bundled Windows virtualenv (`.venv/`, 31 MB of `cp312-win_amd64` binaries), `__pycache__/`, the 16 MB text dump, and the entire `users/` + `data/` runtime state (**including the exposed credentials — they are not in this branch**).
- Verified every Python module byte-compiles cleanly under Python 3.12 (`py_compile`).
- Left `Fvsx.zip` untouched at the repo root.

## 9. Sensible next steps

1. Rotate anything that ever lived in `users/` and `data/secret.key`; purge the zip from Git history if the repo is public.
2. Add `Depends(get_current_user)` to `/users*` and `/auth/register`; drop the `admin`/`admin` default.
3. Break `api.py`/`functions.py` into `services/` (sending, imap, validator, users) behind the existing routers.
4. Move campaign execution to an out-of-process queue (Redis + worker, or Celery) — `redis_manager` already provides the transport.
5. Serve `Admin.html`/`user.html` from FastAPI with a relative API base instead of a hardcoded IP.
6. Choose SQLite or JSON for email lists and delete the other path.
