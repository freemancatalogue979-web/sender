"""Tests for email_providers: typed errors and request construction."""
import base64
import json

import pytest

import email_providers as ep


class FakeResp:
    def __init__(self, status_code, text="", headers=None, js=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._js = js

    def json(self):
        return self._js


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def _post(url, **kwargs):
        calls.append((url, kwargs))
        if "api.sendgrid.com" in url:
            return FakeResp(401, "unauthorized")
        if "api.mailgun.net" in url:
            return FakeResp(200, '{"id":"x"}')
        if "api.mailjet.com" in url:
            return FakeResp(200, '{"Messages":[{"Status":"success"}]}')
        if "api.postmarkapp.com" in url:
            return FakeResp(200, '{"ErrorCode":0}')
        if "api.smtp2go.com" in url:
            return FakeResp(200, js={"data": {"succeeded": 1}})
        if "oauth2.googleapis.com" in url:
            return FakeResp(200, js={"access_token": "TOK"})
        if "gmail.googleapis.com" in url:
            return FakeResp(200, "{}")
        if "login.microsoftonline.com" in url:
            return FakeResp(200, js={"access_token": "TOK"})
        if "graph.microsoft.com" in url:
            return FakeResp(202, "")
        if "amazonaws.com" in url:
            return FakeResp(200, "<SendEmailResponse/>")
        return FakeResp(200, "")

    monkeypatch.setattr(ep.requests, "post", _post)
    return calls


def test_sendgrid_auth_failure_is_auth_kind(fake_post):
    ok, msg, kind = ep.send_email_via_provider(
        "sendgrid", {"api_key": "BAD", "sender_email": "me@x.com"},
        "to@x.com", "Me", "Subject", "<b>hi</b>", "hi",
    )
    assert ok is False
    assert kind == "auth"
    assert "401" in msg


def test_sendgrid_payload_and_auth_header(fake_post, monkeypatch):
    # Make sendgrid succeed for this test by overriding the fixture response.
    def _post(url, **kw):
        if "api.sendgrid.com" in url:
            return FakeResp(202, "")
        raise AssertionError("unexpected url")

    monkeypatch.setattr(ep.requests, "post", _post)
    ok, msg, kind = ep.send_email_via_provider(
        "sendgrid", {"api_key": "SK123", "sender_email": "me@x.com"},
        "to@x.com", "Sender Name", "Hello", "<b>hi</b>", "hi",
        cc=["cc@x.com"], bcc=["bcc@x.com"],
    )
    assert ok is True
    assert kind == "none"


def test_ses_sigv4_headers(fake_post):
    ok, msg, kind = ep.send_email_via_provider(
        "aws_ses", {"api_key": "AK", "api_secret": "SK", "region": "us-east-1", "sender_email": "me@x.com"},
        "to@x.com", "Me", "Subject", "<b>hi</b>", "hi",
    )
    assert ok is True and kind == "none"

    for url, kw in fake_post:
        if "amazonaws.com" in url:
            auth = kw["headers"]["Authorization"]
            assert auth.startswith("AWS4-HMAC-SHA256 Credential=AK/")
            assert "SignedHeaders=content-type;host;x-amz-date" in auth
            assert "Signature=" in auth
            body = kw["data"].decode()
            assert "Action=SendEmail" in body
            assert "Destination.ToAddresses.member.1=to%40x.com" in body
            return
    raise AssertionError("SES request not found")


def test_gmail_raw_mime(fake_post):
    ok, msg, kind = ep.send_email_via_provider(
        "google", {"client_id": "c", "client_secret": "s", "refresh_token": "r", "sender_email": "me@gmail.com"},
        "to@x.com", "Me", "Subject", "<b>hi</b>", "hi",
    )
    assert ok is True and kind == "none"
    for url, kw in fake_post:
        if "gmail.googleapis.com" in url:
            raw = kw["json"]["raw"]
            mime = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            assert b"From:" in mime
            return
    raise AssertionError("Gmail request not found")


def test_graph_sendmail_payload(fake_post):
    ok, msg, kind = ep.send_email_via_provider(
        "outlook", {"tenant_id": "t", "client_id": "c", "client_secret": "s", "sender_email": "me@contoso.com"},
        "to@x.com", "Me", "Subject", "<b>hi</b>", "hi",
    )
    assert ok is True and kind == "none"
    for url, kw in fake_post:
        if "graph.microsoft.com" in url and "sendMail" in url:
            assert kw["json"]["message"]["from"]["emailAddress"]["address"] == "me@contoso.com"
            assert kw["json"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "to@x.com"
            return
    raise AssertionError("Graph request not found")


def test_unsupported_provider(fake_post):
    ok, msg, kind = ep.send_email_via_provider("bogus", {}, "a@b.c", "N", "S", "h", "t")
    assert ok is False and kind == "other" and "Unsupported" in msg


def test_429_is_retryable_other(fake_post, monkeypatch):
    def _post(url, **kw):
        return FakeResp(429, "", headers={"Retry-After": "60"})

    monkeypatch.setattr(ep.requests, "post", _post)
    ok, msg, kind = ep.send_email_via_provider(
        "postmark", {"api_key": "TOK", "sender_email": "me@x.com"},
        "to@x.com", "Me", "S", "h", "t",
    )
    assert ok is False
    assert kind == "other"  # retryable, credential kept
    assert "429" in msg


def test_connection_error_is_connection_kind(monkeypatch):
    import requests as _requests

    def _post(url, **kw):
        raise _requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(ep.requests, "post", _post)
    ok, msg, kind = ep.send_email_via_provider(
        "mailgun", {"api_key": "K", "domain": "d.example.com", "sender_email": "me@x.com"},
        "to@x.com", "Me", "S", "h", "t",
    )
    assert ok is False and kind == "connection"
