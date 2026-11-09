"""Tests for the Decodo-only email validator (email_validator.py).

These exercise the parsing/verdict engine against representative rendered
HTML, with no network access: the Decodo call is monkeypatched.
"""
import json
import pytest
from concurrent.futures import ThreadPoolExecutor

import email_validator as ev
import functions


# ---------------------------------------------------------------- fixtures
SKRAPP_VALID = """
<html><body><div class="styles_single-results-container">
  <p class="css-glb8qx">someone@gmail.com</p>
  <div class="row"><span>Email Syntax</span><span>Valid</span></div>
  <div class="row"><span>Email Status</span><span>Valid</span></div>
  <div class="row"><span>Mailbox Server</span><span>Valid</span></div>
  <div class="row"><span>Domain</span><span>gmail.com</span></div>
  <div class="row"><span>Reachability</span><span>Successfully reachable</span></div>
  <div class="row"><span>Mailbox Type</span><span>Webmail</span></div>
</div></body></html>
"""

# The trap the old parser fell into: a syntax-only "Valid" and a page that
# contains the word "invalid" only inside an unrelated style/svg blob.
SKRAPP_DEAD_MAILBOX = """
<html><body>
  <style>.invalid{border:1px solid red}</style>
  <div class="styles_single-results-container">
    <p class="css-glb8qx">ghost@example.com</p>
    <div class="row"><span>Email Syntax</span><span>Valid</span></div>
    <div class="row"><span>Email Status</span><span>Invalid</span></div>
    <div class="row"><span>Mailbox Server</span><span>Invalid</span></div>
    <div class="row"><span>Reachability</span><span>Not reachable</span></div>
  </div>
</body></html>
"""

SKRAPP_CATCH_ALL = """
<html><body><div class="styles_single-results-container">
  <div class="row"><span>Email Status</span><span>Valid</span></div>
  <div class="row"><span>Mailbox Type</span><span>Catch-all</span></div>
  <div class="row"><span>Reachability</span><span>Successfully reachable</span></div>
</div></body></html>
"""

SKRAPP_RATE_LIMITED = """
<html><body><h1>You have reached the maximum number of validations</h1>
<p>Please sign up for a free account</p></body></html>
"""

SKRAPP_UNPARSEABLE = """
<html><body><div>Something went wrong. Please try again.</div></body></html>
"""

HIPPO_VALID = """
<html><body><table>
  <tr><td>Email address</td><td>someone@yahoo.com</td></tr>
  <tr><td>Format</td><td>Valid</td></tr>
  <tr><td>Email validity</td><td>Valid</td></tr>
  <tr><td>MX record</td><td>mta5.am0.yahoodns.net</td></tr>
  <tr><td>Deliverability</td><td>Deliverable</td></tr>
</table></body></html>
"""


def _patch(monkeypatch, html=None, error=None):
    def fake_call(email, adapter, settings):
        if error:
            raise ev.DecodoError(error)
        return html

    monkeypatch.setattr(ev, "_call_decodo", fake_call)
    # Neutralise DNS so tests never touch the network.
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (True, ["mx.example.com"]))


SETTINGS = {"api_key": "test-key", "api_url": "http://x", "target_url": ev.SKRAPP_URL, "target_type": "universal"}


# ---------------------------------------------------------------- html_to_text
def test_html_to_text_splits_rows_and_strips_tags():
    text = ev.html_to_text("<div><span>Email Status</span><span>Valid</span></div>")
    assert "Email Status" in text
    assert "Valid" in text
    assert "<" not in text


def test_html_to_text_drops_scripts_and_styles():
    text = ev.html_to_text("<style>.valid{color:red}</style><p>Email Status</p>")
    assert "color:red" not in text
    assert "valid" not in text.lower()


# ---------------------------------------------------------------- happy paths
def test_valid_email(monkeypatch):
    _patch(monkeypatch, SKRAPP_VALID)
    result = ev.validate_one("someone@gmail.com", SETTINGS)
    assert result["status"] == ev.STATUS_VALID
    assert result["valid"] is True
    assert result["category"] == "deliverable"
    assert result["site"] == "skrapp"
    assert result["domain"] == "gmail.com"
    assert result["free_email"] is True


def test_dead_mailbox_is_invalid_not_valid(monkeypatch):
    """Regression: the old substring fallback scored this as valid."""
    _patch(monkeypatch, SKRAPP_DEAD_MAILBOX)
    result = ev.validate_one("ghost@example.com", SETTINGS)
    assert result["status"] == ev.STATUS_INVALID
    assert result["valid"] is False


def test_catch_all_is_flagged_and_still_deliverable(monkeypatch):
    _patch(monkeypatch, SKRAPP_CATCH_ALL)
    result = ev.validate_one("x@catchall.example", SETTINGS)
    assert result["status"] == ev.STATUS_CATCH_ALL
    assert result["valid"] is True
    assert result["category"] == "risky"


def test_yahoo_uses_emailhippo_adapter(monkeypatch):
    seen = {}

    def fake_call(email, adapter, settings):
        seen["site"] = adapter.name
        return HIPPO_VALID

    monkeypatch.setattr(ev, "_call_decodo", fake_call)
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (True, ["mx.example.com"]))
    result = ev.validate_one("someone@yahoo.com", SETTINGS)
    assert seen["site"] == "emailhippo"
    assert result["status"] == ev.STATUS_VALID


# ---------------------------------------------------------------- failure modes
def test_blocked_page_counts_as_error_not_verdict(monkeypatch):
    _patch(monkeypatch, SKRAPP_RATE_LIMITED)
    result = ev.validate_one("a@b.com", SETTINGS)
    assert result["error"]
    assert result["valid"] is False


