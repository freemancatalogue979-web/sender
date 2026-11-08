# How the Email Validator Actually Works

*Code references are `file:line` in this repo. Short version: it is **not** an SMTP validator. It types the address into a third-party website through a proxy and scrapes the answer off the page.*

---

## 1. What a "validator" is here

There is no `RCPT TO` handshake, no MX lookup, no catch-all probing in the validation path. Confirmed: `grep 'RCPT TO' functions.py` → **0 hits**; the only DNS code (`functions.py:7744 check_domain_deliverability`) is an unrelated SPF/DKIM/DMARC advisory check for *sending* domains.

The validator is a **web-scraping pipeline**. For each address it drives a public-facing "email verifier" page — **skrapp.io**, **emailhippo.com**, or **zerobounce.net** — fills the form, clicks Verify, and parses the result out of the returned HTML/DOM.

Two interchangeable transport backends:

| | **Decodo** (default) | **Playwright** (fallback) |
|---|---|---|
| Trigger | `decodo_config.use_decodo_api = True` (`config.py`, hardcoded `True`) | `use_decodo_api = False` |
| Transport | HTTP POST to `https://scraper-api.decodo.com/v2/scrape` with a `browser_actions` script | Real Chromium, remote (Bright Data `wss://`) or local |
| Site | skrapp.io (normal), emailhippo.com (Yahoo/AOL) | zerobounce.net |
| Code | `functions.py:10383`, `:10896`, `:10735` | `functions.py:11069` |
| Concurrency | in-request, `ThreadPoolExecutor(max_workers=min(n,10))` | sequential per email, ~1.5 s + 2 s sleeps |

---

## 2. API surface

All under `/validator` (declared late, `api.py:6761`), all behind `Depends(get_current_user)`:

| Endpoint | Purpose |
|---|---|
| `GET /validator/email/lists` | lists available to validate |
| `GET /validator/email/status` | is a session active + progress |
| `POST /validator/email/start` | create session (`api.py:6804`) |
| `POST /validator/email/validate-batch/{sid}` | **process one batch of 10** (`api.py:6913`) |
| `GET /validator/email/session/{sid}` | progress + `last_batch_results` |
| `POST /validator/email/{pause,resume,stop}/{sid}` | flip `status` in the session file |
| `POST /validator/zerobounce/credentials` | save ZeroBounce login (plaintext JSON) |
| `POST /validator/proxy/config`, `GET /validator/proxy/config` | proxy settings |

Sibling `/validator/phone/*` endpoints do the same for phone numbers (`validate_phone_browser`, `functions.py:12496`).

---

## 3. The session file is the whole state machine

`create_email_validation_session()` (`functions.py:10317`) writes **one JSON file per session** to `users/fv-s-<uid>/validator-sessions/email_val_<8hex>.json` plus an "active pointer" file. It refuses to start if one is already active — **one session per user**.

```json
{
  "session_id": "email_val_ac1078cc", "status": "pending",
  "total_items": 132, "processed": 0, "valid": 0, "invalid": 0, "failed": 0,
  "queue": ["a@x.com", "b@y.com", ...],   // remaining addresses
  "current_batch": [],
  "results": [],                           // appended forever
  "last_batch_results": [],                // what the UI renders
  "use_proxy": true, "proxy_type": "brightdata", "browser_api": true,
  "batch_size": 3, "verified_per_minute": 0, "elapsed_seconds": 0
}
```

Status transitions: `pending → running → (paused) → completed | stopped`. Pause/resume/stop are **just writes to this file** — nothing is signalled to a worker; the client stops calling the batch endpoint.

---

## 4. Who runs the loop? The *browser* does.

This is the most surprising part. **The backend has no background task for validation.** `POST /email/start` only creates the session file and returns. The Admin SPA then drives everything (`Admin.html:36579 startEmailValidation`, `:36650 pollEmailValidationProgress`):

```
start  → poll every 2.5s ─┬─ status running/pending → POST /email/validate-batch/{sid} → repeat
                          ├─ status paused           → keep polling, don't invoke batch
                          └─ completed/stopped       → stop timer, reload lists
```

So: **close the tab mid-run and validation silently stalls** with the session stuck at `running`. Reopening the validator page picks it back up (the poll loop restarts from `/email/status`), but nothing server-side will ever finish it. There is also no rate shaping beyond the 2.5 s tick — the only pacing is whatever the scrape backend costs.

---

## 5. A batch, step by step

`process_email_validation_batch()` (`functions.py:12053`):

