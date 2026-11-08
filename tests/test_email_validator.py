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
    assert ev._judge(text) is expected


def test_judge_invalid_beats_valid_substring():
    # "invalid" contains "valid" -- must stay negative.
    assert ev._judge("Email is invalid") is False


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
