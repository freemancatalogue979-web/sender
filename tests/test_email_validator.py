"""Tests for the Decodo-only email validator (email_validator.py).

These exercise the parsing/verdict engine against representative rendered
HTML, with no network access: the Decodo call is monkeypatched.
"""
import pytest

import email_validator as ev


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