def test_unparseable_page_is_unknown_never_valid(monkeypatch):
    _patch(monkeypatch, SKRAPP_UNPARSEABLE)
    result = ev.validate_one("a@b.com", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False
    assert result["confidence"] == 0


def test_transport_error_is_reported_not_swallowed(monkeypatch):
    _patch(monkeypatch, error="Decodo rejected the API key (401/403)")
    result = ev.validate_one("a@b.com", SETTINGS)
    assert "401" in result["error"]
    assert result["valid"] is False


def test_missing_api_key_fails_fast_without_requests(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call Decodo without a key")

    monkeypatch.setattr(ev.requests, "post", boom)
    results = ev.validate_emails(["a@b.com", "c@d.com"], {"api_key": ""})
    assert called["n"] == 0
    assert len(results) == 2
    assert all("No Decodo API key" in r["error"] for r in results)


def test_mismatched_address_on_page_is_rejected(monkeypatch):
    _patch(monkeypatch, SKRAPP_VALID)  # page mentions someone@gmail.com
    result = ev.validate_one("other@example.com", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["error"]


# ---------------------------------------------------------------- DNS corroboration
def test_no_mx_downgrades_scraped_valid(monkeypatch):
    _patch(monkeypatch, SKRAPP_VALID)
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (False, []))
    result = ev.validate_one("someone@gmail.com", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_dns_failure_never_upgrades_unknown(monkeypatch):
    _patch(monkeypatch, SKRAPP_UNPARSEABLE)
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (True, ["mx.example.com"]))
    result = ev.validate_one("a@b.com", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN


# ---------------------------------------------------------------- judgement
@pytest.mark.parametrize("text,expected", [
    ("Valid", True),
    ("Successfully reachable", True),
    ("Deliverable", True),
    ("Invalid", False),
    ("Not reachable", False),
    ("undeliverable", False),
    ("does not exist", False),
    ("unknown", None),
    (None, None),
    ("", None),
])
def test_judge_negatives_win_over_positives(text, expected):
    assert ev.judge_text(text) is expected


def test_judge_invalid_beats_valid_substring():
    # "invalid" contains "valid" -- must stay negative.
    assert ev.judge_text("Email is invalid") is False


# ---------------------------------------------------------------- batch
def test_batch_preserves_order(monkeypatch):
    def fake_call(email, adapter, settings):
        return SKRAPP_VALID.replace("someone@gmail.com", email)

    monkeypatch.setattr(ev, "_call_decodo", fake_call)
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (True, ["mx.example.com"]))
    emails = [f"user{i}@example.com" for i in range(12)]
    results = ev.validate_emails(emails, SETTINGS)
    assert [r["email"] for r in results] == emails


def test_batch_survives_one_crash(monkeypatch):
    def fake_call(email, adapter, settings):
        if email.startswith("bad"):
            raise RuntimeError("boom")
        return SKRAPP_VALID.replace("someone@gmail.com", email)

    monkeypatch.setattr(ev, "_call_decodo", fake_call)
    monkeypatch.setattr(ev, "_domain_mx", lambda domain: (True, ["mx.example.com"]))
    results = ev.validate_emails(["bad@example.com", "ok@example.com"], SETTINGS)
    assert "boom" in results[0]["error"]
    assert results[1]["status"] == ev.STATUS_VALID


# =============================================================================
# PHONE VALIDATOR (veriphone.io via Decodo)
# =============================================================================

VERIPHONE_MOBILE = """
<html><body><div style="display:grid">
  <span>Number</span><span>+234 706 997 7249</span>
  <span>Type</span><span>mobile</span>
  <span>Carrier</span><span>MTN</span>
  <span>Country</span><span>Nigeria (NG)</span>
</div></body></html>
"""

VERIPHONE_INVALID = """
<html><body><div style="display:grid">
  <span>Number</span><span>+1 000 000 0000</span>
  <span>Type</span><span>invalid</span>
  <span>Carrier</span><span></span>
  <span>Country</span><span>United States (US)</span>
</div></body></html>
"""

VERIPHONE_UNCLASSIFIED = """
<html><body><div style="display:grid">
  <span>Number</span><span>+234 706 997 7249</span>
  <span>Type</span><span>unknown</span>
</div></body></html>
"""

VERIPHONE_EMPTY = """
<html><body><h1>Enter a phone number to validate</h1></body></html>
"""


def _patch_phone(monkeypatch, html=None, error=None):
    def fake_call(value, adapter, settings):
        if error:
            raise ev.DecodoError(error)
        return html

    monkeypatch.setattr(ev, "_call_decodo", fake_call)


SETTINGS = {"api_key": "test-key", "api_url": "http://x",
            "target_url": ev.SKRAPP_URL, "target_type": "universal"}


def test_veriphone_adapter_targets_the_phone_form():
    adapter = ev._ADAPTERS["veriphone"]
    assert adapter.url == "https://veriphone.io/"
    actions = adapter.browser_actions("+2347069977249")
    selectors = [a.get("selector", {}).get("value") for a in actions if a.get("selector")]
    # Stable ids from the page, not hashed build classes.
    assert "#phone-input" in selectors
    assert "#checker-submit" in selectors
    # The number must actually be typed into the input.
    assert any(a.get("type") == "input" and a.get("value") == "+2347069977249" for a in actions)


def test_veriphone_parse_extracts_the_result_grid():
    text = ev.html_to_text(VERIPHONE_MOBILE)
    raw = ev._ADAPTERS["veriphone"].parse(text)
    assert raw["number"] == "+234 706 997 7249"
    assert raw["line_type"] == "mobile"
    assert raw["carrier"] == "MTN"
    assert raw["country"] == "Nigeria (NG)"


def test_mobile_number_is_valid(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_MOBILE)
    result = ev.validate_phone_via_decodo("+2347069977249", "NG", SETTINGS)
    assert result["status"] == ev.STATUS_VALID
    assert result["valid"] is True
    assert result["carrier"] == "MTN"
    assert result["line_type"] == "mobile"
    assert result["confidence"] >= 70
    assert result["site"] == "veriphone"


def test_invalid_type_is_invalid(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_INVALID)
    result = ev.validate_phone_via_decodo("+10000000000", "US", SETTINGS)
    assert result["status"] == ev.STATUS_INVALID
    assert result["valid"] is False


def test_unclassified_type_is_unknown_not_valid(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_UNCLASSIFIED)
    result = ev.validate_phone_via_decodo("+2347069977249", "NG", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_page_without_result_is_unknown(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_EMPTY)
    result = ev.validate_phone_via_decodo("+2347069977249", "NG", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_phone_transport_error_is_reported(monkeypatch):
    _patch_phone(monkeypatch, error="Decodo rate limit hit (429)")
    result = ev.validate_phone_via_decodo("+2347069977249", "NG", SETTINGS)
    assert "429" in result["error"]
    assert result["valid"] is False


def test_phone_missing_key_fails_without_request(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call Decodo")

    monkeypatch.setattr(ev.requests, "post", boom)
    result = ev.validate_phone_via_decodo("+2347069977249", "NG", {"api_key": ""})
    assert called["n"] == 0
    assert "No Decodo API key" in result["error"]


def test_phone_mismatched_number_is_rejected(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_MOBILE)  # page shows +234 706 997 7249
    result = ev.validate_phone_via_decodo("+15551234567", "US", SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["error"]


def test_phone_country_code_derived_from_page(monkeypatch):
    _patch_phone(monkeypatch, VERIPHONE_MOBILE)
    result = ev.validate_phone_via_decodo("+2347069977249", "", SETTINGS)
    assert result["country_code"] == "NG"
    assert result["country"] == "Nigeria (NG)"


@pytest.mark.parametrize("raw,expected", [
    ({"line_type": "mobile", "carrier": "MTN"}, ev.STATUS_VALID),
    ({"line_type": "landline"}, ev.STATUS_VALID),
    ({"line_type": "voip", "carrier": "Twilio"}, ev.STATUS_VALID),
    ({"line_type": "fixed line"}, ev.STATUS_VALID),
    ({"line_type": "invalid"}, ev.STATUS_INVALID),
    ({"line_type": "unknown"}, ev.STATUS_UNKNOWN),
    ({"valid_field": "invalid", "line_type": "mobile"}, ev.STATUS_INVALID),
    ({"valid_field": "valid"}, ev.STATUS_VALID),
    ({"number": "+234 706 997 7249"}, ev.STATUS_UNKNOWN),
    ({}, ev.STATUS_UNKNOWN),
])
def test_classify_phone(raw, expected):
    status, _reason, _confidence = ev.classify_phone(raw)
    assert status == expected


def test_empty_cell_does_not_leak_next_label():
    """Regression: an empty Carrier cell made Country bleed into carrier."""
    html = """
    <div><span>Number</span><span>+234 706 997 7249</span>
    <span>Type</span><span>invalid</span>
    <span>Carrier</span><span></span>
    <span>Country</span><span>Nigeria (NG)</span></div>
    """
    raw = ev._ADAPTERS["veriphone"].parse(ev.html_to_text(html))
    assert raw["carrier"] is None
    assert raw["country"] == "Nigeria (NG)"


# ---------------------------------------------------------------- E.164
@pytest.mark.parametrize("number,country,expected", [
    ("+2347069977249", "NG", "+2347069977249"),
    ("7069977249", "NG", "+2347069977249"),      # national, hint wins over RU's "7"
    ("07069977249", "NG", "+2347069977249"),     # trunk zero dropped
    ("5551234567", "US", "+15551234567"),
    ("07911123456", "GB", "+447911123456"),
    ("+447911123456", "", "+447911123456"),
    ("447911123456", "", "+447911123456"),       # detection when no hint
    ("002347069977249", "", "+2347069977249"),   # 00-prefixed international
])
def test_to_e164(number, country, expected):
    assert ev.to_e164(number, country) == expected


def test_validator_sends_e164_not_the_national_number(monkeypatch):
    """The stored value is national; veriphone must receive the dial code."""
    seen = {}

    def fake_call(value, adapter, settings):
        seen["value"] = value
        return VERIPHONE_MOBILE

    monkeypatch.setattr(ev, "_call_decodo", fake_call)
    ev.validate_phone_via_decodo("7069977249", "NG", SETTINGS)
    assert seen["value"] == "+2347069977249"


# =============================================================================
# PHONE VALIDATOR — direct veriphone JSON API (no browser)
# =============================================================================

VP_SETTINGS = {"api_key": "vp-test-key", "api_url": ev.VERIPHONE_API_URL}

VP_OK = {
    "status": "success",
    "phone": "+2347069977249",
    "phone_valid": True,
    "phone_type": "mobile",
    "carrier": "MTN Nigeria",
    "country": "Nigeria",
    "country_code": "NG",
    "phone_region": "Lagos",
    "international_number": "+234 706 997 7249",
    "e164": "+2347069977249",
}

VP_INVALID = {**VP_OK, "phone_valid": False, "phone_type": None, "carrier": None}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_api_path_is_used_when_a_key_exists(monkeypatch):
    """With a key, Decodo must never be called at all."""
    called = {"decodo": 0, "api": 0, "url": None, "params": None, "headers": None}

    def fake_decodo(*a, **k):
        called["decodo"] += 1
        raise AssertionError("Decodo must not be used when the API key is set")

    def fake_get(url, params=None, headers=None, timeout=None):
        called["api"] += 1
        called["url"] = url
        called["params"] = params
        called["headers"] = headers
        return _FakeResponse(200, VP_OK)

    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    monkeypatch.setattr(ev.requests, "get", fake_get)

    result = ev.validate_phone("7069977249", "NG", settings=VP_SETTINGS)

    assert called["decodo"] == 0
    assert called["api"] == 1
    assert called["url"] == ev.VERIPHONE_API_URL
    # E.164 is what gets sent, not the national number.
    assert called["params"]["phone"] == "+2347069977249"
    assert called["headers"]["authorization"] == "Bearer vp-test-key"

    assert result["status"] == ev.STATUS_VALID
    assert result["valid"] is True
    assert result["carrier"] == "MTN Nigeria"
    assert result["line_type"] == "mobile"
    assert result["country"] == "Nigeria"
    assert result["country_code"] == "NG"
    assert result["region"] == "Lagos"
    assert result["confidence"] == 99
    assert result["site"] == "veriphone-api"
    assert result["formatted_number"] == "+234 706 997 7249"


def test_api_invalid_number(monkeypatch):
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(200, VP_INVALID))
    result = ev.validate_phone("+2340000000000", "NG", settings=VP_SETTINGS)
    assert result["status"] == ev.STATUS_INVALID
    assert result["valid"] is False
    assert result["category"] == "undeliverable"


def test_no_key_validates_locally_without_touching_the_network(monkeypatch):
    """The free offline engine is the default; nothing may be spent."""
    called = {"decodo": 0, "api": 0}

    def fake_decodo(*a, **k):
        called["decodo"] += 1
        return VERIPHONE_MOBILE

    def fake_get(*a, **k):
        called["api"] += 1
        return _FakeResponse(200, VP_OK)

    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    monkeypatch.setattr(ev.requests, "get", fake_get)

    result = ev.validate_phone(
        "7069977249", "NG", settings={"api_key": ""}, decodo_settings={"api_key": "d"}
    )

    assert called["decodo"] == 0
    assert called["api"] == 0
    assert result["site"] == "local"
    assert result["status"] == ev.STATUS_VALID
    assert result["carrier"] == "MTN"


def test_scrape_is_used_when_the_offline_engine_is_unavailable(monkeypatch):
    called = {"decodo": 0}

    def fake_decodo(*a, **k):
        called["decodo"] += 1
        return VERIPHONE_MOBILE

    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    monkeypatch.setattr(ev, "is_offline_engine_available", lambda: False)

    result = ev.validate_phone(
        "7069977249", "NG", settings={"api_key": ""}, decodo_settings={"api_key": "d"}
    )
    assert called["decodo"] == 1
    assert result["site"] == "veriphone"


def test_offline_unknown_number_falls_through_to_decodo_when_configured(monkeypatch):
    """A structurally fine number with no local verdict may use the paid path."""
    called = {"decodo": 0}

    def fake_offline(number, country_code="", result=None):
        out = ev._blank_phone_result(number, country_code)
        out["site"] = "local"
        out["status"] = ev.STATUS_UNKNOWN
        return out

    def fake_decodo(*a, **k):
        called["decodo"] += 1
        return VERIPHONE_MOBILE

    monkeypatch.setattr(ev, "validate_phone_offline", fake_offline)
    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    monkeypatch.setattr(ev, "is_offline_engine_available", lambda: True)

    result = ev.validate_phone(
        "+2347069977249", "NG", settings={"api_key": ""}, decodo_settings={"api_key": "d"}
    )
    assert called["decodo"] == 1
    assert result["status"] == ev.STATUS_VALID


def test_offline_unknown_number_stays_unknown_without_decodo(monkeypatch):
    def fake_offline(number, country_code="", result=None):
        out = ev._blank_phone_result(number, country_code)
        out["status"] = ev.STATUS_UNKNOWN
        return out

    called = {"decodo": 0}
    monkeypatch.setattr(ev, "validate_phone_offline", fake_offline)
    monkeypatch.setattr(ev, "_call_decodo", lambda *a, **k: called.__setitem__("decodo", 1))
    monkeypatch.setattr(ev, "is_offline_engine_available", lambda: True)

    result = ev.validate_phone("+2347069977249", "NG", settings={"api_key": ""})
    assert called["decodo"] == 0
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_api_error_does_not_silently_fall_back(monkeypatch):
    """A bad key must surface, not burn scraping credits behind your back."""
    called = {"decodo": 0}

    def fake_decodo(*a, **k):
        called["decodo"] += 1
        raise AssertionError("must not fall back on API error")

    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(401, None, "unauthorized"))

    result = ev.validate_phone("7069977249", "NG", settings=VP_SETTINGS)
    assert called["decodo"] == 0
    assert "401" in result["error"]
    assert result["valid"] is False


@pytest.mark.parametrize("status_code,fragment", [
    (429, "rate limit"),
    (500, "500"),
])
def test_api_http_errors_are_reported(status_code, fragment, monkeypatch):
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(status_code, None, "boom"))
    result = ev.validate_phone("7069977249", "NG", settings=VP_SETTINGS)
    assert fragment.lower() in result["error"].lower()


def test_api_in_band_error_status_is_raised(monkeypatch):
    payload = {"status": "error", "message": "invalid api key"}
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(200, payload))
    result = ev.validate_phone("7069977249", "NG", settings=VP_SETTINGS)
    assert "invalid api key" in result["error"]


def test_api_timeout_is_reported(monkeypatch):
    def boom(*a, **k):
        raise ev.requests.exceptions.Timeout()

    monkeypatch.setattr(ev.requests, "get", boom)
    result = ev.validate_phone("7069977249", "NG", settings=VP_SETTINGS)
    assert "timed out" in result["error"]


def test_api_missing_validity_field_is_unknown(monkeypatch):
    payload = {k: v for k, v in VP_OK.items() if k != "phone_valid"}
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(200, payload))
    result = ev.validate_phone("+2347069977249", "NG", settings=VP_SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_api_string_boolean_is_coerced(monkeypatch):
    payload = {**VP_OK, "phone_valid": "true"}
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(200, payload))
    result = ev.validate_phone("+2347069977249", "NG", settings=VP_SETTINGS)
    assert result["status"] == ev.STATUS_VALID


def test_veriphone_settings_round_trip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", "test-secret")
    monkeypatch.delenv("FV_S_VERIPHONE_API_KEY", raising=False)

    saved = ev.save_veriphone_settings("u1", tmp_path, {"api_key": "super-secret"})
    assert saved["has_api_key"] is True

    raw = (tmp_path / "fv-s-u1" / "validator-config" / "veriphone.json").read_text()
    assert "super-secret" not in raw  # encrypted at rest

    loaded = ev.load_veriphone_settings("u1", tmp_path)
    assert loaded["api_key"] == "super-secret"


def test_veriphone_key_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FV_S_VERIPHONE_API_KEY", "env-key")
    assert ev.load_veriphone_settings("nobody", tmp_path)["api_key"] == "env-key"


def test_implausible_number_is_rejected_without_calling_the_api(monkeypatch):
    """Truncated input must never be blessed as valid by the API."""
    called = {"api": 0}

    def fake_api(e164, settings):
        called["api"] += 1
        return VP_OK

    monkeypatch.setattr(ev, "_call_veriphone_api", fake_api)

    result = ev.validate_phone("+069977249", "NG", settings=VP_SETTINGS)
    assert called["api"] == 0
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False
    assert result["category"] == "malformed"


@pytest.mark.parametrize("value,expected", [
    ("+2347069977249", True),
    ("+447911123456", True),
    ("+069977249", False),          # leading zero after +
    ("+2347", False),               # too short
    ("+1234567890123456", False),   # 16 digits
    ("2347069977249", False),       # no +
])
def test_is_plausible_e164(value, expected):
    assert ev.is_plausible_e164(value) is expected


def test_api_verdict_about_a_different_number_is_not_trusted(monkeypatch):
    """A cached/mismatched echo must not validate the wrong number."""
    payload = {**VP_OK, "phone_valid": True, "e164": "+447911123456"}
    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: _FakeResponse(200, payload))

    result = ev.validate_phone("+2347069977249", "NG", settings=VP_SETTINGS)
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False
    assert "different number" in result["reason"]


