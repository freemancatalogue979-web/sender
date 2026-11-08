# Validators — Decodo only (email + phone)

Both validators now have exactly one transport: the **Decodo Web Scraping API**. There is no Playwright, no Bright Data browser, and no ZeroBounce account anywhere in the codebase.

| | Site driven | Adapter | Field |
|---|---|---|---|
| **Email** | skrapp.io (emailhippo.com for Yahoo/AOL) | `SkrappAdapter`, `EmailHippoAdapter` | `validate_emails()` |
| **Phone** | veriphone.io | `VeriphoneAdapter` | `validate_phone_via_decodo()` |

Everything lives in one module — **`email_validator.py`** — and `functions.py` / `api.py` only orchestrate sessions around it.

---

## 1. What was removed

| Removed | Was at | Why |
|---|---|---|
| `validate_email_batch_browser()` (~800 lines) | `functions.py` | Playwright + ZeroBounce scraper |
| ZeroBounce auto-login / consent-cookie injection | same | account credentials path |
| `validate_single_email()` | same | browser-only wrapper, unused |
| `validate_phone_browser()` | same | Playwright + Bright Data CDP |
| `_validate_single_email_decodo()` | same | replaced by the adapter parser |
| `_validate_single_email_hippo()` | same | same |
| `validate_email_decodo_api()` | same | replaced by `validate_emails()` |
| `DEFAULT_BRIGHTDATA_BROWSER_URL` (2 copies, **with live credentials**) | same | hardcoded secret |
| `get_brightdata_browser_url()` (defined twice) | same | browser era |
| `save_brightdata_browser_url()`, `get_proxy_config()`, `get_proxy_for_validation()` | same | browser/proxy era, unused |
| `BrowserConfig` + `FV_S_BROWSER_URL` | `config.py` | nothing consumes a browser |
| `playwright==1.41.0` | `requirements.txt` | dependency gone |
| `POST/GET /validator/zerobounce/credentials` | `api.py` | ZeroBounce gone |
| Hardcoded Decodo key `TA000425890:PW_…` | `functions.py` | now env/user config only |

Net: **−2,100 lines** in `functions.py`, plus a new 700-line module.

---

## 2. How a lookup works now

```
POST /validator/email/validate-batch/{sid}
   └── process_email_validation_batch()            functions.py
         ├── load_decodo_settings(user)            env → user config → defaults
         └── validate_emails(batch, settings)      email_validator.py
               ├── ThreadPoolExecutor(min(n, 10))  one HTTP call per address
               ├── _call_decodo() → rendered HTML
               ├── html_to_text()                  scripts/styles/svg dropped
               ├── adapter.parse()                 skrapp | emailhippo
               ├── classify_email()                priority cascade → verdict
               └── _downgrade()                    MX corroboration
```

Settings precedence: **user's saved `validator-config/decodo.json` → `FV_S_DECODO_API_KEY` env → empty**. No built-in key, so a missing key fails loudly instead of silently using a stranger's credential.

---

## 3. The verdict engine (the actual fix)

The old code's final fallback was *"does the HTML contain 'valid' and not 'invalid'"*. That scores a dead mailbox as valid whenever a page renders any "Valid" label. Replaced with a fixed-priority cascade where **negatives are tested first and win ties**:

| # | Signal | → |
|---|---|---|
| 1 | "catch all" language anywhere | `catch_all` |
| 2 | `Email Status` | `valid` / `invalid` |
| 3 | `Mailbox Server` | `valid` / `invalid` |
| 4 | `Reachability` / deliverability | `valid` / `invalid` |
| 5 | Syntax/format failure | `invalid` |
| 6 | disposable / role / spam wording | `risky` |
| 7 | anything else | **`unknown`** — never `valid` |

`_judge()` runs `_NEGATIVE` before `_POSITIVE`, so `"Email is invalid"` can never be read as valid via the `valid` substring.

Two further guards:

- **Address echo check** — if the page shows a different address than the one submitted, the result is discarded as `unknown` with an error. The old code scraped whatever was on screen.
- **MX corroboration** — a scraped positive is queried against DNS; a domain with no MX is demoted, and an unresolvable domain drops confidence. DNS can only *downgrade*, never manufacture a "valid".

Result shape now carries `status`, `category` (`deliverable` / `risky` / `undeliverable` / `unknown`), `confidence`, `site` and `checks` alongside the legacy fields, so nothing downstream breaks.

---

## 4. Parsing without hashed class names

The old parser keyed on `css-zu3jh`, `css-glb8qx`, `css-1pzeik7`, `css-vlznub` and `button.horizontalValidator_btn___iH_a` — one redesign from silent failure.

