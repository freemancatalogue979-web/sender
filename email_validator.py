"""
Decodo-native email validation for FV-S.

This module is the single validation transport for the platform. There is no
Playwright/Bright Data browser and no ZeroBounce account: every lookup is a
single Decodo Web Scraping API request that drives a third-party verifier page
(skrapp.io by default, tools.emailhippo.com for Yahoo/AOL) through
``browser_actions`` and returns the rendered HTML, which is parsed here.

Design rules this module enforces:

* **Never guess "valid".** A verdict of ``valid`` must come from an explicit
  positive signal on the page. Missing/unreadable signals produce ``unknown``,
  which is counted separately from both ``valid`` and ``failed``.
* **No class-name hashing.** Parsers work on the *rendered text* of the
  page wherever possible, falling back to structural tags. Hashed CSS module
  names (``css-zu3jh`` …) are not used.
* **No substring traps.** Checks are evaluated in a fixed priority order and
  the first resolved field wins, so a page that merely contains the word
  "valid" somewhere can never flip a verdict.
* **No secrets in source.** The Decodo key comes from ``FV_S_DECODO_API_KEY``
  (or the user's saved ``validator-config/decodo.json``); there is no baked-in
  fallback credential.
* **No side-effect dumps.** Nothing is written to disk from the request path.

Public API
----------
``validate_emails(emails, settings=None)``      batch validation (concurrent)
``validate_one(email, settings=None)``          single validation
``load_decodo_settings(user_id, base_path)``    resolve user → env → defaults
``save_decodo_settings(user_id, base_path, d)`` persist user settings

Shared primitives (``extract_html``, ``html_to_text``, ``SiteAdapter``,
``judge_text``) are public because the phone validator reuses them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape as _html_unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from config import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

DECODO_DEFAULT_API_URL = "https://scraper-api.decodo.com/v2/scrape"
SKRAPP_URL = "https://skrapp.io/email-verifier"
HIPPO_URL = "https://tools.emailhippo.com/"
VERIPHONE_URL = "https://veriphone.io/"

# Stable element ids on veriphone.io (not hashed build classes).
VERIPHONE_INPUT_ID = "phone-input"
VERIPHONE_SUBMIT_ID = "checker-submit"

# The real JSON API. Preferred over scraping the checker page: one GET, no
# browser, no HTML parsing, stable response contract.
VERIPHONE_API_URL = "https://api.veriphone.io/v3/verify"

# Domains that emailhippo handles more reliably than skrapp.
HIPPO_DOMAIN_RE = re.compile(r"(yahoo|ymail|aol|aim)\b", re.IGNORECASE)

# Page text that means "the third party refused / throttled us".
BLOCKED_PHRASES = (
    "you have reached the maximum",
    "maximum number of validations",
    "rate limit",
    "too many requests",
    "please sign up",
    "captcha",
    "access denied",
    "verify you are human",
)

# Page text that means the verifier could not decide.
INDETERMINATE_PHRASES = (
    "unable to verify",
    "could not verify",
    "unknown",
    "no result",
    "try again",
)

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_CATCH_ALL = "catch_all"
STATUS_UNKNOWN = "unknown"
STATUS_RISKY = "risky"

# How a status maps onto the legacy boolean the REST layer / UI expects.
# ``catch_all`` counts as valid because the domain demonstrably accepts
# recipients — the label is preserved in ``status`` so it stays visible.
VALID_STATUSES = {STATUS_VALID, STATUS_CATCH_ALL}

MAX_WORKERS = 10

# A captured "value" that begins with one of these is really the next label,
# not a value (e.g. "Domain Age 12" must not yield Domain = "Age 12").
_NEXT_LABEL = re.compile(
    r"^(age|status|syntax|server|found|record|type|provider|name|mean|email|"
    # phone-grid labels: an empty cell must not let "Carrier" absorb "Country …"
    r"country|carrier|city|region|number|prepaid|leaked|"
    r"mx|smtp|reachability|deliverability)\b",
    re.IGNORECASE,
)
REQUEST_TIMEOUT = 60


def _blank_result(email: str) -> Dict[str, Any]:
    """A result with every field the REST layer, list writer and UI expect."""
    domain = email.rsplit("@", 1)[1].lower() if "@" in email else None
    return {
        "email": email,
        "valid": False,
        "status": STATUS_UNKNOWN,
        "category": "unknown",
        "confidence": 0,
        "sub_status": None,
        "free_email": False,
        "did_you_mean": None,
        "account": None,
        "domain": domain,
        "domain_age_days": None,
        "smtp_provider": None,
        "mx_found": False,
        "mx_record": None,
        "mx_records": [],
        "first_name": None,
        "last_name": None,
        "checks": {},
        "site": None,
        "error": None,
        "reason": None,
    }


# =============================================================================
# SETTINGS
# =============================================================================

def load_decodo_settings(
    user_id: Optional[str] = None,
    base_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Resolve Decodo settings for a user.

    Precedence: saved user config → ``FV_S_DECODO_API_KEY`` env → module default.
    The API key is stored encrypted at rest and decrypted here for use.
    """
    import os

    from config import decodo_config

    settings: Dict[str, Any] = {
        "api_url": decodo_config.api_url or DECODO_DEFAULT_API_URL,
        "api_key": "",
        "target_url": SKRAPP_URL,
        "target_type": decodo_config.target_type or "universal",
    }

    if user_id and base_path:
        cfg_file = Path(base_path) / f"fv-s-{user_id}" / "validator-config" / "decodo.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                settings["api_url"] = saved.get("api_url") or settings["api_url"]
                settings["target_url"] = saved.get("target_url") or settings["target_url"]
                settings["target_type"] = saved.get("target_type") or settings["target_type"]
                settings["api_key"] = decrypt_secret(saved.get("api_key", "") or "")
            except Exception as exc:  # corrupt file must not break validation
                logger.warning("Could not read Decodo config for %s: %s", user_id, exc)

    if not settings["api_key"]:
        settings["api_key"] = os.getenv("FV_S_DECODO_API_KEY", "") or decodo_config.api_key or ""

    return settings