def test_phone_start_queues_numbers_verbatim_and_infers_country(tmp_path, monkeypatch):
    """Regression: /phone/start queued mangled numbers and dropped the country."""
    import asyncio
    import json

    import api

    user_id = "queuetest"
    base = tmp_path
    (base / f"fv-s-{user_id}" / "phone-lists").mkdir(parents=True)
    (base / f"fv-s-{user_id}" / "validator-sessions").mkdir(parents=True)
    (base / f"fv-s-{user_id}" / "validator-active").mkdir(parents=True)
    (base / f"fv-s-{user_id}" / "logs.json").write_text("{}")
    (base / f"fv-s-{user_id}" / "phone-lists" / "L1.json").write_text(json.dumps({
        "id": "L1", "name": "phones",
        "numbers": [
            {"number": "07069977249", "country_code": "NG"},
            {"number": "+14155552671", "country_code": "US"},
        ],
    }))
    monkeypatch.setattr(api, "USERS_BASE_PATH", base)

    response = asyncio.run(api.start_phone_validation({"list_id": "L1"}, {"user_id": user_id}))

    session = json.loads(
        (base / f"fv-s-{user_id}" / "validator-sessions" / f"{response['session_id']}.json")
        .read_text()
    )
    assert session["queue"] == ["07069977249", "+14155552671"]
    assert session["country_code"] == "NG"
    assert response["transport"] in ("veriphone-api", "local", "decodo-scrape")


# =============================================================================
# PHONE VALIDATOR — optional Browser Use cloud browser (CDP)
# =============================================================================

def test_browser_use_requires_a_key(monkeypatch):
    result = ev.validate_phone_via_browser_use("+2347069977249", "NG", settings={})
    assert result["error"]
    assert "API key" in result["error"]
    assert result["valid"] is False


