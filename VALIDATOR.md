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

- **106 tests passing** as of this section (84 at the time of the scrape-only build): 26 phone-specific cases covering the adapter's real selectors, the plan's exact result markup, invalid/unknown/empty pages, transport errors, missing-key fail-fast, number-mismatch rejection, the classifier matrix, E.164 normalisation, and the pointer-file regression.
- End-to-end through the real batch processor: `processed: 4, valid: 2, invalid: 2, failed: 0`, with the list saved as `plist_…` carrying `valid_count`, `invalid_count`, `failed_count`, `site`.
- Over HTTP against a running server: import → list → start (via `list_id`, `batch_size: 7`) → batch → `/phone/status` correctly flipping to `running: false` → a **new** session allowed to start.
- Email flow re-verified after the pointer change (4 valid / 2 invalid, no active session left behind).
- `Admin.html` inline JS passes `node --check`.

---

## 15. Phone validation: the free web checker, with an optional API shortcut

The phone validator uses **veriphone.io's own free checker** — the one at the top
of their homepage, "try it free, no account required" — driven through Decodo
exactly like the email validator drives skrapp.io. There is no signup, no API
key, and no credit balance to manage.

An API key is **optional**. If one happens to be configured, the code takes a
single-request shortcut instead of a browser session. If not, the free checker
runs. Either way the output shape is identical. Do not read this section as
"the phone validator requires a veriphone account" — it does not.

### Order of preference

```
validate_phone(number, country_code)
  │
  ├─ to_e164(number, country_code)          → "+2347069977249"
  ├─ is_plausible_e164(...)                 → refuse malformed input, no lookup
  │
  ├─ key configured?  (optional, usually no)
  │    ├─ yes → GET https://api.veriphone.io/v3/verify   (one request, no browser)
  │    │         └─ fails ⇒ return the error  (no silent fallback)
  │    └─ no  → DEFAULT: the free veriphone.io checker through Decodo
  │
  └─ verdict is never `valid` unless veriphone says so
```

The default path — no key — is the one this repo is built around.

The key point is the **no silent fallback** rule. If a key exists and the call
fails, the row is recorded as `failed` with the reason. Falling back to the
scraper would hide an expired key and quietly consume Decodo credits.

### The request

```
GET https://api.veriphone.io/v3/verify?phone=%2B2347069977249
Authorization: Bearer <key>
```

Both auth forms (`Authorization` header and `?key=`) are sent, because the
docs recommend the header while the on-page checker uses the query parameter.
One credit per verify; the free tier is 1,000/month.

### Field mapping

| veriphone        | result field                              |
|------------------|-------------------------------------------|
| `phone_valid`    | `status` / `valid` (true → valid, 99 conf)|
| `phone_type`     | `line_type`                               |
| `carrier`        | `carrier`                                 |
| `country`        | `country`, `country_code` from `(XX)`     |
| `country_code`   | `country_code`                            |
| `phone_region`   | `region`                                  |
| `international_number` / `e164` | `formatted_number`          |

`site` is set to `veriphone-api` so stored lists can be filtered by transport;
the scrape path keeps `site = veriphone`.

### Guards learned the hard way

- **`is_plausible_e164`** — `+` then 6–15 digits, no leading zero. A truncated
  value (`+069977249`) used to reach the veriphone page and come back with a
  *different* number's verdict, which the parser then reported as `valid`.
  Malformed input is now classified locally as `unknown` / `malformed`.
- **Echo check** — if the API marks a number valid but echoes a different
  `e164`, the verdict is discarded (`unknown`, "returned a different number").
  This is what catches a cached or mismatched response.
- **`to_e164` honours the country hint** — the hint always wins over prefix
  guessing, so `7069977249` + `NG` is `+2347069977249`, not `+7069977249`.

### Why there is no keyless JSON endpoint (checked, not assumed)

The homepage says "Try it free — no account required". That is true of the
**page**, not of a public endpoint. Both playable endpoints reject a keyless
call:

```
GET https://api.veriphone.io/v3/verify?phone=+14236319525
→ 400 {"status":"error","code":400,"type":"Missing Parameter",
       "message":"Required parameter key is missing or empty"}

GET https://api.veriphone.io/v2/verify?phone=+14236319525
→ 400 same error

GET https://veriphone.io/api/v2/verify?phone=+14236319525
→ 404 (that path is not served; the site is a static app)
```

Their documentation states the key is read from, in order, the
`Authorization: Bearer` header, a `key` cookie, or the `key` query parameter,
and that a key comes from the free signup (1,000 credits/month, no card).

So the request the homepage checker issues is a *keyed* request made from
inside their JavaScript, using a key or session token that only exists in their
browser page. Replicating it from a server would mean lifting their credential
out of their bundle, which is not something this code should do: it would be
using someone else's key, it would break the moment they rotate it, and it is
against their terms.

**Conclusion:** there is no free, keyless JSON path to copy. The free path *is*
the page, and that is exactly the path this validator already drives through
Decodo. Nothing is missing — the transport in use with no key configured is the
free one.

### Configuration

| Transport  | When it runs                                   | What it needs        |
|------------|------------------------------------------------|----------------------|
| Free checker (**default**) | no veriphone key configured     | Decodo credentials   |
| Direct API (**optional**)  | a veriphone key is configured   | a veriphone key      |

Leave the key blank and the free checker runs. That is the intended setup.

Stored keys are Fernet-encrypted at `fv-s-<uid>/validator-config/veriphone.json`
(same scheme as the Decodo credentials). The GET endpoint only ever reports
`has_api_key` / `transport`, never the key.

```
GET  /validator/veriphone/config   → {configured, api_url, transport, source}
POST /validator/veriphone/config   → {api_key}  (write-only)
POST /validator/veriphone/test     → one real lookup, proves the key works
```

`/phone/start` / `/phone/status` now report the `transport` actually chosen, and
the session file records it, so a run can be explained after the fact.

### Additional bugs fixed in this pass

1. **`/phone/start` queued mangled numbers.** It piped every input through
   `format_phone_number`, which strips the dial code — and for a bare
   `07069977249` even the trunk zero. Three different inputs became
   `069977249`, `031234567` and `4155552671`. Numbers are now queued verbatim
   and normalised once, in `to_e164`, right before the lookup.
2. **The country hint was dropped.** With no list-level country, the detected
   country was discarded and national digits were sent with no context. The
   start route now falls back to the list's country, then to a detected one.
3. **Result lists were never written.** `save_phone_results_to_list` appended a
   full payload to `lists.json` but never created `plist_<id>.json`, so
   `GET /phone/lists/{id}` returned 404 for every saved list while the index
   advertised it. The file is now written and the index holds a summary only.
4. **Absurd `verified_per_minute`.** The first batch of a session can finish in
   milliseconds; `2 valid / 0.001s` reported 120,000,000/min. `start_time` is
   now anchored on the first batch and the rate stays 0 below one second.

### Verification (this pass)

- **106 tests passing** (was 84). New coverage: API-used-instead-of-Decodo,
  E.164 sent rather than national digits, valid/invalid/unknown mapping,
  401/429/500, in-band `status:"error"`, timeout, string booleans, the echo
  mismatch guard, `is_plausible_e164` matrix, settings encryption round-trip and
  the env fallback, plus the `/phone/start` queue regression.
- **Over HTTP against a running server**, both transports: with a key
  `/phone/start` reported `transport: veriphone-api`; after deleting the stored
  key the same call reported `decodo-scrape`. Both batches returned
  `processed 3, valid 3, failed 0`, and the numbers reached veriphone as
  `+2347069977249` / `+2348031234567` / `+14155552671`.
- **Saved list round-trip**: `lists.json` entry → `GET /phone/lists/plist_…`
  returned the full record with `e164`, `status`, `carrier`, `site` per number.
- Key storage checked on disk: the plaintext key does not appear in
  `validator-config/veriphone.json`.
- `Admin.html` inline JS passes `node --check` after adding the transport badge
  and key controls.
- The sandbox has no outbound internet to veriphone, so the live call itself was
  exercised with a stubbed `_call_veriphone_api`; the request shape (URL,
  `phone` param, bearer header) is asserted in the unit tests.
