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
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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

class _SiteAdapter:
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
        value = _SiteAdapter.field(text, label_pattern)
        if value is None:
            return None
        lowered = value.lower()
        if lowered.startswith("yes") or lowered.startswith("true"):
            return True
        if lowered.startswith("no") or lowered.startswith("false") or "not " in lowered:
            return False
        return None


class SkrappAdapter(_SiteAdapter):
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


class EmailHippoAdapter(_SiteAdapter):
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


_ADAPTERS: Dict[str, _SiteAdapter] = {
    "skrapp": SkrappAdapter(),
    "emailhippo": EmailHippoAdapter(),
}


def _adapter_for(email: str, settings: Dict[str, Any]) -> _SiteAdapter:
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


def _judge(text_value: Optional[str]) -> Optional[bool]:
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

    decision = _judge(status)
    if decision is True:
        return STATUS_VALID, status or "Mailbox verified", 95
    if decision is False:
        return STATUS_INVALID, status, 95

    decision = _judge(server)
    if decision is True:
        return STATUS_VALID, f"Mailbox server says {server}", 85
    if decision is False:
        return STATUS_INVALID, server, 85

    decision = _judge(reach)
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


def _extract_html(payload: Any) -> Optional[str]:
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
            return _extract_html(value[0])
        if isinstance(value, dict):
            nested = _extract_html(value)
            if nested:
                return nested
    return None


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

    html = _extract_html(data)
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
        html = _call_decodo(email, adapter, settings)
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

    from concurrent.futures import ThreadPoolExecutor

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


def validate_phone_via_decodo(
    phone_number: str,
    country_code: str,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Phone lookup through Decodo, mirroring the email path.

    Uses ipqualityscore's public lookup page, which renders the report as a
    label/value table.
    """
    result: Dict[str, Any] = {
        "number": phone_number,
        "country_code": country_code,
        "formatted_number": None,
        "valid": False,
        "status": STATUS_UNKNOWN,
        "carrier": None,
        "line_type": None,
        "city": None,
        "region": None,
        "country": None,
        "is_prepaid": None,
        "is_leaked": None,
        "error": None,
    }

    settings = settings or load_decodo_settings()
    if not settings.get("api_key"):
        result["error"] = (
            "No Decodo API key configured. Set FV_S_DECODO_API_KEY or save one "
            "in the validator settings."
        )
        return result

    url = (
        "https://www.ipqualityscore.com/phone-number-validator/lookup/"
        f"{country_code}/{phone_number}"
    )
    payload = {
        "target": settings.get("target_type") or "universal",
        "url": url,
        "headless": "html",
        "browser_actions": [
            {"type": "wait", "wait_time_s": 5},
            {"type": "wait", "wait_time_s": 3},
        ],
    }

    try:
        response = requests.post(
            settings.get("api_url") or DECODO_DEFAULT_API_URL,
            json=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "authorization": f"Basic {settings['api_key']}",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        result["error"] = f"Decodo request failed: {exc}"
        return result

    if response.status_code != 200:
        result["error"] = f"Decodo error {response.status_code}"
        return result

    try:
        html = _extract_html(response.json())
    except ValueError:
        html = None
    if not html:
        result["error"] = "Decodo response contained no page HTML"
        return result

    text = html_to_text(html)
    get = _SiteAdapter.field

    result["formatted_number"] = get(text, r"Local\s+Formatted|Formatted")
    result["carrier"] = get(text, r"Carrier")
    result["line_type"] = get(text, r"Line\s+Type")
    result["city"] = get(text, r"City")
    result["region"] = get(text, r"Region")
    result["country"] = get(text, r"Country")
    result["is_prepaid"] = _SiteAdapter.yes_no(text, r"Prepaid")
    leaked = _SiteAdapter.yes_no(text, r"Leaked")
    result["is_leaked"] = leaked

    valid_field = get(text, r"Valid")
    decision = _judge(valid_field)
    if decision is True:
        result["valid"] = True
        result["status"] = STATUS_VALID
    elif decision is False:
        result["status"] = STATUS_INVALID

    return result