def test_browser_use_unavailable_without_the_sdk(monkeypatch):
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: False)
    result = ev.validate_phone_via_browser_use(
        "+2347069977249", "NG", settings={"api_key": "k"}
    )
    assert "unavailable" in result["error"]


VP_PAGE = (
    "Number +234 706 997 7249\nType mobile\nCarrier MTN\n"
    "Country Nigeria (NG)\nValid Valid\n"
)


def test_browser_use_renders_the_page_and_classifies(monkeypatch):
    """Happy path: adapter drives the page, classify_phone decides the verdict."""
    captured = {}

    def fake_scrape(adapter, value, settings):
        captured["adapter"] = adapter
        assert value == "+2347069977249"
        return VP_PAGE

    monkeypatch.setattr(ev, "_run_browser_use_scrape", fake_scrape)
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: True)

    result = ev.validate_phone_via_browser_use(
        "07069977249", "NG", settings={"api_key": "k"}
    )
    assert captured["adapter"].name == "veriphone"
    assert result["site"] == "browser-use"
    assert result["status"] == ev.STATUS_VALID
    assert result["carrier"] == "MTN"
    assert result["valid"] is True


def test_browser_use_failure_is_an_error_not_an_invalid(monkeypatch):
    def boom(*a, **k):
        raise ev.DecodoError("browser died")

    monkeypatch.setattr(ev, "_run_browser_use_scrape", boom)
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: True)

    result = ev.validate_phone_via_browser_use(
        "+2347069977249", "NG", settings={"api_key": "k"}
    )
    assert result["error"] == "browser died"
    assert result["status"] != ev.STATUS_INVALID


def test_browser_use_is_only_used_when_explicitly_chosen(monkeypatch):
    """The free offline engine stays the default even if the SDK is installed."""
    called = {"browser": 0}
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: True)
    monkeypatch.setattr(
        ev, "validate_phone_via_browser_use",
        lambda *a, **k: called.__setitem__("browser", called["browser"] + 1),
    )

    ev.validate_phone("07069977249", "NG", settings={"api_key": ""}, decodo_settings={})
    assert called["browser"] == 0

    ev.validate_phone(
        "07069977249", "NG",
        settings={"api_key": ""}, decodo_settings={"transport": "browser-use"},
    )
    assert called["browser"] == 1


def test_browser_use_stops_the_browser_even_when_the_page_fails(monkeypatch):
    """
    Billing runs until stop(), so the stop call must happen on failure too.
    Rendering is faked; the SDK client and Playwright are stubbed.
    """
    events = []

    class FakeSession:
        id = "sess-1"
        cdp_url = "ws://cdp"

    class FakeBrowsers:
        async def create(self, **kwargs):
            events.append(("create", kwargs))
            return FakeSession()

        async def stop(self, session_id):
            events.append(("stop", session_id))

    class FakeClient:
        def __init__(self, api_key=None, **kw):
            self.browsers = FakeBrowsers()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakePage:
        def set_default_navigation_timeout(self, ms):
            pass

        def goto(self, url, wait_until=None):
            events.append(("goto", url))
            raise RuntimeError("navigation exploded")

        def wait_for_timeout(self, ms):
            pass

    class FakeContext:
        pages = []

        def new_page(self):
            return FakePage()

    class FakeChromium:
        def connect_over_cdp(self, url, timeout=None):
            events.append(("cdp", url))
            raise RuntimeError("could not attach")

    class FakePW:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakePlaywrightModule:
        @staticmethod
        def sync_playwright():
            return FakePW()

    import sys
    import types

    fake_sdk = types.ModuleType("browser_use_sdk")
    v4 = types.ModuleType("browser_use_sdk.v4")
    v4.AsyncBrowserUse = FakeClient
    fake_sdk.v4 = v4
    fake_pw = types.ModuleType("playwright")
    fake_pw.sync_api = FakePlaywrightModule()

    monkeypatch.setitem(sys.modules, "browser_use_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "browser_use_sdk.v4", v4)
    monkeypatch.setitem(sys.modules, "playwright", fake_pw)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", FakePlaywrightModule())
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: True)

    with pytest.raises(ev.DecodoError):
        ev._run_browser_use_scrape(ev._ADAPTERS["veriphone"], "+2347069977249", {"api_key": "k"})

    kinds = [e[0] for e in events]
    assert "create" in kinds
    assert ("stop", "sess-1") in events, f"browser was not stopped: {events}"
    # Residential proxy is on by default: one session, one exit IP.
    create_kwargs = [e[1] for e in events if e[0] == "create"][0]
    assert create_kwargs.get("proxy_country_code") == "us"


def test_browser_use_settings_are_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", "s3cret")
    monkeypatch.delenv("FV_S_BROWSER_USE_API_KEY", raising=False)

    saved = ev.save_browser_use_settings(
        "u9", tmp_path, {"api_key": "bu-secret", "proxy_country_code": "US", "browser_timeout": 15}
    )
    assert saved["has_api_key"] is True

    raw = (tmp_path / "fv-s-u9" / "validator-config" / "browseruse.json").read_text()
    assert "bu-secret" not in raw

    loaded = ev.load_browser_use_settings("u9", tmp_path)
    assert loaded["api_key"] == "bu-secret"
    assert loaded["proxy_country_code"] == "us"
    assert loaded["browser_timeout"] == 15


def test_browser_use_gets_its_own_credentials_not_decodos(monkeypatch):
    """Regression: the browser-use branch was handed Decodo's settings."""
    seen = {}

    def fake(browser_settings):
        seen["key"] = browser_settings.get("api_key")
        out = ev._blank_phone_result("+2347069977249", "NG")
        out["status"] = ev.STATUS_VALID
        out["valid"] = True
        out["site"] = "browser-use"
        return out

    monkeypatch.setattr(
        ev, "validate_phone_via_browser_use",
        lambda number, cc, s=None: fake(s or {}),
    )

    ev.validate_phone(
        "+2347069977249", "NG",
        settings={"api_key": ""},
        decodo_settings={"transport": "browser-use", "api_key": "DECODO-KEY"},
        browser_use_settings={"api_key": "BROWSER-USE-KEY"},
    )
    assert seen["key"] == "BROWSER-USE-KEY"


# =============================================================================
# EMAIL VALIDATOR — free offline engine (no vendor, no credits)
# =============================================================================

def test_offline_email_rejects_bad_syntax():
    for bad in ("not-an-email", "no@tld", "a b@c.com", "user@@x.com"):
        r = ev.validate_email_offline(bad, check_dns=False)
        assert r["status"] == ev.STATUS_INVALID, bad
        assert r["valid"] is False


def test_offline_email_no_mx_is_invalid(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (False, []))
    r = ev.validate_email_offline("user@nowhere-xyz.com")
    assert r["status"] == ev.STATUS_INVALID
    assert "MX" in r["reason"]
    assert r["confidence"] == 90


def test_offline_email_mx_without_smtp_is_unknown_not_valid(monkeypatch):
    """The domain accepts mail; that does not prove the mailbox exists."""
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    r = ev.validate_email_offline("someone@example.com")
    assert r["status"] == ev.STATUS_UNKNOWN
    assert r["valid"] is False
    assert r["mx_found"] is True