1. Load session; bail if `stopped`/`completed`.
2. Resolve transport: `browser_url = get_brightdata_browser_url(...)` if `browser_api`, plus proxy list. **`batch_size` from the request is ignored** — `batch_size = 10` is hardcoded at `functions.py:12131` (the frontend sends `batch_size: 10` / `3`, neither has effect on the batch size).
3. Pop up to 10 addresses off `queue` into `current_batch`.
4. Empty queue → mark `completed`.
5. Dispatch:
   - Decodo → `validate_email_decodo_api(batch)` — splits Yahoo/AOL/ymail to emailhippo, everything else to skrapp, runs both in a thread pool.
   - Playwright → `validate_email_batch_browser(batch, ...)` — one Chromium context per email with its own proxy.
6. Bucket results: `valid` → `valid++`; not valid **with** an `error` → `failed++`; not valid without → `invalid++`.
7. Append `{email, valid, status, domain, validated_at, error?, reason?, did_you_mean?}` to `results` and `last_batch_results`, update `queue`, recompute `verified_per_minute = valid / elapsed * 60`.
8. Queue empty → `completed`, then persist winners.

---

## 6. Backend A — Decodo (what actually runs)

`_validate_single_email_decodo()` (`functions.py:10383`) builds a **scripted browser sequence** and lets Decodo's cloud browser execute it:

```json
browser_actions: [
  {wait 1s},
  {click   input[name="email"]},
  {input   input[name="email"] = "target@example.com"},
  {click   text="Verify Email"},
  {wait 3s}
]
target: "universal", headless: "html", url: "https://skrapp.io/email-verifier"
```

Then the returned HTML is parsed with **regex against hashed CSS-modules class names** — `css-zu3jh`, `css-glb8qx`, `css-1pzeik7`, `css-vlznub` (`functions.py:10532-10600`). It also pulls `Email Status`, `Email Syntax`, `Mailbox Server`, `Reachability`, `Mailbox Type` via label-matching regexes.

**Verdict logic** is a 6-level cascade (`functions.py:10570-10680`), first hit wins:

1. `Email Status` text contains *invalid*/false → **invalid**; contains *valid* → **valid**
2. else `Mailbox Server` status text → same test
3. else `Reachability` text ("successfully reachable" / "undeliverable")
4. else regex `Email Status.*valid` with a 200/400-char window that must not contain "invalid"/"unavailable"
5. else **mailbox type**: `professional|business|webmail` → **valid**, `disposable` → invalid
6. else global scan: `"valid" in html and "invalid" not in html` → **valid**

Level 6 is the one that bites — see §9.

For Yahoo/AOL (`_validate_single_email_hippo`, `functions.py:10735`) it scrapes `tools.emailhippo.com` instead and just looks for `badge-ok` / `badge-bad` in the result table.

---

## 7. Backend B — Playwright / ZeroBounce

`validate_email_batch_browser()` (`functions.py:11069`), a ~800-line scraper. Per email:

- Fresh browser **context per address** (optionally a different proxy each), random UA from a list of 8, `headless` auto-derived from whether a display exists.
- Persists `browser/browser_state.json` in the *users base path* to keep consent cookies between runs.
- Pre-injects 5 fake consent cookies for `.zerobounce.net` (`consent`, `euconsent`, `gdprconsent`, …) to skip the GDPR modal.
- Detects rate-limiting by substring-scanning the page for `"you have reached the maximum"`, `"sign up for a free account"`, `"too many requests"`, etc. On hit it **auto-logs into ZeroBounce** using credentials from `validator-config/zerobounce.json`, reloads, and re-checks.
- Types into `input#email-validator`, clicks `button.horizontalValidator_btn___iH_a:has-text('Verify')` (a **build-hashed class name**), then polls up to 45 s for `#validator_component_status`; on timeout it reloads and retries once.
- Reads a dozen `#validator_component_*` nodes: `status`, `sub_status`, `free`, `did_you`, `account`, `domain`, `age`, `stmp` *(sic — typo'd ID)*, `mx_found`, `mx_record`, `first_name`, `last_name`.
- Verdict is simply `status_text.lower() == "valid"`.
- Sleeps 1.5 s between emails, 2 s after each, and has its own cookie-modal dismissal via injected JS.

---

## 8. Where results go

When the queue empties, `save_validation_results_to_list()` (`functions.py:12276`) keeps **only** `valid: true` rows and:

- **Import run** → creates a brand-new list `elist_<12hex>` containing only the valid addresses.
- **Existing list** → overwrites that list's entries with the valid subset.
- `save_valid_emails_to_file()` also writes `users/<uid>/valid emails/emails-YYYYmmdd_HHMMSS.json` — this is the "Export Valid" button's source.

The shipped data shows what that looks like in practice: an export with `mx_found: false` and `smtp_provider: null` on every row, plus a `reason: "Free email service provider"` on a gmail.com address that was nonetheless marked `valid` — i.e. the reasons are scraped labels, not probe results.

---

## 9. Problems, in order of severity

**Correctness**
1. **`invalid` is only reachable under weird conditions.** Level 6's fallback asks "does the HTML contain 'valid' and not 'invalid'". A skrapp page that renders *any* "valid"-labelled field (e.g. "Email Syntax: Valid") while the mailbox is dead will be scored **valid**. Since the fetch succeeded, no `error` is set — so the address lands in `valid`, not `invalid`/`failed`. A busy page can therefore inflate the valid count.
2. **Breakage is silent and total.** The whole parser keyed on `css-zu3jh`-style hashes and `horizontalValidator_btn___iH_a`. One redesign upstream and every address falls through the cascade to "unknown"/valid-ish. Nothing validates that the scrape actually returned a result before counting it.
3. **No honesty in the "failed" bucket.** Only exceptions/HTTP errors set `error`. A garbage parse is indistinguishable from a real verdict.
4. **Yahoo/AOL routing is a loose `in` check** (`'yahoo' in domain`), and the domain list itself is mojibake — `"global CROSS.net"`, `"manly俊.com"`, `"te Target.com"`, `"verification Target.com"` (`functions.py:10929-10937`). Clearly a corrupted paste.
5. `batch_size` is dead config: read from the session, then overwritten with `10` at `functions.py:12131`.

**Fragility / cost**
6. **The loop lives in the browser tab** (§4). No server-side worker, no resume, no crash recovery — the opposite of how the campaign engine works.
7. The hardcoded proxy path builds a list of **the same proxy three times** (`functions.py:12104`), so "rotation" is a no-op when `use_hardcoded_proxy` is on. The `else` branch that would load `proxies.json` assigns `active_proxies` and then throws it away — `proxy_list` stays empty.
8. Cost/throughput is set by third-party scraping: ~10 addresses per HTTP round trip at 60 s timeout, serialised by a 2.5 s client tick.

**Security**
9. **Hardcoded live-looking credentials in `functions.py`:**
   - `:10923` Decodo key `TA000425890:PW_111bc4...` (used as a silent fallback whenever `decodo_config` fails to import)
   - `:12050` and `:13312` `DEFAULT_BRIGHTDATA_BROWSER_URL = wss://brd-customer-hl_e297c7f0-...:mmynb335a7f0@brd.superproxy.io:9222`
   
   These bypass the env-var work `config.py` did — `test_hardcoded_secrets_removed` doesn't catch them because it only checks `config.py`.
10. **ZeroBounce account passwords are stored in plaintext** (`api.py:7342` writes `{email, password}` straight to `validator-config/zerobounce.json`), unlike SMTP creds which go through `encrypt_secret`.
11. The Playwright path reads `validator-config/zerobounce.json` from the **base path**, but saves it to the **user folder** (`fv-s-<uid>/validator-config/`) — the auto-login silently never finds its credentials.
12. Two functions are defined twice and the *later* ones win: `get_brightdata_browser_url` (`:12031` user-scoped, shadowed by `:13315` which defaults to the hardcoded URL) and `format_phone_number` (`:12660`, `:13153`).
13. `_validate_single_email_decodo` writes a **full scrape response to `/workspace/user_input_files/response.json`** for the first email of every batch (`functions.py:10480`) — leftover debugging, unbounded growth.

---

## 10. If you want it to actually validate

The scraper is the design flaw, not the implementation. A real validator for this codebase is ~300 lines and needs no proxies:

| Step | Check | Cost |
|---|---|---|
| 1 | Syntax + disposable/role-account list | free |
| 2 | `dns.resolver` MX/A lookup (dnspython is already a dependency) | free |
| 3 | SMTP probe: connect to the MX, `EHLO`, `MAIL FROM:<>`, `RCPT TO:<addr>`, read `250/450/550`, `QUIT` | free, ~2 s |
| 4 | Catch-all detection: probe 2–3 random addresses at the domain; all accepted ⇒ domain is catch-all ⇒ downgrade verdict | free |

That gives real `valid / invalid / catch-all / unknown` buckets with defensible sub-statuses, and removes the dependency on skrapp/emailhippo/ZeroBounce markup entirely. If you want to keep a paid service, call ZeroBounce's **REST API** (`api.zerobounce.net/v2/validate`) instead of scraping their website — same cost, stable contract.