def save_decodo_settings(
    user_id: str,
    base_path: Path,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist per-user Decodo settings. The API key is encrypted at rest."""
    cfg_dir = Path(base_path) / f"fv-s-{user_id}" / "validator-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "decodo.json"

    existing: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    api_key = (data.get("api_key") or "").strip()
    payload = {
        "api_url": (data.get("api_url") or existing.get("api_url") or DECODO_DEFAULT_API_URL).strip(),
        "target_url": (data.get("target_url") or existing.get("target_url") or SKRAPP_URL).strip(),
        "target_type": (data.get("target_type") or existing.get("target_type") or "universal").strip(),
        # Only replace the stored key when a new one is supplied, so saving
        # other fields twice does not wipe the credential.
        "api_key": encrypt_secret(api_key) if api_key else existing.get("api_key", ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return {
        "api_url": payload["api_url"],
        "target_url": payload["target_url"],
        "target_type": payload["target_type"],
        "has_api_key": bool(payload["api_key"]),
    }


# =============================================================================
# VERIPHONE API SETTINGS
# =============================================================================

def load_veriphone_settings(
    user_id: Optional[str] = None,
    base_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Resolve veriphone API settings for a user.

    Precedence: saved user config → ``FV_S_VERIPHONE_API_KEY`` env → empty.
    The key is stored encrypted at rest and decrypted here for use.
    """
    import os

    settings: Dict[str, Any] = {"api_url": VERIPHONE_API_URL, "api_key": ""}

    if user_id and base_path:
        cfg_file = Path(base_path) / f"fv-s-{user_id}" / "validator-config" / "veriphone.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                settings["api_url"] = saved.get("api_url") or settings["api_url"]
                settings["api_key"] = decrypt_secret(saved.get("api_key", "") or "")
            except Exception as exc:
                logger.warning("Could not read veriphone config for %s: %s", user_id, exc)

    if not settings["api_key"]:
        settings["api_key"] = os.getenv("FV_S_VERIPHONE_API_KEY", "")

    return settings


def save_veriphone_settings(
    user_id: str,
    base_path: Path,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist per-user veriphone settings. The API key is encrypted at rest."""
    cfg_dir = Path(base_path) / f"fv-s-{user_id}" / "validator-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "veriphone.json"

    existing: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    api_key = (data.get("api_key") or "").strip()
    payload = {
        "api_url": (data.get("api_url") or existing.get("api_url") or VERIPHONE_API_URL).strip(),
        "api_key": encrypt_secret(api_key) if api_key else existing.get("api_key", ""),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return {
        "api_url": payload["api_url"],
        "has_api_key": bool(payload["api_key"]),
    }


# =============================================================================
# HTML → TEXT
# =============================================================================

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_ROW_END = re.compile(r"</(?:tr|div|p|li|h[1-6]|td|th|section|article)>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")


def html_to_text(html: str) -> str:
    """Turn rendered HTML into newline-separated text with rows kept apart."""
    if not html:
        return ""
    text = _DROP_BLOCKS.sub(" ", html)
    text = _ROW_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = _html_unescape(text)
    text = text.replace("\r", "")
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _find_first(patterns: Iterable[str], text: str) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            for group in match.groups():
                if group and group.strip():
                    return group.strip()
            if not match.groups():
                return ""
    return None


# =============================================================================
# SITE ADAPTERS
# =============================================================================

class SiteAdapter:
    """How to drive and read one third-party verifier page."""

    name = "generic"
    url = ""
    category = "email"

    def browser_actions(self, email: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def parse(self, text: str) -> Dict[str, Any]:
        """Return raw scraped fields; classification happens centrally."""
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------
    @staticmethod
    def field(text: str, label_pattern: str) -> Optional[str]:
        """
        Read the value belonging to ``label_pattern``.

        Rendered verifier pages put the label and value either on separate
        lines, colon-separated, or as adjacent spans that collapse onto one
        line ("Email Status Valid"). All three shapes are handled, and a match
        whose "value" is really the start of the next label is rejected so
        "Domain Age 12" can never be read as ``Domain = "Age 12"``.
        """
        # (?:...) keeps an alternation inside label_pattern from leaking into
        # the outer expression, which would otherwise drop the capture group.
        label = f"(?:{label_pattern})"
        patterns = (
            # label on one line, value on the next
            rf"{label}\s*:?\s*\n\s*([^\n]{{1,80}})",
            # label: value
            rf"{label}\s*:\s*([^\n]{{1,80}})",
            # label value  (adjacent, no separator)
            rf"{label}\s+([^\n]{{1,80}})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            captured = match.group(1)
            if not captured:
                continue
            value = captured.strip(" -–—:|")
            if not value:
                continue
            # Reject when the capture is actually the next label's name.
            if _NEXT_LABEL.search(value):
                continue
            if value.lower() in ("unknown", "n/a", "none", "-", "—"):
                return None
            return value
        return None

    @staticmethod
    def yes_no(text: str, label_pattern: str) -> Optional[bool]:
        value = SiteAdapter.field(text, label_pattern)
        if value is None:
            return None
        lowered = value.lower()
        if lowered.startswith("yes") or lowered.startswith("true"):
            return True
        if lowered.startswith("no") or lowered.startswith("false") or "not " in lowered:
            return False
        return None


class SkrappAdapter(SiteAdapter):
    """
    skrapp.io/email-verifier

    Rendered result block is a label/value list:

        Email Syntax      Valid
        Email Status      Valid
        Mailbox Server    Valid
        Domain            gmail.com
        Reachability      Successfully reachable
        Mailbox Type      Webmail
    """

    name = "skrapp"
    url = SKRAPP_URL

    def browser_actions(self, email: str) -> List[Dict[str, Any]]:
        return [
            {"type": "wait", "wait_time_s": 1},
            {
                "type": "click",
                "selector": {"type": "css", "value": 'input[name="email"]'},
                "timeout_s": 5,
            },
            {
                "type": "input",
                "selector": {"type": "css", "value": 'input[name="email"]'},
                "value": email,
            },
            {
                "type": "click",
                "selector": {"type": "text", "value": "Verify Email"},
                "timeout_s": 5,
            },
            {"type": "wait", "wait_time_s": 4},
        ]

    def parse(self, text: str) -> Dict[str, Any]:
        return {
            "email_status": self.field(text, r"Email\s+Status"),
            "syntax_status": self.field(text, r"Email\s+Syntax"),
            "server_status": self.field(text, r"Mailbox\s+Server|Server\s+Status"),
            "reachability": self.field(text, r"Reachability|Deliverab"),
            "mailbox_type": self.field(text, r"Mailbox\s+Type|Account\s+Type"),
            "domain_field": self.field(text, r"Domain"),
            "did_you_mean": self.field(text, r"Did\s+You\s+Mean|Suggestion"),
            "domain_age": self.field(text, r"Domain\s+Age|Age\s+of\s+Domain"),
            "free_field": self.field(text, r"Free\s+Email|Free\s+Service"),
            "first_name": self.field(text, r"First\s+Name"),
            "last_name": self.field(text, r"Last\s+Name"),
            "mx_found": self.yes_no(text, r"MX\s+Found"),
            "mx_record": self.field(text, r"MX\s+Record|Mail\s+Server"),
            "smtp_provider": self.field(text, r"SMTP\s+Provider|Provider"),
        }


class EmailHippoAdapter(SiteAdapter):
    """
    tools.emailhippo.com

    Result table renders ``Badge | Key | Value`` rows, e.g.

        ➜  Email address   someone@yahoo.com
        ✔  Format         Valid
        ✘  MX record      none
    """

    name = "emailhippo"
    url = HIPPO_URL

    def browser_actions(self, email: str) -> List[Dict[str, Any]]:
        return [
            {"type": "wait", "wait_time_s": 1},
            {
                "type": "input",
                "selector": {"type": "css", "value": "input[type='text']"},
                "value": email,
            },
            {
                "type": "click",
                "selector": {"type": "text", "value": "Verify"},
                "timeout_s": 5,
            },
            {"type": "wait", "wait_time_s": 4},
        ]

    def parse(self, text: str) -> Dict[str, Any]:
        return {
            "email_status": self.field(text, r"Email\s+(?:validity|status)"),
            "syntax_status": self.field(text, r"Format|Syntax"),
            "server_status": self.field(text, r"MX\s+record|Mailbox|Server"),
            "reachability": self.field(text, r"Deliverab|Reachab|Result"),
            "mailbox_type": self.field(text, r"Type|Account\s+Type"),
            "domain_field": self.field(text, r"Domain"),
            "did_you_mean": self.field(text, r"Did\s+You\s+Mean|Suggestion"),
            "domain_age": None,
            "free_field": None,
            "first_name": None,
            "last_name": None,
            "mx_found": None,
            "mx_record": self.field(text, r"MX\s+record"),
            "smtp_provider": None,
        }


class VeriphoneAdapter(SiteAdapter):
    """
    veriphone.io — the phone-number checker.

    The form is a single tel input plus a "Validate" submit; the result renders
    as a two-column label/value grid:

        Number   +234 706 997 7249
        Type     mobile
        Carrier  MTN
        Country  Nigeria (NG)

    Element ids on that page are stable (``phone-input`` / ``checker-submit``),
    so they are used instead of positional or hashed selectors.
    """

    name = "veriphone"
    url = VERIPHONE_URL
    category = "phone"

    def browser_actions(self, value: str) -> List[Dict[str, Any]]:
        return [
            {"type": "wait", "wait_time_s": 1},
            {
                "type": "click",
                "selector": {"type": "css", "value": f"#{VERIPHONE_INPUT_ID}"},
                "timeout_s": 5,
            },
            {
                "type": "input",
                "selector": {"type": "css", "value": f"#{VERIPHONE_INPUT_ID}"},
                "value": value,
            },
            {
                "type": "click",
                "selector": {"type": "css", "value": f"#{VERIPHONE_SUBMIT_ID}"},
                "timeout_s": 5,
            },
            # The grid is rendered after an in-page fetch; give it room.
            {"type": "wait", "wait_time_s": 4},
        ]

    def parse(self, text: str) -> Dict[str, Any]:
        return {
            "number": self.field(text, r"Number"),
            "line_type": self.field(text, r"Type"),
            "carrier": self.field(text, r"Carrier"),
            "country": self.field(text, r"Country"),
            "region": self.field(text, r"Region|State"),
            "city": self.field(text, r"City"),
            "valid_field": self.field(text, r"Valid(?:e|ity)?"),
        }


_ADAPTERS: Dict[str, SiteAdapter] = {
    "skrapp": SkrappAdapter(),
    "emailhippo": EmailHippoAdapter(),
    "veriphone": VeriphoneAdapter(),
}


def _adapter_for(email: str, settings: Dict[str, Any]) -> SiteAdapter:
    target = (settings.get("target_url") or "").lower()
    if "emailhippo" in target:
        return _ADAPTERS["emailhippo"]
    domain = email.rsplit("@", 1)[1] if "@" in email else ""
    if HIPPO_DOMAIN_RE.search(domain):
        return _ADAPTERS["emailhippo"]
    return _ADAPTERS["skrapp"]


# =============================================================================
# VERDICT
# =============================================================================

_POSITIVE = re.compile(r"\b(valid|deliverable|reachable|accepts?|ok)\b", re.IGNORECASE)
_NEGATIVE = re.compile(
    r"\b(invalid|undeliverable|not\s+reachable|does\s+not\s+exist|rejected|"
    r"no\s+mailbox|mailbox\s+(?:not\s+found|full)|bounce[sd]?|doesn'?t\s+exist)\b",
    re.IGNORECASE,
)


def judge_text(text_value: Optional[str]) -> Optional[bool]:
    """
    Classify one scraped sentence.

    Negatives are tested first and win ties, so "not reachable" / "invalid"
    can never be read as a positive by a loose ``valid`` substring test.
    """
    if not text_value:
        return None
    if _NEGATIVE.search(text_value):
        return False
    if _POSITIVE.search(text_value):
        return True
    return None


def classify_email(raw: Dict[str, Any]) -> Tuple[str, str, int]:
    """
    Map scraped fields to ``(status, reason, confidence)``.

    Priority is deliberate and first-match wins:

    1. explicit ``Email Status`` / validity field
    2. mailbox-server (SMTP) status
    3. reachability / deliverability sentence
    4. syntax or format failure -> invalid
    5. catch-all / accept-all language -> catch_all
    6. disposable / role account language -> risky
    7. otherwise -> unknown (never "valid" by default)
    """
    status = (raw.get("email_status") or "")
    server = (raw.get("server_status") or "")
    reach = (raw.get("reachability") or "")
    syntax = (raw.get("syntax_status") or "")
    combined = " ".join(
        v for v in (status, server, reach, (raw.get("mailbox_type") or "")) if v
    ).lower()

    if "catch" in combined and "all" in combined:
        return STATUS_CATCH_ALL, "Domain accepts all recipients (catch-all)", 60

    decision = judge_text(status)
    if decision is True:
        return STATUS_VALID, status or "Mailbox verified", 95
    if decision is False:
        return STATUS_INVALID, status, 95

    decision = judge_text(server)
    if decision is True:
        return STATUS_VALID, f"Mailbox server says {server}", 85
    if decision is False:
        return STATUS_INVALID, server, 85

    decision = judge_text(reach)
    if decision is True:
        return STATUS_VALID, reach, 80
    if decision is False:
        return STATUS_INVALID, reach, 80

    if syntax and _NEGATIVE.search(syntax):
        return STATUS_INVALID, f"Syntax check failed: {syntax}", 90

    if re.search(r"\b(disposable|temp(?:orary)?|role|spam)\b", combined):
        return STATUS_RISKY, "Disposable or role address", 50

    return STATUS_UNKNOWN, "Verifier returned no conclusive result", 0


def _domain_mx(domain: str) -> Tuple[bool, List[str]]:
    """Best-effort MX lookup used only to corroborate, never to decide."""
    if not domain:
        return False, []
    try:
        import dns.resolver  # type: ignore
    except Exception:
        return False, []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        records = sorted((str(r.exchange).rstrip(".") for r in answers))
        return bool(records), records
    except Exception:
        return False, []


def _downgrade(status: str, reason: str, confidence: int, domain: str) -> Tuple[str, str, int]:
    """
    Corroborate a positive verdict against DNS.

    A domain with no MX cannot receive mail, so a scraped "valid" is demoted.
    Absence of MX is *not* used to confirm invalidity of a negative verdict.
    """
    if status not in VALID_STATUSES:
        return status, reason, confidence
    has_mx, records = _domain_mx(domain)
    if records and not has_mx:
        return STATUS_INVALID, "Domain has no MX records", 90
    if not records:
        return STATUS_UNKNOWN, "Could not resolve MX to corroborate", min(confidence, 40)
    return status, reason, confidence


# =============================================================================
# DECODO TRANSPORT
# =============================================================================

class DecodoError(Exception):
    """Raised when the scraping API itself fails (not an email verdict)."""


def extract_html(payload: Any) -> Optional[str]:
    """Pull the rendered page out of whichever envelope Decodo returns."""
    if isinstance(payload, str):
        return payload if "<" in payload else None
    if not isinstance(payload, dict):
        return None
    for key in ("content", "html", "body"):
        value = payload.get(key)
        if isinstance(value, str) and "<" in value:
            return value
    for key in ("results", "data"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return extract_html(value[0])
        if isinstance(value, dict):
            nested = extract_html(value)
            if nested:
                return nested
    return None


# =============================================================================
# SCRAPINGBEE — managed scraping API with click/fill scenarios
# =============================================================================
#
# Each veriphone lookup is one GET to app.scrapingbee.com/api/v1 carrying a
# js_scenario that fills the phone input, clicks Validate, and waits for the
# result grid. Credits (published): 1 plain, 5 with JavaScript rendering (what
# this needs), 10 premium proxy, 25 premium + JS, 75 stealth + JS. A 500 is not
# charged, and every response carries its real cost in the Spb-cost header.

SCRAPINGBEE_API_URL = "https://app.scrapingbee.com/api/v1"
SCRAPINGBEE_USAGE_URL = "https://app.scrapingbee.com/api/v1/usage"
SCRAPINGBEE_RESULT_WAIT_MS = 6000


def load_scrapingbee_settings(
    user_id: Optional[str] = None,
    base_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """ScrapingBee credentials (env FV_S_SCRAPINGBEE_API_KEY or encrypted)."""
    import os

    settings: Dict[str, Any] = {"api_key": "", "premium_proxy": False, "country_code": ""}

    if user_id and base_path:
        cfg_file = Path(base_path) / f"fv-s-{user_id}" / "validator-config" / "scrapingbee.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                settings["api_key"] = decrypt_secret(saved.get("api_key", "") or "")
                settings["premium_proxy"] = bool(saved.get("premium_proxy"))
                settings["country_code"] = saved.get("country_code") or ""
            except Exception as exc:
                logger.warning("Could not read scrapingbee config for %s: %s", user_id, exc)

    if not settings["api_key"]:
        settings["api_key"] = os.getenv("FV_S_SCRAPINGBEE_API_KEY", "")

    return settings


def save_scrapingbee_settings(
    user_id: str,
    base_path: Path,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist ScrapingBee settings. The API key is encrypted at rest."""
    cfg_dir = Path(base_path) / f"fv-s-{user_id}" / "validator-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "scrapingbee.json"

    existing: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    raw_key = (data.get("api_key") or "").strip()
    if raw_key:
        existing["api_key"] = encrypt_secret(raw_key)
    if "premium_proxy" in data:
        existing["premium_proxy"] = bool(data.get("premium_proxy"))
    if "country_code" in data:
        existing["country_code"] = (data.get("country_code") or "").strip().lower()

    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    return {
        "has_api_key": bool(existing.get("api_key")),
        "premium_proxy": bool(existing.get("premium_proxy")),
        "country_code": existing.get("country_code") or "",
    }


def build_js_scenario(adapter: "SiteAdapter", value: str) -> Dict[str, Any]:
    """
    Translate a site adapter's browser actions into a ScrapingBee js_scenario.

    The adapter stays the single source of truth for selectors: the phone
    adapter's click/input/click/wait sequence becomes fill + click + click +
    wait here, so ScrapingBee drives the same form Decodo and Playwright do.
    """
    instructions: List[Dict[str, Any]] = []
    for action in adapter.browser_actions(value):
        kind = action.get("type")
        selector = (action.get("selector") or {}).get("value")
        if kind == "wait":
            ms = int(float(action.get("wait_time_s", 1)) * 1000)
            # Merge into a trailing wait rather than queueing two sleeps in a row.
            if instructions and "wait" in instructions[-1]:
                instructions[-1]["wait"] += ms
            else:
                instructions.append({"wait": ms})
        elif kind == "click" and selector:
            instructions.append({"click": selector})
        elif kind == "input" and selector:
            instructions.append({"fill": [selector, action.get("value", "")]})

    # The result grid renders after an in-page fetch; give it a fixed window.
    if instructions and "wait" in instructions[-1]:
        instructions[-1]["wait"] += SCRAPINGBEE_RESULT_WAIT_MS
    else:
        instructions.append({"wait": SCRAPINGBEE_RESULT_WAIT_MS})
    return {"instructions": instructions}


def _call_scrapingbee(
    adapter: "SiteAdapter",
    value: str,
    settings: Dict[str, Any],
) -> str:
    """Render one page through ScrapingBee and return its text. Raises DecodoError."""
    api_key = settings.get("api_key")
    if not api_key:
        raise DecodoError(
            "No ScrapingBee API key configured. Set FV_S_SCRAPINGBEE_API_KEY or save "
            "one in the validator settings."
        )

    params: Dict[str, Any] = {
        "api_key": api_key,
        "url": adapter.url,
        "render_js": "true",          # the checker is a SPA; 5 credits/request
        "block_resources": "true",    # drop images/fonts: faster, no extra cost
        "js_scenario": json.dumps(build_js_scenario(adapter, value)),
    }
    if settings.get("premium_proxy"):
        params["premium_proxy"] = "true"
    if settings.get("country_code"):
        params["country_code"] = settings["country_code"]

    try:
        response = requests.get(
            SCRAPINGBEE_API_URL,
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout as exc:
        raise DecodoError("ScrapingBee request timed out") from exc
    except requests.exceptions.RequestException as exc:
        raise DecodoError(f"ScrapingBee request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise DecodoError("ScrapingBee rejected the API key (401/403)")
    if response.status_code == 429:
        raise DecodoError("ScrapingBee rate limit hit (429) — or out of credits")
    if response.status_code == 500:
        # ScrapingBee does not charge for a 500, so retrying is free.
        raise DecodoError("ScrapingBee failed to render the page (500, not charged)")
    if response.status_code != 200:
        snippet = (response.text or "")[:200].replace("\n", " ")
        raise DecodoError(f"ScrapingBee error {response.status_code}: {snippet}")

    credits = response.headers.get("Spb-cost") or response.headers.get("spb-cost")
    if credits:
        logger.info("ScrapingBee charged %s credit(s) for %s", credits, adapter.url)

    if not (response.text or "").strip():
        raise DecodoError("ScrapingBee returned an empty page")
    return response.text


def scrapingbee_usage(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Current credit balance. Costs 0 credits; useful before a big batch."""
    api_key = (settings or {}).get("api_key")
    if not api_key:
        return {"error": "No ScrapingBee API key configured"}
    try:
        response = requests.get(
            SCRAPINGBEE_USAGE_URL,
            params={"api_key": api_key},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        if response.status_code != 200:
            return {"error": f"usage lookup failed ({response.status_code})"}
        return response.json()
    except Exception as exc:
        return {"error": f"usage lookup failed: {exc}"}


def active_scraper_provider(settings: Optional[Dict[str, Any]] = None) -> str:
    """
    Which scraping provider to drive.

    ``decodo`` is the default and behaves exactly as before. ``browser-use``
    swaps the same adapter (same selectors, same parse) onto a metered remote
    Chromium, which matters when a flat plan lapses but a few dollars remain.
    Override per user with ``provider`` in the saved settings, or globally with
    ``FV_S_SCRAPER_PROVIDER``.
    """
    import os

    if settings and settings.get("provider"):
        return str(settings["provider"]).strip().lower()
    return (os.getenv("FV_S_SCRAPER_PROVIDER") or "decodo").strip().lower()


def scrape_site(
    adapter: "SiteAdapter",
    value: str,
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render one verifier page through the active provider and return its text.

    Provider-agnostic on purpose: the adapter owns the selectors and the parser,
    so adding a provider never touches the validation logic.
    """
    provider = active_scraper_provider(settings)

    if provider == "browser-use":
        return _run_browser_use_scrape(adapter, value, load_browser_use_settings())

    if provider == "scrapingbee":
        # Prefer an explicit sub-dict, then a top-level key already in hand
        # (how the batch layer passes it), then whatever is saved/env.
        settings = settings or {}
        sb = settings.get("scrapingbee")
        if not sb:
            sb = settings if settings.get("api_key") else load_scrapingbee_settings()
        return _call_scrapingbee(adapter, value, sb)

    return extract_html(
        _call_decodo(value, adapter, settings if settings is not None else load_decodo_settings())
    )


def _call_decodo(
    email: str,
    adapter: _SiteAdapter,
    settings: Dict[str, Any],
) -> str:
    """Execute one scan and return the rendered HTML. Raises DecodoError."""
    api_key = settings.get("api_key")
    if not api_key:
        raise DecodoError(
            "No Decodo API key configured. Set FV_S_DECODO_API_KEY or save one in "
            "the validator settings."
        )

    payload = {
        "target": settings.get("target_type") or "universal",
        "url": adapter.url,
        "headless": "html",
        "browser_actions": adapter.browser_actions(email),
    }

    try:
        response = requests.post(
            settings.get("api_url") or DECODO_DEFAULT_API_URL,
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "authorization": f"Basic {api_key}",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout as exc:
        raise DecodoError("Decodo request timed out") from exc
    except requests.exceptions.RequestException as exc:
        raise DecodoError(f"Decodo request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise DecodoError("Decodo rejected the API key (401/403)")
    if response.status_code == 429:
        raise DecodoError("Decodo rate limit hit (429)")
    if response.status_code != 200:
        snippet = (response.text or "")[:200].replace("\n", " ")
        raise DecodoError(f"Decodo error {response.status_code}: {snippet}")

    try:
        data = response.json()
    except ValueError as exc:
        raise DecodoError("Decodo returned non-JSON response") from exc

    html = extract_html(data)
    if not html:
        keys = ", ".join(sorted(data)) if isinstance(data, dict) else type(data).__name__
        raise DecodoError(f"Decodo response contained no page HTML (keys: {keys})")
    return html


# =============================================================================
# PUBLIC ENTRY POINTS
# =============================================================================

def validate_one(
    email: str,
    settings: Optional[Dict[str, Any]] = None,
    check_dns: bool = True,
) -> Dict[str, Any]:
    """
    Validate a single address through Decodo.

    Any transport/parse failure returns the blank result with ``error`` set,
    which the REST layer counts as ``failed`` rather than ``invalid``.
    """
    email = (email or "").strip()
    result = _blank_result(email)
    if not email or "@" not in email:
        result["error"] = "Malformed address"
        return result

    settings = settings or load_decodo_settings()
    adapter = _adapter_for(email, settings)
    result["site"] = adapter.name
    domain = result["domain"] or ""

    try:
        html = scrape_site(adapter, email, settings)
    except DecodoError as exc:
        result["error"] = str(exc)
        return result

    text = html_to_text(html)

    lowered = text.lower()
    if any(phrase in lowered for phrase in BLOCKED_PHRASES):
        result["error"] = "Verifier blocked or throttled the request"
        result["status"] = STATUS_UNKNOWN
        return result

    raw = adapter.parse(text)
    result["checks"] = {k: v for k, v in raw.items() if v not in (None, "")}

    # Guard: only trust a page that actually echoed this address.
    echoed = _find_first(
        (r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",),
        text,
    )
    other = {e.lower() for e in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)}
    other.discard(email.lower())
    if other and echoed and echoed.lower() != email.lower():
        result["status"] = STATUS_UNKNOWN
        result["reason"] = "Verifier page showed a different address"
        result["error"] = "Result did not correspond to the submitted address"
        return result

    status, reason, confidence = classify_email(raw)

    if any(phrase in lowered for phrase in INDETERMINATE_PHRASES) and status == STATUS_UNKNOWN:
        reason = "Verifier could not determine the result"

    if check_dns:
        status, reason, confidence = _downgrade(status, reason, confidence, domain)
        has_mx, records = _domain_mx(domain)
        result["mx_found"] = has_mx
        result["mx_records"] = records
        result["mx_record"] = records[0] if records else raw.get("mx_record")

    result["status"] = status
    result["valid"] = status in VALID_STATUSES
    result["reason"] = reason
    result["confidence"] = confidence
    result["category"] = (
        "deliverable" if status == STATUS_VALID
        else "risky" if status in (STATUS_CATCH_ALL, STATUS_RISKY)
        else "undeliverable" if status == STATUS_INVALID
        else "unknown"
    )

    # Cosmetic fields (kept for the list writer / "Export Valid" file).
    result["sub_status"] = raw.get("syntax_status") or raw.get("mailbox_type")
    result["smtp_provider"] = raw.get("smtp_provider") or raw.get("mailbox_type")
    result["did_you_mean"] = raw.get("did_you_mean")
    result["first_name"] = raw.get("first_name")
    result["last_name"] = raw.get("last_name")
    result["account"] = raw.get("mailbox_type")

    if raw.get("free_field"):
        result["free_email"] = raw["free_field"].strip().lower() in ("yes", "true", "1")
    elif raw.get("mailbox_type"):
        mailbox_type = str(raw["mailbox_type"]).lower()
        result["free_email"] = "free" in mailbox_type or "webmail" in mailbox_type

    age = raw.get("domain_age")
    if age:
        match = re.search(r"(\d+)", str(age))
        if match:
            result["domain_age_days"] = int(match.group(1))

    if raw.get("domain_field"):
        candidate = str(raw["domain_field"]).strip().lower()
        if "." in candidate and " " not in candidate:
            result["domain"] = candidate

    if status not in VALID_STATUSES and not result["reason"]:
        result["reason"] = f"Email is {status}"

    return result


def validate_emails(
    emails: List[str],
    settings: Optional[Dict[str, Any]] = None,
    check_dns: bool = True,
    max_workers: int = MAX_WORKERS,
) -> List[Dict[str, Any]]:
    """
    Validate a batch concurrently, preserving input order.

    Decodo bills per request, so the batch is fanned out with a bounded thread
    pool rather than one request per address in series.
    """
    emails = [e for e in (emails or []) if isinstance(e, str)]
    if not emails:
        return []

    settings = settings or load_decodo_settings()

    # A missing key is a configuration error, not a per-address failure:
    # surface it once on every result instead of firing N doomed requests.
    if not settings.get("api_key"):
        results = []
        for email in emails:
            blank = _blank_result(email.strip())
            blank["error"] = (
                "No Decodo API key configured. Set FV_S_DECODO_API_KEY or save one "
                "in the validator settings."
            )
            results.append(blank)
        return results

    workers = max(1, min(len(emails), max(1, max_workers)))

    def _run(address: str) -> Dict[str, Any]:
        try:
            return validate_one(address, settings, check_dns=check_dns)
        except Exception as exc:  # never let one address sink the batch
            logger.exception("Validation crashed for %s", address)
            blank = _blank_result(address.strip())
            blank["error"] = f"Validation error: {exc}"
            return blank

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_run, emails))


async def validate_emails_async(
    emails: List[str],
    settings: Optional[Dict[str, Any]] = None,
    check_dns: bool = True,
) -> List[Dict[str, Any]]:
    """Async wrapper so callers can await without blocking the event loop."""
    return await asyncio.to_thread(validate_emails, emails, settings, check_dns)


# ITU dial codes for the countries the panel works with. Longest prefix wins,
# so 234 (NG) is matched before 23 or 2.
_DIAL_CODES: Dict[str, str] = {
    "US": "1", "CA": "1", "GB": "44", "NG": "234", "GH": "233", "CI": "225",
    "SN": "221", "KE": "254", "TZ": "255", "UG": "256", "MA": "212", "DZ": "213",
    "TN": "216", "EG": "20", "SA": "966", "AE": "971", "BH": "973", "KW": "965",
    "OM": "968", "QA": "974", "IN": "91", "CN": "86", "JP": "81", "KR": "82",
    "AU": "61", "NZ": "64", "BR": "55", "MX": "52", "ES": "34", "FR": "33",
    "DE": "49", "IT": "39", "NL": "31", "BE": "32", "CH": "41", "AT": "43",
    "SE": "46", "NO": "47", "DK": "45", "FI": "358", "PT": "351", "IE": "353",
    "PL": "48", "RU": "7", "TR": "90", "IL": "972", "ZA": "27", "ZW": "263",
    "ZM": "260", "RW": "250", "CM": "237", "BD": "880", "PK": "92", "ID": "62",
    "MY": "60", "SG": "65", "PH": "63", "TH": "66", "VN": "84",
}

# Every dial code, longest first, for prefix matching.
_DIAL_CODES_BY_LENGTH = sorted(set(_DIAL_CODES.values()), key=len, reverse=True)

# Countries whose national numbers are conventionally written with a trunk "0"
# that must be dropped before the dial code (e.g. UK 07911 -> 447911).
_TRUNK_ZERO_COUNTRIES = {"GB", "DE", "FR", "IT", "NL", "BE", "CH", "AT", "SE",
                         "NO", "DK", "FI", "PT", "IE", "PL", "ZA", "NG", "KE",
                         "GH", "AU", "NZ", "IN", "BD", "PK", "ID", "MY", "TH"}


def to_e164(phone_number: str, country_code: str = "") -> str:
    """
    Normalise a number to E.164 for veriphone.

    ``format_phone_number`` in functions.py deliberately strips the dial code
    and returns a national number, so the stored value cannot be sent as-is.
    A supplied ``country_code`` always wins over prefix guessing, because short
    dial codes (7 = RU, 55 = BR) would otherwise capture unrelated national
    numbers.
    """
    raw = (phone_number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return raw

    # Already international.
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]

    cc = (country_code or "").strip().upper()
    dial = _DIAL_CODES.get(cc, "")

    if dial:
        # Trust the hint: only treat the value as international when the dial
        # code is present and enough national digits follow it.
        if digits.startswith(dial) and len(digits) - len(dial) >= 7:
            return "+" + digits
        national = digits
        if cc in _TRUNK_ZERO_COUNTRIES and national.startswith("0"):
            national = national[1:]
        return f"+{dial}{national}"

    # No hint: fall back to longest-prefix detection.
    for code in _DIAL_CODES_BY_LENGTH:
        if digits.startswith(code) and len(digits) - len(code) >= 7:
            return "+" + digits

    return "+" + digits


# E.164 allows at most 15 digits; national significant numbers are at least 6.
_MIN_E164_DIGITS = 6
_MAX_E164_DIGITS = 15


def is_plausible_e164(value: str) -> bool:
    """Cheap structural check: ``+`` then 6-15 digits, no leading zero."""
    if not value or not value.startswith("+"):
        return False
    digits = value[1:]
    if not digits.isdigit() or len(digits) < _MIN_E164_DIGITS:
        return False
    if len(digits) > _MAX_E164_DIGITS:
        return False
    return not digits.startswith("0")


def _blank_phone_result(phone_number: str, country_code: str = "") -> Dict[str, Any]:
    """Phone result with every field the session/list/UI layer reads."""
    return {
        "number": phone_number,
        "country_code": country_code or "",
        "formatted_number": None,
        # Verdict mirror of the email path
        "valid": False,
        "status": STATUS_UNKNOWN,
        "category": "unknown",
        "confidence": 0,
        "reason": None,
        # Veriphone fields
        "line_type": None,
        "carrier": None,
        "country": None,
        "region": None,
        "city": None,
        # Legacy keys kept for stored lists / existing UI bindings
        "is_prepaid": None,
        "is_leaked": None,
        "site": None,
        "e164": None,
        "raw": {},
        "error": None,
    }


# A veriphone "Type" value maps onto a verdict: a real, reachable line is a
# usable number; "invalid"/"unknown" means the checker could not place it.
_PHONE_VALID_TYPES = {"mobile", "landline", "fixed_line", "voip", "fixed_line_or_mobile"}
_PHONE_DEAD_TYPES = {"invalid", "unknown"}


def classify_phone(raw: Dict[str, Any]) -> Tuple[str, str, int]:
    """
    Map scraped veriphone fields to ``(status, reason, confidence)``.

    Mirrors ``classify_email``: an explicit negative wins, then an explicit
    positive signal, and anything else is ``unknown`` — never silently valid.
    """
    valid_field = (raw.get("valid_field") or "").strip().lower()
    line_type = (raw.get("line_type") or "").strip().lower()
    carrier = (raw.get("carrier") or "").strip()
    number = (raw.get("number") or "").strip()

    if valid_field:
        decision = judge_text(valid_field)
        if decision is False:
            return STATUS_INVALID, valid_field, 95
        if decision is True:
            return STATUS_VALID, valid_field, 90

    if line_type:
        normalised = line_type.replace("-", "_").replace(" ", "_")
        if normalised in _PHONE_DEAD_TYPES:
            # "unknown" is an absence of signal, not proof of a dead line.
            return (
                (STATUS_INVALID, f"Type: {line_type}", 80)
                if normalised == "invalid"
                else (STATUS_UNKNOWN, "Number could not be classified", 0)
            )
        if normalised in _PHONE_VALID_TYPES:
            reason = f"{line_type}" + (f" on {carrier}" if carrier else "")
            # A live line type is decent evidence but not definitive: the page
            # can still be showing a cached/partial render.
            return STATUS_VALID, reason, 80 if carrier else 70

    if number:
        # The checker echoed a formatted number back, which only happens on a
        # successful lookup, but without a type/carrier this stays weak.
        return STATUS_UNKNOWN, "Lookup returned a number but no classification", 30

    return STATUS_UNKNOWN, "Verifier returned no conclusive result", 0


def _call_veriphone_api(e164: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call the real veriphone JSON API for one number.

    Returns the parsed JSON body. Raises DecodoError on any transport/auth
    problem so the caller can distinguish "no answer" from "no verdict"
    (the error type is shared deliberately: both means involve the scraping
    vendor, and callers only need failed-vs-unknown).
    """
    api_key = settings.get("api_key")
    if not api_key:
        raise DecodoError(
            "No veriphone API key configured. Set FV_S_VERIPHONE_API_KEY or save one "
            "in the validator settings."
        )

    url = settings.get("api_url") or VERIPHONE_API_URL
    params = {"phone": e164}
    headers = {
        "accept": "application/json",
        # Bearer is the documented form; the query-param form is also accepted
        # by veriphone, so we send both to be resilient to either.
        "authorization": f"Bearer {api_key}",
    }
    params["key"] = api_key

    try:
        response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout as exc:
        raise DecodoError("veriphone request timed out") from exc
    except requests.exceptions.RequestException as exc:
        raise DecodoError(f"veriphone request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise DecodoError("veriphone rejected the API key (401/403)")
    if response.status_code == 429:
        raise DecodoError("veriphone rate limit hit (429)")
    if response.status_code != 200:
        snippet = (response.text or "")[:200].replace("\n", " ")
        raise DecodoError(f"veriphone error {response.status_code}: {snippet}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DecodoError("veriphone returned non-JSON response") from exc

    if not isinstance(payload, dict):
        raise DecodoError("veriphone returned an unexpected payload")

    # veriphone reports failures in-band.
    if str(payload.get("status", "")).lower() == "error":
        raise DecodoError(f"veriphone error: {payload.get('message') or payload.get('error')}")

    return payload


def _result_from_veriphone_api(payload: Dict[str, Any], phone_number: str, e164: str) -> Dict[str, Any]:
    """Map a veriphone API response onto the shared phone-result shape."""
    result = _blank_phone_result(phone_number)
    result["site"] = "veriphone-api"
    result["e164"] = e164
    result["raw"] = payload

    phone_valid = payload.get("phone_valid")
    phone_type = payload.get("phone_type")
    carrier = payload.get("carrier")
    country = payload.get("country")
    region = payload.get("phone_region")

    if isinstance(phone_valid, str):
        phone_valid = phone_valid.strip().lower() in ("true", "yes", "1")

    if phone_valid is True:
        status, confidence = STATUS_VALID, 99
        reason = "Verified by veriphone API"
        # Guard against a mismatched or cached response: if the API echoes a
        # different number, the verdict is not about our input.
        returned = re.sub(r"\D", "", str(payload.get("e164") or payload.get("phone") or ""))
        if returned and returned != re.sub(r"\D", "", e164):
            status, confidence = STATUS_UNKNOWN, 0
            reason = "veriphone API returned a different number"
    elif phone_valid is False:
        status, confidence = STATUS_INVALID, 99
        reason = payload.get("status") or "veriphone reports the number as invalid"
    else:
        status, confidence = STATUS_UNKNOWN, 0
        reason = "veriphone returned no validity verdict"

    # A valid number with a carrier/type is the strongest signal available;
    # mirror the scraped path so downstream buckets stay consistent.
    result["status"] = status
    result["valid"] = status == STATUS_VALID
    result["reason"] = reason
    result["confidence"] = confidence
    result["category"] = (
        "deliverable" if status == STATUS_VALID
        else "undeliverable" if status == STATUS_INVALID
        else "unknown"
    )

    result["formatted_number"] = (
        payload.get("international_number")
        or payload.get("e164")
        or payload.get("local_number")
        or e164
    )
    result["line_type"] = phone_type
    result["carrier"] = carrier
    result["country"] = country
    result["region"] = region

    country_code = payload.get("country_code")
    if country_code:
        result["country_code"] = str(country_code).upper()
    elif country:
        match = re.search(r"\(([A-Za-z]{2})\)", country)
        if match:
            result["country_code"] = match.group(1).upper()

    return result


# =============================================================================
# BROWSER USE CLOUD — optional remote Chromium, driven over CDP
# =============================================================================
#
# A third phone transport, behind the same adapter interface as the Decodo
# scrape, so the result shape does not change. It exists for email validation
# more than phones: the offline engine already answers phone numbers for free.
#
# Cost model, from their published rates: $0.02 per browser-hour (billed by the
# minute, one-minute minimum) plus $5/GB for managed residential proxy traffic
# or $0.20/GB direct, and optionally $0.10 per solved CAPTCHA. The browser is
# charged to its timeout up front and refunded when stopped, so the stop call in
# the finally block is what keeps a failed run from billing the full timeout.

BROWSER_USE_API_URL = "https://api.browser-use.com"
BROWSER_USE_BROWSER_TIMEOUT_S = 60

# Residential proxy country. Browser Use provisions a US residential IP by
# default; blank/"none"/"direct" turns the proxy off (cheaper per GB, but every
# request then leaves from one datacenter IP and the target can rate-limit it).
BROWSER_USE_DEFAULT_PROXY_COUNTRY = "us"
_PROXY_OFF_VALUES = {"", "none", "direct", "off", "false", "0"}
BROWSER_USE_NAV_TIMEOUT_MS = 30000
BROWSER_USE_SETTLE_MS = 2500


def load_browser_use_settings(
    user_id: Optional[str] = None,
    base_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Resolve Browser Use cloud settings for a user.

    Precedence: saved user config -> ``FV_S_BROWSER_USE_API_KEY`` env -> empty.
    ``proxy_country_code`` is passed to the API verbatim; blank means no proxy,
    which is also the cheap option (direct traffic is $0.20/GB, managed
    residential is $5/GB).
    """
    import os

    settings: Dict[str, Any] = {
        "api_key": "",
        "proxy_country_code": BROWSER_USE_DEFAULT_PROXY_COUNTRY,
        "browser_timeout": BROWSER_USE_BROWSER_TIMEOUT_S,
    }

    if user_id and base_path:
        cfg_file = Path(base_path) / f"fv-s-{user_id}" / "validator-config" / "browseruse.json"
        if cfg_file.exists():
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                settings["api_key"] = decrypt_secret(saved.get("api_key", "") or "")
                if saved.get("proxy_country_code") is not None:
                    settings["proxy_country_code"] = saved["proxy_country_code"]
                if saved.get("browser_timeout"):
                    settings["browser_timeout"] = int(saved["browser_timeout"])
            except Exception as exc:
                logger.warning("Could not read browser-use config for %s: %s", user_id, exc)

    if not settings["api_key"]:
        settings["api_key"] = os.getenv("FV_S_BROWSER_USE_API_KEY", "")

    return settings


def save_browser_use_settings(
    user_id: str,
    base_path: Path,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist Browser Use settings. The API key is encrypted at rest."""
    cfg_dir = Path(base_path) / f"fv-s-{user_id}" / "validator-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "browseruse.json"

    existing: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    raw_key = (data.get("api_key") or "").strip()
    if raw_key:
        existing["api_key"] = encrypt_secret(raw_key)

    if "proxy_country_code" in data:
        existing["proxy_country_code"] = (data.get("proxy_country_code") or "").strip().lower()
    if data.get("browser_timeout"):
        try:
            existing["browser_timeout"] = max(10, min(int(data["browser_timeout"]), 240))
        except (TypeError, ValueError):
            pass

    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    return {
        "has_api_key": bool(existing.get("api_key")),
        "proxy_country_code": existing.get("proxy_country_code") or "",
        "browser_timeout": existing.get("browser_timeout") or BROWSER_USE_BROWSER_TIMEOUT_S,
    }


def resolve_proxy_country(settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Resolve the proxy country for a cloud browser.

    Each browser is its own session with its own residential exit IP, so one
    browser per number means one IP per number, which is what keeps the target
    site from rate-limiting a whole batch to a single address.

    Returns None only when the proxy is explicitly switched off -- that traffic
    is $0.20/GB instead of $5/GB but leaves from one datacenter IP.
    """
    raw = (settings or {}).get("proxy_country_code")
    if raw is None:
        return BROWSER_USE_DEFAULT_PROXY_COUNTRY
    value = str(raw).strip().lower()
    if value in _PROXY_OFF_VALUES:
        return None
    return value


def is_browser_use_available() -> bool:
    """True when the SDK and its transport deps are importable."""
    try:
        import browser_use_sdk.v4  # noqa: F401
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


def _run_browser_use_scrape(
    adapter: "SiteAdapter",
    value: str,
    settings: Dict[str, Any],
) -> str:
    """
    Render one page in a Browser Use cloud browser and return its text.

    Synchronous on purpose: the batch processor already runs each number in a
    thread pool, and the SDK's async client would need its own event loop to
    drive Playwright's async API here.
    """
    try:
        from browser_use_sdk.v4 import AsyncBrowserUse
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - import guard
        raise DecodoError(
            "Browser Use transport unavailable: install browser-use-sdk and playwright"
        ) from exc

    api_key = settings.get("api_key")
    if not api_key:
        raise DecodoError(
            "No Browser Use API key configured. Set FV_S_BROWSER_USE_API_KEY."
        )

    try:
        timeout = int(settings.get("browser_timeout") or BROWSER_USE_BROWSER_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = BROWSER_USE_BROWSER_TIMEOUT_S

    async def _create():
        async with AsyncBrowserUse(api_key=api_key) as client:
            return await client.browsers.create(
                proxy_country_code=resolve_proxy_country(settings),
                timeout=timeout,
            )

    try:
        session = asyncio.run(_create())
    except Exception as exc:
        raise DecodoError(f"Browser Use could not create a browser: {exc}") from exc

    session_id = getattr(session, "id", None)
    cdp_url = getattr(session, "cdp_url", None)
    if not cdp_url:
        raise DecodoError("Browser Use returned no CDP URL")

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(cdp_url, timeout=BROWSER_USE_NAV_TIMEOUT_MS)
            except Exception as exc:
                raise DecodoError(f"Could not attach to the Browser Use session: {exc}") from exc

            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_navigation_timeout(BROWSER_USE_NAV_TIMEOUT_MS)
                page.goto(adapter.url, wait_until="domcontentloaded")

                for action in adapter.browser_actions(value):
                    kind = action.get("type")
                    if kind == "wait":
                        page.wait_for_timeout(int(float(action.get("wait_time_s", 1)) * 1000))
                    elif kind == "click":
                        page.click(action["selector"]["value"], timeout=BROWSER_USE_NAV_TIMEOUT_MS)
                    elif kind == "input":
                        page.fill(action["selector"]["value"], action.get("value", ""))

                page.wait_for_timeout(BROWSER_USE_SETTLE_MS)
                text = extract_html(page.content())
                if not text:
                    raise DecodoError("Browser Use returned an empty page")
                return text
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        # Billing runs until the browser stops or its timeout expires. This is
        # the call that stops the meter, so it must survive any failure above.
        async def _stop():
            async with AsyncBrowserUse(api_key=api_key) as client:
                return await client.browsers.stop(session_id)

        try:
            asyncio.run(_stop())
        except Exception:
            pass


# =============================================================================
# EMAIL VALIDATION, OFFLINE — DNS + optional SMTP, no API, no credits
# =============================================================================
#
# What can be settled without a paid vendor:
#
#   1. syntax and normalisation          (RFC 5322, local)
#   2. does the domain accept mail?      (MX lookup, free, DNS)
#   3. is the domain known-disposable?   (local domain list)
#   4. does the mailbox exist?           (SMTP RCPT probe, free, needs port 25)
#
# (4) is the only one that proves the *mailbox*, not just the domain. Many
# hosting networks block outbound port 25, in which case it is skipped and the
# domain-level answer is reported instead -- honestly, as less certain.
#
# What cannot be settled for free: whether a recipient at a catch-all domain
# exists, and reputation/abuse signals. Those stay unknown rather than "valid".

# Common throwaway domains. Not exhaustive; a match is a warning, not proof the
# address is dead, so it maps to ``risky`` rather than ``invalid``.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "10minutemail.com",
    "tempmail.com", "temp-mail.org", "throwawaymail.com", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    "maildrop.cc", "fakeinbox.com", "mailnesia.com", "spam4.me",
    "mintemail.com", "mytemp.email", "tempr.email", "emailondeck.com",
    "moakt.com", "burnermail.io", "mailcatch.com", "inboxkitten.com",
})

# Free consumer providers — not invalid, just worth labelling.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "gmx.com", "gmx.net", "mail.com", "yandex.com", "yandex.ru", "proton.me",
    "protonmail.com", "zoho.com", "tutanota.com",
})

# Role mailboxes: real, but usually not a person.
ROLE_ACCOUNTS = frozenset({
    "admin", "administrator", "postmaster", "hostmaster", "webmaster", "abuse",
    "noreply", "no-reply", "donotreply", "support", "help", "info", "sales",
    "billing", "accounts", "contact", "enquiries", "office", "hello", "team",
    "marketing", "hr", "jobs", "careers", "press", "media", "legal", "privacy",
    "security", "compliance", "root", "sysadmin", "mailer-daemon",
})

_EMAIL_SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _smtp_probe(
    mx_host: str,
    email: str,
    timeout: float = 6.0,
    sender: str = "verify@example.com",
) -> Optional[bool]:
    """
    Ask the recipient's mail server whether the mailbox exists.

    Opens an SMTP conversation and issues ``RCPT TO`` without sending a message,
    then disconnects with ``QUIT``. Returns True (accepted), False (rejected), or
    None when the server declines to answer (greylisting, catch-all, blocked
    port). Never raises.
    """
    import smtplib

    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as smtp:
            smtp.ehlo_or_helo_if_needed()
            code, _ = smtp.mail(sender)
            if code >= 400:
                return None
            code, _ = smtp.rcpt(email)
            if 200 <= code < 300:
                return True
            if code in (550, 551, 553):
                return False
            return None
    except Exception:
        return None


def validate_email_offline(
    email: str,
    check_dns: bool = True,
    smtp_probe: bool = False,
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate one address with no paid service.

    Returns the same shape as the scraped path, with ``site="local"``. The
    verdict ladder, strongest first:

    * SMTP rejected the recipient      -> invalid   (confidence 92)
    * SMTP accepted the recipient      -> valid     (confidence 88)
    * no MX for the domain             -> invalid   (confidence 90)
    * disposable domain                -> risky     (confidence 60)
    * role mailbox                     -> risky     (confidence 55)
    * MX present, mailbox unverified   -> unknown   (confidence 45)
    * unusable syntax                  -> invalid   (confidence 95)

    MX present with no SMTP answer is deliberately ``unknown``: the domain
    demonstrably accepts mail, but nothing proves this mailbox exists.
    """
    email = (email or "").strip()
    out = result if result is not None else _blank_result(email)
    out["site"] = "local"

    # 1. Syntax
    if not email or "@" not in email:
        out.update(status=STATUS_INVALID, valid=False, category="malformed",
                   confidence=95, reason="Not an email address")
        return out
    local_part, domain = email.rsplit("@", 1)
    domain = domain.lower().strip().rstrip(".")
    out["domain"] = domain or None
    out["account"] = local_part or None

    if not _EMAIL_SYNTAX_RE.match(email) or ".." in email or domain.startswith("-"):
        out.update(status=STATUS_INVALID, valid=False, category="malformed",
                   confidence=95, reason="Fails email syntax rules")
        return out

    out["free_email"] = domain in FREE_EMAIL_DOMAINS

    # 2. DNS: can the domain receive mail at all?
    mx_found, mx_records = (False, [])
    if check_dns:
        mx_found, mx_records = _domain_mx(domain)
    out["mx_found"] = mx_found
    out["mx_records"] = mx_records
    out["mx_record"] = mx_records[0] if mx_records else None
    if mx_found and mx_records:
        out["smtp_provider"] = mx_records[0].split(".")[-2] if "." in mx_records[0] else None

    if check_dns and not mx_found:
        out.update(
            status=STATUS_INVALID,
            valid=False,
            category="undeliverable",
            confidence=90,
            reason="Domain has no MX record, so it cannot receive mail",
        )
        out["checks"] = {"syntax": "valid", "mx": "missing"}
        return out

    # 3. SMTP probe: the only thing that proves the mailbox itself.
    smtp_verdict = None
    if smtp_probe and mx_found and mx_records:
        for host in mx_records[:2]:
            smtp_verdict = _smtp_probe(host, email)
            if smtp_verdict is not None:
                break
        out["checks"] = {
            "syntax": "valid",
            "mx": "found",
            "smtp": {True: "accepted", False: "rejected"}.get(smtp_verdict, "no answer"),
        }
    else:
        out["checks"] = {"syntax": "valid", "mx": "found" if mx_found else "not checked"}

    if smtp_verdict is True:
        out.update(status=STATUS_VALID, valid=True, category="deliverable",
                   confidence=88, reason="Mailbox server accepted the recipient")
        return out
    if smtp_verdict is False:
        out.update(status=STATUS_INVALID, valid=False, category="undeliverable",
                   confidence=92, reason="Mailbox server rejected the recipient")
        return out

    # 4. Reputation-style signals that need no network.
    if domain in DISPOSABLE_DOMAINS:
        out.update(status=STATUS_RISKY, valid=False, category="risky",
                   confidence=60, reason="Disposable email domain")
        return out
    if local_part.lower() in ROLE_ACCOUNTS:
        out.update(status=STATUS_RISKY, valid=False, category="risky",
                   confidence=55, reason="Role address rather than a person")
        return out

    out.update(
        status=STATUS_UNKNOWN,
        valid=False,
        category="unknown",
        confidence=45,
        reason="Domain accepts mail, but the mailbox could not be verified"
        + ("" if smtp_probe else " (SMTP probe disabled)"),
    )
    return out


def validate_emails_offline(
    emails: List[str],
    check_dns: bool = True,
    smtp_probe: bool = False,
    max_workers: int = MAX_WORKERS,
) -> List[Dict[str, Any]]:
    """Validate a batch locally, preserving input order."""
    emails = [e for e in (emails or []) if isinstance(e, str)]
    if not emails:
        return []

    workers = max(1, min(len(emails), max(1, max_workers)))

    def _run(address: str) -> Dict[str, Any]:
        try:
            return validate_email_offline(address, check_dns=check_dns, smtp_probe=smtp_probe)
        except Exception as exc:
            logger.exception("Offline validation crashed for %s", address)
            blank = _blank_result(address.strip())
            blank["site"] = "local"
            blank["error"] = f"Validation error: {exc}"
            return blank

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_run, emails))


async def validate_emails_offline_async(
    emails: List[str],
    check_dns: bool = True,
    smtp_probe: bool = False,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        validate_emails_offline, emails, check_dns, smtp_probe
    )


# =============================================================================
# PHONE VALIDATION, OFFLINE — free, no API, no credits
# =============================================================================
#
# Google's libphonenumber (the ``phonenumbers`` port) ships the numbering plan
# of every country: which prefixes are assigned, how long the national number
# must be, which carrier holds each prefix, and what kind of line it is. That is
# the same source veriphone answers from, and answering it locally costs
# nothing per lookup.
#
# This is only possible for phone numbers. An email's existence can only be
# told by a mail server or a validation vendor, which is why the email path
# still needs Decodo. Numbers are decidable from a public table.

try:  # optional at import time so the app still boots without the package
    import phonenumbers
    from phonenumbers import (
        PhoneNumberType,
        carrier as _pn_carrier,
        geocoder as _pn_geocoder,
        timezone as _pn_timezone,
    )

    PHONENUMBERS_AVAILABLE = True
except Exception:  # pragma: no cover - exercised by the "not installed" test
    phonenumbers = None
    PHONENUMBERS_AVAILABLE = False


def is_offline_engine_available() -> bool:
    """True when local validation can run (the ``phonenumbers`` package loaded)."""
    return bool(PHONENUMBERS_AVAILABLE)


_PN_TYPE_NAMES = {
    "MOBILE": "mobile",
    "FIXED_LINE": "fixed_line",
    "FIXED_LINE_OR_MOBILE": "fixed_line_or_mobile",
    "TOLL_FREE": "toll_free",
    "PREMIUM_RATE": "premium_rate",
    "SHARED_COST": "shared_cost",
    "VOIP": "voip",
    "PERSONAL_NUMBER": "personal_number",
    "PAGER": "pager",
    "UAN": "uan",
    "VOICEMAIL": "voicemail",
    "UNKNOWN": "unknown",
}

# Line types that mean a real, reachable line.
_PN_REACHABLE_TYPES = {
    "mobile",
    "fixed_line",
    "fixed_line_or_mobile",
    "voip",
    "toll_free",
    "personal_number",
}


def _pn_type_name(number) -> str:
    """Human line type for a parsed number (``mobile``, ``fixed_line``, …)."""
    raw = phonenumbers.number_type(number)
    for name, value in vars(PhoneNumberType).items():
        if name.startswith("_") or not isinstance(value, int):
            continue
        if value == raw:
            return _PN_TYPE_NAMES.get(name, name.lower())
    return "unknown"


def validate_phone_offline(
    phone_number: str,
    country_code: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate a phone number with no network call at all.

    Returns the same result shape as the network paths, with ``site="local"``
    so stored lists show where a verdict came from. An unparseable or
    unassigned number is ``invalid``; a number that is structurally sound but
    whose line type is unknown stays ``unknown`` — never silently valid.
    """
    out = result if result is not None else _blank_phone_result(phone_number, country_code)
    out["site"] = "local"

    if not PHONENUMBERS_AVAILABLE:
        out["status"] = STATUS_UNKNOWN
        out["category"] = "unknown"
        out["reason"] = "Offline validation unavailable (phonenumbers not installed)"
        out["error"] = "phonenumbers package is not installed"
        return out

    region = (country_code or "").strip().upper() or None
    candidate = (phone_number or "").strip()

    parsed = None
    for attempt in (candidate, to_e164(candidate, country_code)):
        try:
            parsed = phonenumbers.parse(attempt, region)
            break
        except Exception:
            parsed = None
    if parsed is None:
        out.update(status=STATUS_INVALID, valid=False, category="malformed",
                   confidence=0, reason="Not a parseable phone number")
        return out

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    out["e164"] = e164
    out["country_code"] = (
        phonenumbers.region_code_for_number(parsed) or out.get("country_code") or ""
    )

    # 1. Assigned to a real numbering plan?
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        out.update(
            status=STATUS_INVALID,
            valid=False,
            category="undeliverable",
            confidence=98,
            reason="Not a valid number for its numbering plan",
            formatted_number=phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
            ),
        )
        return out

    # 2. It is assigned. Fill in type / carrier / region from the same plan.
    line_type = _pn_type_name(parsed)
    carrier_name = _pn_carrier.name_for_number(parsed, "en") or None
    region_name = _pn_geocoder.description_for_number(parsed, "en") or None
    try:
        tz = ",".join(_pn_timezone.time_zones_for_number(parsed)) or None
    except Exception:
        tz = None

    out["line_type"] = line_type
    out["carrier"] = carrier_name
    out["region"] = region_name
    out["city"] = None
    out["formatted_number"] = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    out["raw"] = {"timezones": tz, "country_prefix": str(parsed.country_code)}

    if line_type in _PN_REACHABLE_TYPES:
        out.update(
            status=STATUS_VALID,
            valid=True,
            category="deliverable",
            confidence=95 if carrier_name else 85,
            reason="Valid for its numbering plan"
            + (f", {line_type}" if line_type else "")
            + (f" on {carrier_name}" if carrier_name else ""),
        )
    else:
        # Assigned, but we cannot say the line is live.
        out.update(
            status=STATUS_UNKNOWN,
            valid=False,
            category="unknown",
            confidence=40,
            reason=f"Assigned number, unrecognised line type ({line_type})",
        )
    return out


def validate_phone_via_scrapingbee(
    phone_number: str,
    country_code: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate one number by driving the veriphone form through ScrapingBee.

    Same contract as the other scrape paths: the adapter supplies the selectors
    (turned into a js_scenario), ``classify_phone`` decides the verdict, and a
    transport failure becomes ``error`` rather than a silent "invalid".
    """
    result = _blank_phone_result(phone_number, country_code)
    result["site"] = "scrapingbee"

    if not phone_number:
        result["error"] = "Empty phone number"
        return result

    settings = settings if settings is not None else load_scrapingbee_settings()
    if not settings.get("api_key"):
        result["error"] = (
            "No ScrapingBee API key configured. Set FV_S_SCRAPINGBEE_API_KEY or save "
            "one in the validator settings."
        )
        return result

    adapter = _ADAPTERS["veriphone"]
    e164 = to_e164(phone_number, country_code)
    result["e164"] = e164

    try:
        html = _call_scrapingbee(adapter, e164, settings)
    except DecodoError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = f"ScrapingBee failed: {exc}"
        return result

    raw = adapter.parse(html)
    status, reason, confidence = classify_phone(raw)
    result["raw"] = raw
    result["status"] = status
    result["valid"] = status == STATUS_VALID
    result["reason"] = reason
    result["confidence"] = confidence
    result["category"] = (
        "deliverable" if status == STATUS_VALID
        else "undeliverable" if status == STATUS_INVALID
        else "unknown"
    )
    result["formatted_number"] = raw.get("number") or e164
    result["line_type"] = raw.get("line_type")
    result["carrier"] = raw.get("carrier")
    result["country"] = raw.get("country")
    result["region"] = raw.get("region")
    result["city"] = raw.get("city")
    return result


def validate_phone_via_browser_use(
    phone_number: str,
    country_code: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate one number by rendering veriphone.io in a Browser Use cloud browser.

    Same contract as ``validate_phone_via_decodo``: the adapter decides where to
    type and what to read, ``classify_phone`` decides the verdict, and any
    transport failure becomes ``error`` rather than a silent "invalid".
    """
    result = _blank_phone_result(phone_number, country_code)
    result["site"] = "browser-use"

    if not phone_number:
        result["error"] = "Empty phone number"
        return result

    settings = settings if settings is not None else load_browser_use_settings()
    if not settings.get("api_key"):
        result["error"] = (
            "No Browser Use API key configured. Set FV_S_BROWSER_USE_API_KEY or save "
            "one in the validator settings."
        )
        return result
    if not is_browser_use_available():
        result["error"] = (
            "Browser Use transport unavailable: install browser-use-sdk and playwright"
        )
        return result

    adapter = _ADAPTERS["veriphone"]
    e164 = to_e164(phone_number, country_code)
    result["e164"] = e164

    try:
        html = _run_browser_use_scrape(adapter, e164, settings)
    except DecodoError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = f"Browser Use failed: {exc}"
        return result

    raw = adapter.parse(html)
    status, reason, confidence = classify_phone(raw)
    result["raw"] = raw
    result["status"] = status
    result["valid"] = status == STATUS_VALID
    result["reason"] = reason
    result["confidence"] = confidence
    result["category"] = (
        "deliverable" if status == STATUS_VALID
        else "undeliverable" if status == STATUS_INVALID
        else "unknown"
    )
    result["formatted_number"] = raw.get("number") or e164
    result["line_type"] = raw.get("line_type")
    result["carrier"] = raw.get("carrier")
    result["country"] = raw.get("country")
    result["region"] = raw.get("region")
    result["city"] = raw.get("city")
    return result


def validate_phone(
    phone_number: str,
    country_code: str = "",
    settings: Optional[Dict[str, Any]] = None,
    decodo_settings: Optional[Dict[str, Any]] = None,
    browser_use_settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate a phone number, preferring the veriphone JSON API.

    Order of preference:
      1. veriphone API  -- one GET, no browser, no HTML parsing
      2. Decodo scrape of veriphone.io -- only if no API key is configured

    If an API key *is* configured but the call fails, the error is returned
    rather than silently falling back, so a bad key is visible instead of
    costing extra scraping credits.
    """
    phone_number = (phone_number or "").strip()
    if not phone_number:
        result = _blank_phone_result(phone_number, country_code)
        result["error"] = "Empty phone number"
        return result

    e164 = to_e164(phone_number, country_code)

    # A value that cannot be a telephone number must never be reported valid.
    # Truncated input ("+069977249") can come from a mis-parsed list or a bad
    # country hint; do not spend a lookup on it and do not mark it good.
    if not is_plausible_e164(e164):
        result = _blank_phone_result(phone_number, country_code)
        result["site"] = "local"
        result["e164"] = e164 or None
        result["category"] = "malformed"
        result["reason"] = "Not a plausible international number"
        result["status"] = STATUS_UNKNOWN
        return result

    vp = settings if settings is not None else load_veriphone_settings()

    if vp.get("api_key"):
        try:
            payload = _call_veriphone_api(e164, vp)
            return _result_from_veriphone_api(payload, phone_number, e164)
        except DecodoError as exc:
            result = _blank_phone_result(phone_number, country_code)
            result["site"] = "veriphone-api"
            result["e164"] = e164
            result["error"] = str(exc)
            return result

    # An explicitly requested browser transport wins over the free engine: the
    # operator asked for it, and it is the only path that can render a page the
    # offline tables cannot answer. Note it takes its OWN credentials -- passing
    # the Decodo settings here silently failed the key check.
    import os

    chosen = (
        (decodo_settings or {}).get("transport")
        or (settings or {}).get("transport")
        or os.getenv("FV_S_SCRAPER_PROVIDER")
        or ""
    ).strip().lower()

    if chosen == "scrapingbee":
        return validate_phone_via_scrapingbee(
            phone_number, country_code, load_scrapingbee_settings()
        )
    if chosen == "browser-use":
        return validate_phone_via_browser_use(
            phone_number,
            country_code,
            browser_use_settings
            if browser_use_settings is not None
            else load_browser_use_settings(),
        )

    # No API key. Validate locally first: this costs nothing and settles the
    # overwhelming majority of numbers (well-formed, assigned, carrier known).
    if is_offline_engine_available():
        local = validate_phone_offline(phone_number, country_code)
        if local.get("status") != STATUS_UNKNOWN or not decodo_settings:
            return local
        # Structurally fine but no verdict, and the caller has Decodo to spend:
        # fall through and let the paid transport try to settle it.
        scraped = validate_phone_via_decodo(phone_number, country_code, decodo_settings)
        if scraped.get("error") or scraped.get("status") == STATUS_UNKNOWN:
            return local
        return scraped

    # Offline engine not installed: fall back to scraping through Decodo.
    return validate_phone_via_decodo(phone_number, country_code, decodo_settings)


def validate_phone_via_decodo(
    phone_number: str,
    country_code: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Validate a phone number through Decodo against veriphone.io.

    Same contract as ``validate_one``: transport problems return a result with
    ``error`` set (counted as *failed*), while a page that simply gives nothing
    away returns ``status="unknown"``.
    """
    phone_number = (phone_number or "").strip()
    result = _blank_phone_result(phone_number, country_code)

    if not phone_number:
        result["error"] = "Empty phone number"
        return result

    settings = settings or load_decodo_settings()
    adapter = _ADAPTERS["veriphone"]
    result["site"] = adapter.name

    # veriphone needs the international form; stored numbers are national.
    e164 = to_e164(phone_number, country_code)
    result["e164"] = e164

    try:
        html = scrape_site(adapter, e164, settings)
    except DecodoError as exc:
        result["error"] = str(exc)
        return result

    text = html_to_text(html)
    lowered = text.lower()
    if any(phrase in lowered for phrase in BLOCKED_PHRASES):
        result["error"] = "Verifier blocked or throttled the request"
        return result

    raw = adapter.parse(text)
    result["raw"] = {k: v for k, v in raw.items() if v not in (None, "")}

    # Guard: only trust a page that echoed the number we submitted.
    submitted_digits = re.sub(r"\D", "", e164)
    echoed_digits = re.sub(r"\D", "", raw.get("number") or "")
    if echoed_digits and submitted_digits and not (
        echoed_digits.endswith(submitted_digits[-7:])
        or submitted_digits.endswith(echoed_digits[-7:])
    ):
        result["status"] = STATUS_UNKNOWN
        result["reason"] = "Verifier page showed a different number"
        result["error"] = "Result did not correspond to the submitted number"
        return result

    status, reason, confidence = classify_phone(raw)
    result["status"] = status
    result["valid"] = status == STATUS_VALID
    result["reason"] = reason
    result["confidence"] = confidence
    result["category"] = (
        "deliverable" if status == STATUS_VALID
        else "undeliverable" if status == STATUS_INVALID
        else "unknown"
    )

    result["formatted_number"] = raw.get("number") or e164
    result["line_type"] = raw.get("line_type")
    result["carrier"] = raw.get("carrier")
    result["country"] = raw.get("country")
    result["region"] = raw.get("region")
    result["city"] = raw.get("city")

    # Derive the ISO country code from "Nigeria (NG)" when we have no hint.
    if not result["country_code"] and result["country"]:
        match = re.search(r"\(([A-Za-z]{2})\)", result["country"])
        if match:
            result["country_code"] = match.group(1).upper()

    return result


async def validate_phones_async(
    numbers: List[str],
    country_code: str = "",
    settings: Optional[Dict[str, Any]] = None,
    veriphone_settings: Optional[Dict[str, Any]] = None,
    browser_use_settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Concurrent wrapper used by the batch endpoint."""
    numbers = [n for n in (numbers or []) if isinstance(n, str)]
    if not numbers:
        return []

    def _run(number: str) -> Dict[str, Any]:
        try:
            return validate_phone(
                number,
                country_code,
                settings=veriphone_settings,
                decodo_settings=settings,
                browser_use_settings=browser_use_settings,
            )
        except Exception as exc:  # never let one number sink the batch
            logger.exception("Phone validation crashed for %s", number)
            blank = _blank_phone_result(number, country_code)
            blank["error"] = f"Validation error: {exc}"
            return blank

    workers = max(1, min(len(numbers), MAX_WORKERS))

    def _run_all() -> List[Dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_run, numbers))

    return await asyncio.to_thread(_run_all)