def test_offline_email_disposable_is_risky(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.mailinator.com"]))
    r = ev.validate_email_offline("someone@mailinator.com")
    assert r["status"] == ev.STATUS_RISKY


def test_offline_email_role_address_is_risky(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    r = ev.validate_email_offline("admin@example.com")
    assert r["status"] == ev.STATUS_RISKY


def test_offline_email_smtp_accept_makes_it_valid(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    monkeypatch.setattr(ev, "_smtp_probe", lambda host, email, **k: True)
    r = ev.validate_email_offline("someone@example.com", smtp_probe=True)
    assert r["status"] == ev.STATUS_VALID
    assert r["valid"] is True
    assert r["checks"]["smtp"] == "accepted"


def test_offline_email_smtp_reject_makes_it_invalid(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    monkeypatch.setattr(ev, "_smtp_probe", lambda host, email, **k: False)
    r = ev.validate_email_offline("ghost@example.com", smtp_probe=True)
    assert r["status"] == ev.STATUS_INVALID
    assert r["confidence"] == 92


def test_offline_email_smtp_no_answer_stays_unknown(monkeypatch):
    """Greylisting or a blocked port must not be read as a rejection."""
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    monkeypatch.setattr(ev, "_smtp_probe", lambda host, email, **k: None)
    r = ev.validate_email_offline("someone@example.com", smtp_probe=True)
    assert r["status"] == ev.STATUS_UNKNOWN
    assert r["valid"] is False


def test_offline_email_batch_preserves_order(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    addrs = [f"user{i}@example.com" for i in range(12)]
    out = ev.validate_emails_offline(addrs)
    assert [r["email"] for r in out] == addrs


def test_offline_email_flags_free_provider(monkeypatch):
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["mx.example.com"]))
    r = ev.validate_email_offline("someone@gmail.com")
    assert r["free_email"] is True
    assert r["site"] == "local"


def test_smtp_probe_never_raises_on_a_dead_host():
    assert ev._smtp_probe("host.invalid.zzz", "a@b.com", timeout=0.5) is None


# =============================================================================
# PROVIDER SWAP — Decodo stays the default, others plug into the same adapters
# =============================================================================

def test_decodo_is_the_default_provider(monkeypatch):
    monkeypatch.delenv("FV_S_SCRAPER_PROVIDER", raising=False)
    assert ev.active_scraper_provider({}) == "decodo"
    assert ev.active_scraper_provider(None) == "decodo"


def test_provider_can_be_overridden_per_settings_or_env(monkeypatch):
    assert ev.active_scraper_provider({"provider": "browser-use"}) == "browser-use"
    monkeypatch.setenv("FV_S_SCRAPER_PROVIDER", "decodo")
    assert ev.active_scraper_provider({}) == "decodo"


def test_scrape_site_uses_decodo_by_default(monkeypatch):
    seen = {}

    def fake_decodo(value, adapter, settings):
        seen["value"] = value
        seen["adapter"] = adapter.name
        return "<html><body><pre>Email Status Valid</pre></body></html>"

    monkeypatch.setattr(ev, "_call_decodo", fake_decodo)
    out = ev.scrape_site(ev._ADAPTERS["skrapp"], "a@b.com", {"provider": "decodo"})
    assert seen["adapter"] == "skrapp"
    assert "Email Status Valid" in out


def test_scrape_site_can_use_browser_use_without_touching_decodo(monkeypatch):
    called = {"decodo": 0, "browser": 0}

    monkeypatch.setattr(ev, "_call_decodo", lambda *a: called.__setitem__("decodo", 1))
    monkeypatch.setattr(
        ev, "_run_browser_use_scrape",
        lambda adapter, value, settings: called.__setitem__("browser", 1) or "page text",
    )
    monkeypatch.setattr(ev, "load_browser_use_settings", lambda *a, **k: {"api_key": "k"})

    out = ev.scrape_site(ev._ADAPTERS["skrapp"], "a@b.com", {"provider": "browser-use"})
    assert out == "page text"
    assert called["decodo"] == 0
    assert called["browser"] == 1


def test_email_validation_works_through_the_provider_swap(monkeypatch):
    """The same adapter+parser drives whichever provider is configured."""
    monkeypatch.setattr(
        ev, "_run_browser_use_scrape",
        lambda adapter, value, settings: (
            "Email Status Valid\nSyntax Valid\nMX Record Found\n"
            "SMTP Provider google\nDomain gmail.com\n"
        ),
    )
    monkeypatch.setattr(ev, "load_browser_use_settings", lambda *a, **k: {"api_key": "k"})
    monkeypatch.setattr(ev, "_domain_mx", lambda d: (True, ["gmail-smtp-in.l.google.com"]))

    result = ev.validate_one("someone@gmail.com", {"provider": "browser-use", "api_key": "k"})
    assert result["status"] == ev.STATUS_VALID
    assert result["site"] == "skrapp"


# =============================================================================
# PROXY — one residential IP per browser session
# =============================================================================

@pytest.mark.parametrize("settings,expected", [
    (None, "us"),                                   # Browser Use's own default
    ({}, "us"),
    ({"proxy_country_code": "us"}, "us"),
    ({"proxy_country_code": "GB"}, "gb"),           # normalised
    ({"proxy_country_code": "de"}, "de"),
    ({"proxy_country_code": ""}, None),             # explicitly off
    ({"proxy_country_code": "none"}, None),
    ({"proxy_country_code": "direct"}, None),
    ({"proxy_country_code": "off"}, None),
])
def test_resolve_proxy_country(settings, expected):
    assert ev.resolve_proxy_country(settings) == expected


def test_a_fresh_browser_is_created_per_lookup_so_ips_rotate(monkeypatch):
    """
    One number = one browser = one residential exit IP. Reusing a single browser
    for a whole batch would pin every lookup to the same IP and invite limiting.
    """
    created = []

    def fake_scrape(adapter, value, settings):
        created.append((value, settings.get("proxy_country_code")))
        return "Number +234 706 997 7249\nType mobile\nCarrier MTN\nValid Valid"

    monkeypatch.setattr(ev, "_run_browser_use_scrape", fake_scrape)
    monkeypatch.setattr(ev, "is_browser_use_available", lambda: True)

    numbers = ["+2347069977249", "+2348031234567", "+14155552671"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(
            lambda n: ev.validate_phone_via_browser_use(n, "", settings={"api_key": "k"}),
            numbers,
        ))

    assert len(created) == 3, "one browser per number is required for IP rotation"


# =============================================================================
# SCRAPINGBEE — veriphone web automation through js_scenario
# =============================================================================

def test_js_scenario_drives_the_veriphone_form():
    sc = ev.build_js_scenario(ev._ADAPTERS["veriphone"], "+2347069977249")
    ins = sc["instructions"]
    assert {"fill": ["#phone-input", "+2347069977249"]} in ins
    assert {"click": "#checker-submit"} in ins
    # The form is submitted, then the readiness check runs after it.
    submit = ins.index({"click": "#checker-submit"})
    first_check = next(i for i, a in enumerate(ins) if "evaluate" in a)
    assert submit < first_check
    # Enough attempts to outlast a slow in-page fetch, but bounded.
    polls = sum(1 for a in ins if "evaluate" in a)
    assert polls == ev.SCRAPINGBEE_READY_POLLS
    assert polls > 1


def test_js_scenario_is_built_from_the_email_adapter_too():
    sc = ev.build_js_scenario(ev._ADAPTERS["skrapp"], "a@b.com")
    ins = sc["instructions"]
    assert any("fill" in i for i in ins)
    assert any("click" in i for i in ins)


def test_scrapingbee_request_shape(monkeypatch):
    captured = {}

    class R:
        status_code = 200
        text = "Number +234 706 997 7249\nType mobile\nCarrier MTN\nValid Valid"
        headers = {"Spb-cost": "5"}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return R()

    monkeypatch.setattr(ev.requests, "get", fake_get)
    out = ev._call_scrapingbee(ev._ADAPTERS["veriphone"], "+2347069977249", {"api_key": "SB"})

    assert captured["url"] == ev.SCRAPINGBEE_API_URL
    p = captured["params"]
    assert p["api_key"] == "SB"
    assert p["render_js"] == "true"
    assert p["url"] == ev.VERIPHONE_URL
    assert json.loads(p["js_scenario"])["instructions"]
    assert captured["headers"]["Authorization"] == "Bearer SB"
    assert "Number" in out


def test_scrapingbee_uses_premium_proxy_only_when_asked(monkeypatch):
    captured = {}

    class R:
        status_code = 200
        # A phone render must contain the result grid, or the transport now
        # reports "the form did not submit" instead of returning the page.
        text = (
            "<body><div><span>Number</span><span>+234 706 997 7249</span>"
            "<span>Type</span><span>mobile</span></div></body>"
        )
        headers = {}

    monkeypatch.setattr(ev.requests, "get",
                        lambda url, params=None, **k: captured.update(p=params) or R())

    ev._call_scrapingbee(ev._ADAPTERS["veriphone"], "+1", {"api_key": "SB"})
    assert "premium_proxy" not in captured["p"]

    ev._call_scrapingbee(
        ev._ADAPTERS["veriphone"], "+1",
        {"api_key": "SB", "premium_proxy": True, "country_code": "us"},
    )
    assert captured["p"]["premium_proxy"] == "true"
    assert captured["p"]["country_code"] == "us"


@pytest.mark.parametrize("status,fragment", [
    (401, "rejected the API key"),
    (429, "rate limit"),
    (500, "not charged"),      # 500s are free, so the message says so
])
def test_scrapingbee_errors_are_readable(status, fragment, monkeypatch):
    class R:
        status_code = status
        text = "boom"
        headers = {}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())
    with pytest.raises(ev.DecodoError) as exc:
        ev._call_scrapingbee(ev._ADAPTERS["veriphone"], "+1", {"api_key": "SB"})
    assert fragment in str(exc.value)


def test_scrapingbee_requires_a_key():
    with pytest.raises(ev.DecodoError) as exc:
        ev._call_scrapingbee(ev._ADAPTERS["veriphone"], "+1", {})
    assert "API key" in str(exc.value)


def test_phone_validation_end_to_end_via_scrapingbee(monkeypatch):
    class R:
        status_code = 200
        text = ("Number +234 706 997 7249\nType mobile\nCarrier MTN\n"
                "Country Nigeria (NG)\nValid Valid")
        headers = {"Spb-cost": "5"}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())

    result = ev.scrape_site(
        ev._ADAPTERS["veriphone"], "+2347069977249",
        {"provider": "scrapingbee", "api_key": "SB"},
    )
    raw = ev._ADAPTERS["veriphone"].parse(result)
    status, _, _ = ev.classify_phone(raw)
    assert status == ev.STATUS_VALID
    assert raw["carrier"] == "MTN"


def test_scrapingbee_settings_round_trip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", "s3cret")
    monkeypatch.delenv("FV_S_SCRAPINGBEE_API_KEY", raising=False)

    saved = ev.save_scrapingbee_settings(
        "u7", tmp_path, {"api_key": "sb-secret", "premium_proxy": True, "country_code": "US"}
    )
    assert saved["has_api_key"] is True

    raw = (tmp_path / "fv-s-u7" / "validator-config" / "scrapingbee.json").read_text()
    assert "sb-secret" not in raw

    loaded = ev.load_scrapingbee_settings("u7", tmp_path)
    assert loaded["api_key"] == "sb-secret"
    assert loaded["premium_proxy"] is True
    assert loaded["country_code"] == "us"


def test_usage_lookup_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(ev.requests, "get", boom)
    assert "error" in ev.scrapingbee_usage({"api_key": "k"})
    assert "error" in ev.scrapingbee_usage({})


def test_explicit_scrapingbee_choice_beats_the_free_engine(monkeypatch):
    """
    Asking for scrapingbee must run the web automation, not the local tables --
    the whole point is the automation.
    """
    called = {"local": 0, "sb": 0}

    monkeypatch.setattr(
        ev, "validate_phone_offline",
        lambda *a, **k: called.__setitem__("local", called["local"] + 1),
    )
    monkeypatch.setattr(
        ev, "validate_phone_via_scrapingbee",
        lambda n, c, s=None: called.__setitem__("sb", called["sb"] + 1) or {"status": "valid"},
    )

    ev.validate_phone(
        "+2347069977249", "NG",
        settings={"api_key": ""},
        decodo_settings={"transport": "scrapingbee", "api_key": "decodo"},
        browser_use_settings={"api_key": ""},
    )
    assert called["sb"] == 1
    assert called["local"] == 0


def test_scrapingbee_phone_path_end_to_end(monkeypatch):
    class R:
        status_code = 200
        text = ("Number +1 423 631 9525\nType mobile\nCarrier T-Mobile\n"
                "Country United States (US)\nValid Valid")
        headers = {"Spb-cost": "5"}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())
    r = ev.validate_phone_via_scrapingbee("+1 (423) 631-9525", "", settings={"api_key": "SB"})
    assert r["status"] == ev.STATUS_VALID
    assert r["carrier"] == "T-Mobile"
    assert r["site"] == "scrapingbee"
    assert r["e164"] == "+14236319525"