Now the HTML is flattened to text (`html_to_text` keeps row boundaries, drops `<script>/<style>/<svg>`) and values are read as **`Label: value`**, handling all three layouts verifier pages use:

```
Email Status        ← label line, value next line
Valid

Email Status: Valid ← inline

Email Status Valid  ← adjacent spans collapsed
```

An alternation inside the label pattern is wrapped in `(?:…)` so it cannot leak into the outer regex, and a capture that is really the next label (`"Age 12"` after `Domain`) is rejected — both were real bugs caught by the tests.

Site adapters: **skrapp.io** for the general case, **emailhippo.com** for Yahoo/AOL (matched on the domain, not a mojibake list — the old list contained `"manly俊.com"` and `"te Target.com"`).

---

## 5. Honest counting

`process_email_validation_batch` used to bucket as *valid / invalid / failed*, where `failed` only ever meant "exception". Now:

- `error` set → **failed** (transport, auth, block, unparseable envelope)
- explicit positive verdict → **valid**
- everything else, including `unknown` and `risky` → **invalid**

Verified over HTTP with a bad API key: `processed: 2, valid: 0, invalid: 0, failed: 2` — nothing promoted.

---

## 6. Session / loop behaviour

Unchanged in shape (the UI still drives batches, one session per user) but fixed:

- `batch_size` is now **honoured** (was read from the session then overwritten with a hardcoded `10`), clamped to `MAX_VALIDATION_BATCH_SIZE`.
- `GET /validator/email/status` **reconciles** a session whose queue is drained but whose status is still `pending`/`running` — the "closed tab leaves it limbo" case now closes out.
- Phone validation runs through `asyncio.to_thread` instead of blocking the event loop, and uses the same Decodo transport (`validate_phone_via_decodo`).
- Results are written to `results` + `last_batch_results`; the valid subset still goes to the list and `valid emails/emails-*.json`.

---

## 7. New / changed API

| Endpoint | Change |
|---|---|
| `GET /validator/decodo/config` | **new** — `{configured, api_url, target_url, target_type, source}`; never returns the key |
| `POST /validator/decodo/config` | **new** — saves `api_key` (encrypted at rest, write-only), `api_url`, `target_url`, `target_type` |
| `POST /validator/decodo/test` | **new** — one real lookup to prove key + site work |
| `GET/POST /validator/proxy/config` | kept as aliases of the above |
| `POST/GET /validator/zerobounce/credentials` | **removed** (404) |
| `POST /validator/email/start` | no longer accepts `use_proxy` / `proxy_type` / `browser_api` |
| `GET /validator/email/lists` | now reads the same `emails/*.json` source as `/email/start` |

The API key round-trips encrypted (`gAAAAA…` in `decodo.json`) via the same `encrypt_secret` used for SMTP credentials.

---

## 8. Frontend

`Admin.html` live-log mapping updated only:

- `catch_all` renders as **Catch-All** with its confidence score (was keyed off a `sub_status` string that no longer exists).
- a genuine `unknown` renders as **Unknown** in the neutral style instead of being labelled *Invalid*.
- score uses the backend's `confidence` instead of a hardcoded 95.

No markup, styling or layout changed.

---

## 9. Validation performed

- `tests/test_email_validator.py` — **26 tests**, no network: happy paths, the dead-mailbox regression, catch-all, rate-limit page, unparseable page, transport error, missing key (asserts zero HTTP calls), mismatched-address rejection, MX downgrade, negative-beats-positive judgement, batch order + crash isolation.
- Full suite: **50 passed**.
- End-to-end against a running server: config save/get, encrypted at rest, aliases, removed route 404s, session created with the requested `batch_size` and `transport: decodo`, and a batch with a bad key producing `failed: 2`.
- In-process end-to-end: 8 addresses → `valid: 5, invalid: 3, failed: 0`, with exactly the 5 good addresses exported.

---

## 10. Remaining caveat

Parsing is still regex-over-text on a third party's markup — far more resilient than hashed classes, but if skrapp/emailhippo redesign their result layout, verdicts degrade to `unknown` rather than silently lying. That is the intended failure mode: `unknown` is counted separately and never exported as valid.


---

## 11. Phone validator (veriphone.io)

Wired to mirror the email path exactly — same transport, same session model, same honest counting.

### The page it drives

`https://veriphone.io/` — a single `input#phone-input[type=tel]` and a `#checker-submit` "Validate" button. Both ids are **stable page ids, not hashed build classes**, so the actions don't rot the way the ZeroBounce selectors did:

```json
browser_actions: [
  {wait 1s},
  {click  #phone-input},
  {input  #phone-input = "+234 706 997 7249"},
  {click  #checker-submit},
  {wait 4s}
]
```

### The result it parses

The page renders a two-column label/value grid. Text extraction turns it into:

```
Number +234 706 997 7249
Type mobile
Carrier MTN
Country Nigeria (NG)
```

`VeriphoneAdapter.parse()` reads `Number`, `Type`, `Carrier`, `Country`, plus optional `Region` / `City` / `Valid`.

### Verdict

`classify_phone()` mirrors `classify_email()` — explicit negative first, then explicit positive, else `unknown`:

| Signal | Verdict | Confidence |
|---|---|---|
| `Valid` field says invalid/valid | `invalid` / `valid` | 95 / 90 |
| `Type` = invalid | `invalid` | 80 |
| `Type` = mobile, landline, fixed line, voip | `valid` | 80 (with carrier) / 70 |
| `Type` = unknown | `unknown` | 0 |
| Number echoed but nothing else | `unknown` | 30 |
| Nothing | `unknown` | 0 |

A page that shows a **different number** than the one submitted is discarded as `unknown` with an error, same guard as email.

### E.164 normalisation (required)

`format_phone_number()` in `functions.py` deliberately **strips the dial code** and returns a national number, so the stored value cannot be sent to veriphone as-is (`7069977249` is meaningless to the checker).

`to_e164(number, country_code)` fixes that:

| Input | Hint | Sent |
|---|---|---|
| `+2347069977249` | NG | `+2347069977249` |
| `7069977249` | NG | `+2347069977249` |
| `07069977249` | NG | `+2347069977249` (trunk zero dropped) |
| `5551234567` | US | `+15551234567` |
| `07911123456` | GB | `+447911123456` |
| `447911123456` | — | `+447911123456` (detected) |
| `002347069977249` | — | `+2347069977249` |

A supplied country hint **always beats prefix guessing**, because short dial codes (7 = RU, 55 = BR) would otherwise capture unrelated national numbers — `7069977249` was being read as Russian during development.

### Session

Same shape as email, with `transport: "decodo"`, `site: "veriphone"`, and a real `batch_size` (was ignored; now honoured and clamped). `POST /validator/phone/start` accepts `batch_size`, `POST /validator/phone/validate/{sid}` processes the next batch, and the session response now includes `site`, `batch_size` and `last_batch_results` for the live log.

---

## 12. Bug fixed while wiring the phone path

**The active-session pointer was writing itself into the sessions folder.**

`_get_active_session_pointer_file()` put `active_phone.json` inside `validator-sessions/`, and `get_active_validator_session()` globs that same directory for `*.json`. Result: every run left behind a second, permanently-`pending` "session" that was never cleaned up — so after one validation, `/validator/phone/status` reported `running: true` forever and every new run was rejected with *"a validation process is already running"*.

The pointer now lives in a separate `validator-active/` folder, legacy pointers left inside the sessions directory are skipped, and `tests/test_phone_session.py` locks the behaviour in.

Also fixed: an empty result cell (`Carrier` blank) used to let the next label bleed in, so `Country Nigeria (NG)` was being recorded as the carrier. The label guard now covers the phone grid's labels.

---

## 13. Frontend (phone)

- **`pollPhoneValidationProgress` now triggers batches.** It only read the session and re-polled, so a phone session would sit at `pending` forever — the backend has no worker. It now posts to `/validator/phone/validate/{sid}` each tick, exactly like the email poller.
- New **`addPhoneNumberLogEntry`** — one row per number with carrier/country, verdict badge, and error text.
- Added `startPhoneTimer` / `stopPhoneTimer` (the poller already called `stopPhoneTimer`, which didn't exist) and reset `resetPhoneValidationUI` on completion.

---

## 14. Verification

- **84 tests passing** (was 50): 26 phone-specific cases covering the adapter's real selectors, the plan's exact result markup, invalid/unknown/empty pages, transport errors, missing-key fail-fast, number-mismatch rejection, the classifier matrix, E.164 normalisation, and the pointer-file regression.
- End-to-end through the real batch processor: `processed: 4, valid: 2, invalid: 2, failed: 0`, with the list saved as `plist_…` carrying `valid_count`, `invalid_count`, `failed_count`, `site`.
- Over HTTP against a running server: import → list → start (via `list_id`, `batch_size: 7`) → batch → `/phone/status` correctly flipping to `running: false` → a **new** session allowed to start.
- Email flow re-verified after the pointer change (4 valid / 2 invalid, no active session left behind).
- `Admin.html` inline JS passes `node --check`.