def test_scrapingbee_phone_path_requires_a_key():
    r = ev.validate_phone_via_scrapingbee("+1", "", settings={})
    assert "API key" in r["error"]
    assert r["valid"] is False


# =============================================================================
# LIVE PROVIDER SELECTION (admin panel: paste a key, switch transport)
# =============================================================================

def _isolate_provider_env(monkeypatch):
    for var in ("FV_S_SCRAPER_PROVIDER", "FV_S_SCRAPINGBEE_API_KEY",
                "FV_S_DECODO_API_KEY", "FV_S_BROWSER_USE_API_KEY",
                "FV_S_VERIPHONE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_saved_provider_round_trips(tmp_path, monkeypatch):
    _isolate_provider_env(monkeypatch)

    assert ev.load_scraper_provider("u1", tmp_path) == "decodo"  # untouched default
    ev.save_scraper_provider("u1", tmp_path, "scrapingbee")
    assert ev.load_scraper_provider("u1", tmp_path) == "scrapingbee"
    ev.save_scraper_provider("u1", tmp_path, "local")
    assert ev.load_scraper_provider("u1", tmp_path) == "local"


def test_unknown_provider_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ev.save_scraper_provider("u1", tmp_path, "zerobounce")


def test_saved_provider_survives_a_corrupt_file(tmp_path, monkeypatch):
    _isolate_provider_env(monkeypatch)
    cfg = tmp_path / "fv-s-u1" / "validator-config"
    cfg.mkdir(parents=True)
    (cfg / "scraper.json").write_text("{not json")

    assert ev.load_scraper_provider("u1", tmp_path) == "decodo"


def test_active_scraper_provider_reads_the_saved_choice(tmp_path, monkeypatch):
    """The panel's choice must reach scrape_site without an env var."""
    _isolate_provider_env(monkeypatch)
    monkeypatch.setattr(ev, "load_scraper_provider", lambda *a, **k: "scrapingbee")

    assert ev.active_scraper_provider({}) == "scrapingbee"
    # An explicit per-call provider still wins over the saved one.
    assert ev.active_scraper_provider({"provider": "decodo"}) == "decodo"


def test_phone_transport_label_follows_the_saved_choice(tmp_path, monkeypatch):
    _isolate_provider_env(monkeypatch)
    import functions

    monkeypatch.setattr(functions, "load_scraper_provider", lambda *a, **k: "scrapingbee")
    monkeypatch.setattr(functions, "is_scrapingbee_configured", lambda *a, **k: True)

    assert functions.phone_transport_label({}, "u1", tmp_path) == "scrapingbee"


def test_saved_choice_without_a_key_falls_through(tmp_path, monkeypatch):
    """Picking a provider whose key is missing must not claim to use it."""
    _isolate_provider_env(monkeypatch)
    import functions

    monkeypatch.setattr(functions, "load_scraper_provider", lambda *a, **k: "scrapingbee")
    monkeypatch.setattr(functions, "is_scrapingbee_configured", lambda *a, **k: False)
    monkeypatch.setattr(functions, "is_offline_engine_available", lambda: True)

    assert functions.phone_transport_label({}, "u1", tmp_path) == "local"


def test_scrapingbee_transport_test_reports_a_real_result(monkeypatch):
    class R:
        status_code = 200
        text = ("Number +1 415 555 2671\nType mobile\nCarrier T-Mobile\n"
                "Country United States (US)\nValid Valid")
        headers = {"Spb-cost": "5"}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())
    probe = ev.test_scrapingbee_transport({"api_key": "SB"})
    assert probe["success"] is True
    assert probe["carrier"] == "T-Mobile"
    assert probe["site"] == "scrapingbee"


def test_scrapingbee_transport_test_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ev.requests, "get", boom)
    probe = ev.test_scrapingbee_transport({"api_key": "SB"})
    assert probe["success"] is False
    assert probe["error"]

    assert ev.test_scrapingbee_transport({})["success"] is False


def test_scraper_endpoints_switch_and_persist(tmp_path, monkeypatch):
    """Endpoints: GET reports readiness, POST switches, key never echoed."""
    import asyncio
    import api

    monkeypatch.setattr(api, "USERS_BASE_PATH", tmp_path)
    user = {"user_id": "scraperuser"}

    before = asyncio.run(api.get_scraper_provider(user))
    assert before["provider"] == "decodo"
    assert {"scrapingbee", "decodo", "browser-use", "local"} == {p["id"] for p in before["providers"]}

    after = asyncio.run(api.set_scraper_provider({"provider": "scrapingbee"}, user))
    assert after["success"] is True
    assert after["provider"] == "scrapingbee"

    reread = asyncio.run(api.get_scraper_provider(user))
    assert reread["provider"] == "scrapingbee"

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.set_scraper_provider({"provider": "nope"}, user))
    assert excinfo.value.status_code == 400


def test_saved_key_counts_without_an_env_var(tmp_path, monkeypatch):
    """
    Regression: a key pasted into the panel reported transport "local".

    The readiness checks consulted the environment only, so a user-scoped key
    looked absent and the batch fell through to the free engine.
    """
    _isolate_provider_env(monkeypatch)
    import functions

    ev.save_scrapingbee_settings("paneluser", tmp_path, {"api_key": "SB-PANEL"})
    ev.save_scraper_provider("paneluser", tmp_path, "scrapingbee")

    assert functions.is_scrapingbee_configured("paneluser", tmp_path) is True
    assert functions.phone_transport_label({}, "paneluser", tmp_path) == "scrapingbee"


def test_local_choice_stays_offline(tmp_path, monkeypatch):
    """Picking the free engine must not reach for a paid transport."""
    _isolate_provider_env(monkeypatch)
    monkeypatch.setattr(ev, "load_scraper_provider", lambda *a, **k: "local")

    def boom(*a, **k):
        raise AssertionError("paid transport used despite an explicit local choice")

    monkeypatch.setattr(ev, "validate_phone_via_scrapingbee", boom)
    monkeypatch.setattr(ev, "validate_phone_via_browser_use", boom)
    monkeypatch.setattr(ev, "validate_phone_via_decodo", boom)

    result = ev.validate_phone("+2347069977249", "NG", settings={"api_key": ""})
    assert result["site"] == "local"


def test_batch_uses_the_panel_saved_key(tmp_path, monkeypatch):
    """
    Regression: after saving a key in the panel the batch failed every number.

    The scrapingbee branch called load_scrapingbee_settings() with no user
    scope, so a user-saved key was invisible and each lookup errored.
    """
    _isolate_provider_env(monkeypatch)
    ev.save_scrapingbee_settings("batchuser", tmp_path, {"api_key": "SB-BATCH"})
    used = {}
    real = ev.validate_phone_via_scrapingbee

    def spy(number, country_code="", settings=None):
        used["key"] = (settings or {}).get("api_key")
        return real(number, country_code, {"api_key": "SB-BATCH"})

    monkeypatch.setattr(ev, "validate_phone_via_scrapingbee", spy)

    class R:
        status_code = 200
        text = ("Number +234 706 997 7249\nType mobile\nCarrier MTN\n"
                "Country Nigeria (NG)\nValid Valid")
        headers = {"Spb-cost": "5"}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())

    import asyncio
    results = asyncio.run(ev.validate_phones_async(
        ["07069977249"],
        "NG",
        {"transport": "scrapingbee"},
        {"api_key": ""},
        {"api_key": ""},
        ev.load_scrapingbee_settings("batchuser", tmp_path),
    ))

    assert used["key"] == "SB-BATCH"
    assert results[0]["site"] == "scrapingbee"
    assert results[0]["carrier"] == "MTN"
    assert not results[0].get("error")


def test_scraper_report_distinguishes_saved_from_env_keys(tmp_path, monkeypatch):
    """The panel shows 'Saved' vs 'Env' per provider, so it must be told which."""
    import asyncio
    import api

    _isolate_provider_env(monkeypatch)
    monkeypatch.setattr(api, "USERS_BASE_PATH", tmp_path)
    user = {"user_id": "srcuser"}

    fresh = asyncio.run(api.get_scraper_provider(user))
    assert fresh["sources"]["scrapingbee"] is None

    ev.save_scrapingbee_settings("srcuser", tmp_path, {"api_key": "SB-SAVED"})
    after = asyncio.run(api.get_scraper_provider(user))
    assert after["sources"]["scrapingbee"] == "user"
    assert after["configured"]["scrapingbee"] is True

    monkeypatch.setenv("FV_S_DECODO_API_KEY", "FROM-ENV")
    env_report = asyncio.run(api.get_scraper_provider(user))
    assert env_report["sources"]["decodo"] == "environment"
    assert env_report["configured"]["decodo"] is True


def test_switch_endpoint_reports_the_new_transport(tmp_path, monkeypatch):
    """
    Regression: POST /validator/scraper said "transport": "local" after a
    successful switch, because it built the label without the user scope.
    """
    import asyncio
    import api

    _isolate_provider_env(monkeypatch)
    monkeypatch.setattr(api, "USERS_BASE_PATH", tmp_path)
    user = {"user_id": "switchuser"}

    ev.save_scrapingbee_settings("switchuser", tmp_path, {"api_key": "SB-SWITCH"})
    result = asyncio.run(api.set_scraper_provider({"provider": "scrapingbee"}, user))

    assert result["provider"] == "scrapingbee"
    assert result["transport"] == "scrapingbee"


# =============================================================================
# The veriphone result grid
#
# The card in plan.txt promises: Number / Type / Carrier / Country. The parser
# used to regex the whole page, so a rendered page that had not run a lookup
# still "found" a carrier -- in the <title> and the marketing copy -- and the
# HUD showed "Carrier: Lookup — Veriphone</title>".
# =============================================================================

PHONE_PAGE_WITH_GRID = """<!DOCTYPE html><html lang="en"><head>
<title>Phone Validator &amp; Carrier Lookup — Veriphone</title>
<style>.x{color:red}</style>
<script>var t = "Number Type Carrier Country";</script>
</head><body>
<h1>Phone Validator &amp; Carrier Lookup — Veriphone</h1>
<p>Validate phone numbers in milliseconds — one number or millions, by API or
CSV. 1,000 free lookups a month.</p>
<div style="display: grid; grid-template-columns: auto 1fr;">
  <span style="color: var(--muted-foreground);">Number</span>
  <span style="font-family: 'SF Mono';">+234 706 997 7249</span>
  <span style="color: var(--muted-foreground);">Type</span>
  <span style="text-transform: capitalize;">mobile</span>
  <span style="color: var(--muted-foreground);">Carrier</span>
  <span>MTN</span>
  <span style="color: var(--muted-foreground);">Country</span>
  <span>Nigeria (NG)</span>
</div>
<div class="faq"><h2>Is this a valid phone number?</h2><p>We answer in milliseconds.</p></div>
</body></html>"""

PHONE_PAGE_WITHOUT_GRID = """<html><head><title>Phone Validator — Veriphone</title></head>
<body><h1>Phone Validator</h1><p>Carrier and country in one call.</p></body></html>"""


def test_veriphone_parser_reads_the_result_grid():
    raw = ev._ADAPTERS["veriphone"].parse(PHONE_PAGE_WITH_GRID)

    assert raw["number"] == "+234 706 997 7249"
    assert raw["line_type"] == "mobile"
    assert raw["carrier"] == "MTN"
    assert raw["country"] == "Nigeria (NG)"


def test_veriphone_parser_grades_the_grid_as_valid():
    raw = ev._ADAPTERS["veriphone"].parse(PHONE_PAGE_WITH_GRID)
    status, reason, confidence = ev.classify_phone(raw)

    assert status == ev.STATUS_VALID
    assert "MTN" in reason
    assert confidence > 0


def test_veriphone_parser_ignores_page_chrome():
    """The <title> and the marketing paragraph are not results."""
    raw = ev._ADAPTERS["veriphone"].parse(PHONE_PAGE_WITHOUT_GRID)

    assert all(value is None for value in raw.values())
    status, _, _ = ev.classify_phone(raw)
    assert status == ev.STATUS_UNKNOWN


def test_veriphone_parser_tolerates_a_missing_carrier():
    page = (
        "<body><div><span>Number</span><span>+44 7911 123456</span>"
        "<span>Type</span><span>mobile</span>"
        "<span>Country</span><span>United Kingdom (GB)</span></div></body>"
    )
    raw = ev._ADAPTERS["veriphone"].parse(page)

    assert raw["number"] == "+44 7911 123456"
    assert raw["carrier"] is None
    assert raw["country"] == "United Kingdom (GB)"


# =============================================================================
# Storing a number keeps its dial code
#
# "i put a number with country code it sends only the number" -- the importer
# stored the national digits only, so +2347069977249 was queued as "7069977249"
# and a trunk-zero "07069977249" as "069977249" (0 read as a dial code).
# =============================================================================

@pytest.mark.parametrize("raw, hint, expected", [
    ("+2347069977249", "", "+2347069977249"),
    ("+234 706 997 7249", "", "+2347069977249"),
    ("2347069977249", "", "+2347069977249"),
    ("07069977249", "NG", "+2347069977249"),
    ("07069977249", "", "07069977249"),      # no hint: never invent a country
    ("+14155552671", "", "+14155552671"),
    ("4155552671", "US", "+14155552671"),    # not "+41..." (Switzerland)
    ("07911123456", "GB", "+447911123456"),
    ("+44 7911 123456", "", "+447911123456"),
    ("0044 7911 123456", "", "+447911123456"),
])
def test_normalize_phone_number_keeps_the_dial_code(raw, hint, expected):
    country_code, e164 = functions.normalize_phone_number(raw, hint)

    assert e164 == expected
    # A country is either a real ISO-2 code or the explicit "UNKNOWN" marker;
    # it is never a guess dressed up as fact.
    assert country_code == "UNKNOWN" or len(country_code) == 2


def test_normalize_phone_number_reports_the_country():
    assert functions.normalize_phone_number("+2347069977249")[0] == "NG"
    assert functions.normalize_phone_number("+14155552671")[0] == "US"
    assert functions.normalize_phone_number("07069977249", "NG")[0] == "NG"


def test_imported_numbers_keep_their_country_code():
    """The bug the user hit: importing with a dial code stored no dial code."""
    categorized = functions.categorize_phone_numbers(
        ["+2347069977249", "+14155552671"]
    )
    stored = [entry["cleaned"] for entries in categorized.values() for entry in entries]

    assert "+2347069977249" in stored
    assert "+14155552671" in stored
    assert "7069977249" not in stored


def test_imported_trunk_zero_numbers_resolve_against_the_hint():
    categorized = functions.categorize_phone_numbers(["07069977249"], "NG")
    entry = list(categorized.values())[0][0]

    assert entry["e164"] == "+2347069977249"
    assert entry["country_code"] == "NG"


def test_stored_number_survives_the_lookup_normaliser():
    """Whatever the importer stores, to_e164() must send the same number."""
    for raw, hint in [("+2347069977249", ""), ("07069977249", "NG"),
                      ("+14155552671", ""), ("07911123456", "GB")]:
        country_code, stored = functions.normalize_phone_number(raw, hint)
        assert ev.to_e164(stored, country_code) == stored


# =============================================================================
# Stored results are scrubbed on the way out
#
# Sessions saved before the parser fix still contain the page's <title> in the
# number, carrier and country fields. The HUD read them back verbatim, so the
# fix has to apply to old rows too -- not just to new parses.
# =============================================================================

SCRAPED_JUNK_RESULT = {
    "number": "Validator &amp; Carrier Lookup — Veriphone</title>",
    "formatted_number": "Validator &amp; Carrier Lookup — Veriphone</title>",
    "country": "in milliseconds — one number or millions, by API or CSV.",
    "carrier": "Lookup — Veriphone</title>",
    "line_type": 'Html><html lang="en"><head>',
    "country_code": "US",
    "status": "valid",
    "valid": True,
    "confidence": 90,
}


def test_scrub_removes_page_chrome_from_an_old_result():
    cleaned = ev.scrub_phone_result(SCRAPED_JUNK_RESULT)

    assert cleaned["formatted_number"] is None
    assert cleaned["carrier"] is None
    assert cleaned["country"] is None
    assert cleaned["line_type"] is None


def test_scrub_downgrades_a_verdict_with_no_readable_number():
    cleaned = ev.scrub_phone_result(SCRAPED_JUNK_RESULT)

    assert cleaned["status"] == ev.STATUS_UNKNOWN
    assert cleaned["valid"] is False
    assert "readable number" in cleaned["reason"]


def test_scrub_keeps_a_genuine_result_intact():
    genuine = {
        "number": "07069977249",
        "e164": "+2347069977249",
        "formatted_number": "+234 706 997 7249",
        "country": "Nigeria (NG)",
        "country_code": "NG",
        "carrier": "MTN",
        "line_type": "mobile",
        "status": "valid",
        "valid": True,
        "confidence": 90,
    }
    cleaned = ev.scrub_phone_result(genuine)

    assert cleaned["formatted_number"] == "+234 706 997 7249"
    assert cleaned["country"] == "Nigeria (NG)"
    assert cleaned["carrier"] == "MTN"
    assert cleaned["line_type"] == "mobile"
    assert cleaned["status"] == "valid"

def test_scrapingbee_render_without_the_grid_is_a_failure(monkeypatch):
    """A page that never ran the lookup is not an answer.

    This is the bug behind "Carrier: Lookup — Veriphone</title>": the render
    returned the marketing page, the parser read its <title>, and the row was
    saved as if it were a lookup.
    """
    class R:
        status_code = 200
        text = "<html><head><title>Phone Validator — Veriphone</title></head><body>Validate numbers</body></html>"
        headers = {}

    monkeypatch.setattr(ev.requests, "get", lambda *a, **k: R())
    with pytest.raises(ev.DecodoError) as exc:
        ev._call_scrapingbee(ev._ADAPTERS["veriphone"], "+2347069977249", {"api_key": "SB"})

    assert "result grid" in str(exc.value)


def test_js_scenario_polls_for_the_result_grid():
    """
    Readiness is a page condition, not an element id we never saw.

    The earlier version emitted wait_for on "#phone-result", an invented
    selector. It could never match, so every lookup sat there until the
    transport timed out -- which is exactly the 60s timeout seen in the log.
    """
    scenario = ev.build_js_scenario(ev._ADAPTERS["veriphone"], "+2347069977249")
    steps = scenario["instructions"]
    kinds = [list(step)[0] for step in steps]

    assert "wait_for" not in kinds          # no guessed selector
    assert "evaluate" in kinds              # a real condition instead
    submit = kinds.index("click", kinds.index("fill"))
    assert kinds.index("fill") < submit < kinds.index("evaluate")

    # The condition must look for the grid, not for page chrome.
    condition = next(s["evaluate"] for s in steps if "evaluate" in s)
    assert "Number" in condition
    assert "innerText" in condition


# =============================================================================
# Browser Use transport
#
# The ScrapingBee render timed out after 60s and the log ended there, so this
# transport has to (a) exist as a route the panel can call, (b) not wait on a
# selector nobody has ever seen, and (c) refuse to invent a verdict when the
# grid never appears.
# =============================================================================

def test_browser_use_test_route_exists():
    """The panel posts here; before this it 404'd on every Test click."""
    import api

    paths = {(tuple(sorted(r.methods)), r.path) for r in api.validator_router.routes}
    assert (("POST",), "/validator/browser-use/test") in paths
    assert (("GET",), "/validator/browser-use/config") in paths


def test_browser_use_config_route_is_not_shadowed():
    """Two handlers once shared /decodo/test; the settings reader won by accident."""
    import api

    decodo = [r for r in api.validator_router.routes if r.path == "/validator/decodo/test"]
    assert len(decodo) == 1
    assert decodo[0].endpoint.__name__ == "test_decodo_config"


def test_browser_use_without_a_key_fails_cleanly():
    result = ev.validate_phone_via_browser_use("+2349028938460", "NG", {"api_key": ""})

    assert result["error"]
    assert result["status"] == ev.STATUS_UNKNOWN
    assert result["valid"] is False


def test_veriphone_readiness_checks_the_page_not_a_selector():
    adapter = ev._ADAPTERS["veriphone"]

    assert not hasattr(adapter, "result_selector"), "guessed selectors are gone"
    assert "innerText" in adapter.ready_js
    assert "Number" in adapter.ready_js


def test_result_ready_times_out_instead_of_raising():
    """A page that never renders the grid must return False, not hang or raise."""
    class Dead:
        def evaluate(self, script):
            return False

    adapter = ev._ADAPTERS["veriphone"]
    assert adapter.result_ready(Dead(), timeout_ms=300) is False


def test_result_ready_succeeds_when_the_grid_appears():
    class Live:
        def evaluate(self, script):
            return True

    adapter = ev._ADAPTERS["veriphone"]
    assert adapter.result_ready(Live(), timeout_ms=1000) is True


def test_request_timeout_is_bounded():
    """ScrapingBee's own ceiling is 40s; waiting longer only waits on nothing."""
    assert ev.REQUEST_TIMEOUT <= 60
    assert ev.REQUEST_TIMEOUT >= 10


# =============================================================================
# The full veriphone JSON API answer
#
# The API returns far more than a verdict -- the number in three formats, the
# dialling prefix, the region, the timezone -- and all of it was being dropped,
# so a paid lookup showed no more than a scraped one.
# =============================================================================

VERIPHONE_US_VOIP = {
    "status": "success", "phone": "+12012014514", "phone_valid": True,
    "phone_type": "voip", "phone_region": "New Jersey",
    "country": "United States", "country_code": "US", "country_prefix": "1",
    "international_number": "+1 201-201-4514", "local_number": "(201) 201-4514",
    "e164": "+12012014514", "carrier": "Bandwidth.com", "mode": "static",
    "timezone": ["America/New_York"], "geographical": True,
}


def test_api_result_keeps_every_field_the_api_returned():
    result = ev._result_from_veriphone_api(
        VERIPHONE_US_VOIP, "2012014514", "+12012014514"
    )

    assert result["status"] == ev.STATUS_VALID
    assert result["formatted_number"] == "+1 201-201-4514"
    assert result["international_number"] == "+1 201-201-4514"
    assert result["local_number"] == "(201) 201-4514"
    assert result["dial_code"] == "+1"
    assert result["country_prefix"] == "1"
    assert result["country"] == "United States"
    assert result["country_code"] == "US"
    assert result["region"] == "New Jersey"
    assert result["carrier"] == "Bandwidth.com"
    assert result["line_type"] == "voip"
    assert result["timezone"] == ["America/New_York"]
    assert result["geographical"] is True


def test_api_fields_survive_the_scrub():
    result = ev._result_from_veriphone_api(
        VERIPHONE_US_VOIP, "2012014514", "+12012014514"
    )
    cleaned = ev.scrub_phone_result(result)

    assert cleaned["local_number"] == "(201) 201-4514"
    assert cleaned["timezone"] == ["America/New_York"]
    assert cleaned["dial_code"] == "+1"
    assert cleaned["region"] == "New Jersey"
    assert cleaned["status"] == ev.STATUS_VALID


def test_saved_list_keeps_the_api_fields(tmp_path):
    session = {
        "list_name": "api-fields",
        "country_code": "US",
        "site": "veriphone-api",
        "results": [ev._result_from_veriphone_api(
            VERIPHONE_US_VOIP, "2012014514", "+12012014514"
        )],
    }
    list_id = functions.save_phone_results_to_list("fieldsuser", tmp_path, session)
    stored = json.loads(
        (tmp_path / "fv-s-fieldsuser" / "phone-lists" / f"{list_id}.json").read_text()
    )
    entry = stored["numbers"][0]

    assert entry["local_number"] == "(201) 201-4514"
    assert entry["timezone"] == ["America/New_York"]
    assert entry["dial_code"] == "+1"
    assert entry["region"] == "New Jersey"


def test_save_veriphone_config_route_does_not_crash():
    """This raised NameError: phone_transport_label was never imported."""
    import api
    import inspect

    source = inspect.getsource(api.save_veriphone_config)
    assert "from functions import phone_transport_label" in source
