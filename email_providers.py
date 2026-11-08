"""
Email provider senders for FV-S.

Synchronous send functions for every supported sending channel:
  - SMTP (any SMTP server)
  - Gmail API (OAuth2 refresh-token flow)
  - Microsoft Graph API (client-credentials app-only flow)
  - SendGrid, Mailgun, Mailjet, Postmark, SMTP2GO (HTTP APIs)
  - Amazon SES (SigV4-signed Query API)

Every ``send_*`` function returns ``(success: bool, message: str)`` on success
and RAISES a typed exception on failure so callers can decide what to do:

  - :class:`SendAuthError`        -> bad credentials (remove the credential)
  - :class:`SendConnectionError`  -> network/connection failure (remove the credential)
  - :class:`SendError`            -> anything else (recipient refused, rate
                                     limit, content error...) — keep the credential.

The shared entry point :func:`send_email_via_provider` returns
``(success, message, kind)`` where ``kind`` is one of
``"none" | "auth" | "connection" | "other"``.

All HTTP calls use the synchronous ``requests`` library (already a project
dependency) because the campaign engine runs each send inside a worker thread,
so blocking I/O is fine and keeps things simple.
"""

import base64
import hashlib
import hmac
import re
import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from urllib.parse import urlencode

import requests

HTTP_TIMEOUT = 30


# =============================================================================
# TYPED ERRORS
# =============================================================================

class SendAuthError(Exception):
    """Authentication / authorization failure (bad credential)."""


class SendConnectionError(Exception):
    """Network / connection failure (server unreachable, timeout, TLS)."""


class SendError(Exception):
    """Any other send failure (recipient refused, rate limit, content...)."""


# =============================================================================
# HELPERS
# =============================================================================

def _addr(name, email):
    """Format 'Name <email>' with RFC2047 encoding for the display name."""
    email = (email or "").strip()
    name = (name or "").strip()
    if name:
        return f"{str(Header(name, 'utf-8'))} <{email}>"
    return email


def _strip_html(html):
    """Very small HTML -> text converter for plain-text parts."""
    if not html:
        return ""
    s = str(html)
    s = re.sub(r"<(style|script|head)[^>]*>[\s\S]*?</\1>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</div>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<") \
         .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _ensure_text(html, text):
    """Return a non-empty plain-text body, deriving it from HTML if needed."""
    text = (text or "").strip()
    if text:
        return text
    return _strip_html(html) or (html or "")


def build_mime_message(
    to_email,
    from_addr,
    from_name,
    subject,
    html_content,
    text_content,
    reply_to=None,
    cc=None,
    bcc=None,
):
    """Build a multipart/alternative MIME message as bytes."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject or "", "utf-8").encode()
    msg["From"] = _addr(from_name, from_addr)
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if reply_to:
        msg["Reply-To"] = reply_to
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

    text = _ensure_text(html_content, text_content)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    if html_content:
        msg.attach(MIMEText(html_content, "html", "utf-8"))

    return msg.as_bytes()


def _http_post(url, **kwargs):
    """requests.post wrapper that translates connection errors."""
    try:
        return requests.post(url, timeout=HTTP_TIMEOUT, **kwargs)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
            requests.exceptions.SSLError) as e:
        raise SendConnectionError(f"Connection error: {e}")
    except requests.exceptions.RequestException as e:
        raise SendError(f"HTTP error: {e}")


def _raise_http_error(provider, response):
    """Raise the right typed error for a non-2xx HTTP response."""
    if response.status_code in (401, 403):
        raise SendAuthError(f"{provider} authentication failed (HTTP {response.status_code})")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        hint = f" (Retry-After: {retry_after})" if retry_after else ""
        raise SendError(f"{provider} rate limited (HTTP 429){hint}")
    raise SendError(f"{provider} error: HTTP {response.status_code} {response.text[:200]}")


# =============================================================================
# SMTP
# =============================================================================

def _smtp_connect(config, timeout=30):
    host = (config.get("host") or "").strip()
    port = int(config.get("port") or 587)
    if not host:
        raise SendError("SMTP host not configured")
    if port == 465:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return smtplib.SMTP_SSL(host, port, context=context, timeout=timeout)
    server = smtplib.SMTP(host, port, timeout=timeout)
    server.ehlo()
    try:
        server.starttls()
        server.ehlo()
    except smtplib.SMTPException:
        pass
    return server


def check_smtp_valid(config, timeout=20):
    """
    Lightweight preflight: connect + EHLO (+STARTTLS) + LOGIN, then quit.
    Does NOT send any email. Returns ``(bool, message)``.
    """
    username = config.get("user") or config.get("username") or ""
    password = config.get("password") or ""
    try:
        server = _smtp_connect(config, timeout=timeout)
        if username:
            server.login(username, password)
        server.quit()
        return True, "SMTP valid"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP authentication failed: {e}"
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            ConnectionError, socket.error, TimeoutError, socket.timeout, ssl.SSLError) as e:
        return False, f"SMTP connection error: {e}"
    except Exception as e:
        return False, f"SMTP error: {e}"


def send_smtp(config, to_email, from_name, subject, html_content, text_content,
              reply_to=None, cc=None, bcc=None):
    host = (config.get("host") or "").strip()
    port = int(config.get("port") or 587)
    username = config.get("user") or config.get("username") or ""
    password = config.get("password") or ""
    from_addr = config.get("from_address") or config.get("sender_email") or username

    if not host:
        raise SendError("SMTP host not configured")
    if not from_addr:
        raise SendError("SMTP sender address not configured")

    raw = build_mime_message(to_email, from_addr, from_name, subject,
                             html_content, text_content, reply_to, cc, bcc)
    all_recipients = [to_email] + list(cc or []) + list(bcc or [])

    try:
        server = _smtp_connect(config)
        if username:
            server.login(username, password)
        server.sendmail(from_addr, all_recipients, raw)
        server.quit()
        return True, "Email sent via SMTP"
    except smtplib.SMTPAuthenticationError as e:
        raise SendAuthError(f"SMTP authentication failed: {e}")
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"SMTP recipient refused: {e}")
    except smtplib.SMTPDataError as e:
        raise SendError(f"SMTP data error: {e}")
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            ConnectionError, socket.error, TimeoutError, socket.timeout, ssl.SSLError) as e:
        raise SendConnectionError(f"SMTP connection error: {e}")
    except Exception as e:
        raise SendError(f"SMTP error: {e}")


# =============================================================================
# GOOGLE GMAIL API (OAuth2 refresh-token flow)
# =============================================================================

def _gmail_access_token(config):
    r = _http_post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
            "refresh_token": config.get("refresh_token", ""),
        },
    )
    if r.status_code != 200:
        _raise_http_error("Gmail OAuth", r)
    return r.json()["access_token"]


def send_gmail_api(config, to_email, from_name, subject, html_content, text_content,
                   reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or ""
    if not from_addr:
        raise SendError("Gmail sender email not configured")

    raw = build_mime_message(to_email, from_addr, from_name, subject,
                             html_content, text_content, reply_to, cc, bcc)
    token = _gmail_access_token(config)
    body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
    r = _http_post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    if r.status_code == 200:
        return True, "Email sent via Gmail API"
    _raise_http_error("Gmail API", r)


# =============================================================================
# MICROSOFT GRAPH API (client-credentials app-only flow)
# =============================================================================

def _graph_access_token(config):
    r = _http_post(
        f"https://login.microsoftonline.com/{config.get('tenant_id', '')}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.get("client_id", ""),
            "client_secret": config.get("client_secret", ""),
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    if r.status_code != 200:
        _raise_http_error("Graph token", r)
    return r.json()["access_token"]


def send_graph_api(config, to_email, from_name, subject, html_content, text_content,
                   reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or ""
    if not from_addr:
        raise SendError("Outlook sender email not configured")

    token = _graph_access_token(config)
    if html_content:
        body = {"contentType": "HTML", "content": html_content}
    else:
        body = {"contentType": "Text", "content": _ensure_text(html_content, text_content)}

    message = {
        "subject": subject,
        "body": body,
        "toRecipients": [{"emailAddress": {"address": to_email}}],
        "from": {"emailAddress": {"address": from_addr, "name": from_name or ""}},
    }
    if reply_to:
        message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    if bcc:
        message["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc]

    r = _http_post(
        f"https://graph.microsoft.com/v1.0/users/{from_addr}/sendMail",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": message, "saveToSentItems": True},
    )
    if r.status_code in (200, 202):
        return True, "Email sent via Microsoft Graph API"
    _raise_http_error("Graph API", r)


# =============================================================================
# SENDGRID
# =============================================================================

def send_sendgrid(config, to_email, from_name, subject, html_content, text_content,
                  reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not from_addr:
        raise SendError("SendGrid sender email not configured")

    personalization = {"to": [{"email": to_email}]}
    if cc:
        personalization["cc"] = [{"email": a} for a in cc]
    if bcc:
        personalization["bcc"] = [{"email": a} for a in bcc]

    content = []
    if text_content:
        content.append({"type": "text/plain", "value": text_content})
    if html_content:
        content.append({"type": "text/html", "value": html_content})
    if not content:
        content.append({"type": "text/plain", "value": ""})

    payload = {
        "personalizations": [personalization],
        "from": {"email": from_addr, "name": from_name or ""},
        "subject": subject,
        "content": content,
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}

    r = _http_post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {config.get('api_key', '')}"},
        json=payload,
    )
    if r.status_code in (200, 202):
        return True, "Email sent via SendGrid"
    _raise_http_error("SendGrid", r)


# =============================================================================
# MAILGUN
# =============================================================================

def send_mailgun(config, to_email, from_name, subject, html_content, text_content,
                 reply_to=None, cc=None, bcc=None):
    domain = (config.get("domain") or "").strip()
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not domain:
        raise SendError("Mailgun domain not configured")
    if not from_addr:
        raise SendError("Mailgun sender email not configured")

    data = {
        "from": _addr(from_name, from_addr),
        "to": to_email,
        "subject": subject,
    }
    if text_content:
        data["text"] = text_content
    if html_content:
        data["html"] = html_content
    if cc:
        data["cc"] = ",".join(cc)
    if bcc:
        data["bcc"] = ",".join(bcc)
    if reply_to:
        data["h:Reply-To"] = reply_to

    r = _http_post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", config.get("api_key", "")),
        data=data,
    )
    if r.status_code == 200:
        return True, "Email sent via Mailgun"
    _raise_http_error("Mailgun", r)


# =============================================================================
# MAILJET
# =============================================================================

def send_mailjet(config, to_email, from_name, subject, html_content, text_content,
                 reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not from_addr:
        raise SendError("Mailjet sender email not configured")

    message = {
        "From": {"Email": from_addr, "Name": from_name or ""},
        "To": [{"Email": to_email}],
        "Subject": subject,
    }
    if text_content:
        message["TextPart"] = text_content
    if html_content:
        message["HTMLPart"] = html_content
    if cc:
        message["Cc"] = [{"Email": a} for a in cc]
    if bcc:
        message["Bcc"] = [{"Email": a} for a in bcc]
    if reply_to:
        message["ReplyTo"] = {"Email": reply_to}

    r = _http_post(
        "https://api.mailjet.com/v3.1/send",
        auth=(config.get("api_key", ""), config.get("api_secret", "")),
        json={"Messages": [message]},
    )
    if r.status_code == 200:
        return True, "Email sent via Mailjet"
    _raise_http_error("Mailjet", r)


# =============================================================================
# POSTMARK
# =============================================================================

def send_postmark(config, to_email, from_name, subject, html_content, text_content,
                  reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not from_addr:
        raise SendError("Postmark sender email not configured")

    payload = {
        "From": _addr(from_name, from_addr),
        "To": to_email,
        "Subject": subject,
    }
    if text_content:
        payload["TextBody"] = text_content
    if html_content:
        payload["HtmlBody"] = html_content
    if cc:
        payload["Cc"] = ",".join(cc)
    if bcc:
        payload["Bcc"] = ",".join(bcc)
    if reply_to:
        payload["ReplyTo"] = reply_to

    r = _http_post(
        "https://api.postmarkapp.com/email",
        headers={
            "X-Postmark-Server-Token": config.get("api_key", ""),
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if r.status_code == 200:
        return True, "Email sent via Postmark"
    _raise_http_error("Postmark", r)


# =============================================================================
# SMTP2GO
# =============================================================================

def send_smtp2go(config, to_email, from_name, subject, html_content, text_content,
                 reply_to=None, cc=None, bcc=None):
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not from_addr:
        raise SendError("SMTP2GO sender email not configured")

    recipients = [to_email] + list(cc or []) + list(bcc or [])
    payload = {
        "api_key": config.get("api_key", ""),
        "sender": _addr(from_name, from_addr),
        "to": recipients,
        "subject": subject,
    }
    if text_content:
        payload["text_body"] = text_content
    if html_content:
        payload["html_body"] = html_content
    if reply_to:
        payload["custom_headers"] = [{"header": "Reply-To", "value": reply_to}]

    r = _http_post("https://api.smtp2go.com/v3/email/send", json=payload)
    if r.status_code == 200:
        data = r.json()
        result = (data.get("data") or {})
        if result.get("succeeded"):
            return True, "Email sent via SMTP2GO"
        err = result.get("error") or data.get("error") or data
        raise SendError(f"SMTP2GO error: {str(err)[:200]}")
    _raise_http_error("SMTP2GO", r)


# =============================================================================
# AMAZON SES (SigV4)
# =============================================================================

def _hmac_sig(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _aws_signing_key(secret, datestamp, region, service):
    k_date = _hmac_sig(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac_sig(k_date, region)
    k_service = _hmac_sig(k_region, service)
    return _hmac_sig(k_service, "aws4_request")


def send_ses(config, to_email, from_name, subject, html_content, text_content,
             reply_to=None, cc=None, bcc=None):
    access = config.get("api_key") or ""
    secret = config.get("api_secret") or ""
    region = config.get("region") or "us-east-1"
    from_addr = config.get("sender_email") or config.get("from_address") or ""
    if not access or not secret:
        raise SendError("SES access key / secret key not configured")
    if not from_addr:
        raise SendError("SES sender email not configured")

    host = f"email.{region}.amazonaws.com"
    endpoint = f"https://{host}/"

    params = {
        "Action": "SendEmail",
        "Version": "2010-12-01",
        "Source": _addr(from_name, from_addr),
        "Destination.ToAddresses.member.1": to_email,
        "Message.Subject.Data": subject,
        "Message.Subject.Charset": "UTF-8",
    }
    if html_content:
        params["Message.Body.Html.Data"] = html_content
        params["Message.Body.Html.Charset"] = "UTF-8"
    if text_content:
        params["Message.Body.Text.Data"] = text_content
        params["Message.Body.Text.Charset"] = "UTF-8"
    for i, a in enumerate(cc or [], 1):
        params[f"Destination.CcAddresses.member.{i}"] = a
    for i, a in enumerate(bcc or [], 1):
        params[f"Destination.BccAddresses.member.{i}"] = a
    if reply_to:
        params["ReplyToAddresses.member.1"] = reply_to

    body = urlencode(params)
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    canonical_headers = (
        "content-type:application/x-www-form-urlencoded\n"
        f"host:{host}\n"
        f"x-amz-date:{amzdate}\n"
    )
    signed_headers = "content-type;host;x-amz-date"
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/ses/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amzdate}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _aws_signing_key(secret, datestamp, region, "ses")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"{algorithm} Credential={access}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Amz-Date": amzdate,
        "Authorization": authorization,
    }
    r = _http_post(endpoint, data=body.encode("utf-8"), headers=headers)
    if r.status_code == 200:
        if "<ErrorResponse" in r.text or "<Error>" in r.text:
            raise SendError(f"SES error: {r.text[:300]}")
        return True, "Email sent via Amazon SES"
    _raise_http_error("SES", r)


# =============================================================================
# DISPATCHER
# =============================================================================

PROVIDER_ALIASES = {
    "smtp": "smtp",
    "google": "google",
    "gmail": "google",
    "gmail_api": "google",
    "outlook": "outlook",
    "microsoft": "outlook",
    "graph": "outlook",
    "graph_api": "outlook",
    "sendgrid": "sendgrid",
    "mailgun": "mailgun",
    "mailjet": "mailjet",
    "postmark": "postmark",
    "smtp2go": "smtp2go",
    "aws": "aws_ses",
    "aws_ses": "aws_ses",
    "ses": "aws_ses",
}

_SENDERS = {
    "smtp": send_smtp,
    "google": send_gmail_api,
    "outlook": send_graph_api,
    "sendgrid": send_sendgrid,
    "mailgun": send_mailgun,
    "mailjet": send_mailjet,
    "postmark": send_postmark,
    "smtp2go": send_smtp2go,
    "aws_ses": send_ses,
}


def send_email_via_provider(provider_type, config, to_email, from_name, subject,
                            html_content, text_content, reply_to=None, cc=None, bcc=None):
    """
    Send one email through the given provider.

    Returns ``(success, message, kind)`` where ``kind`` is:
      "none"       - sent successfully
      "auth"       - authentication failure (credential should be removed)
      "connection" - connection failure (credential should be removed)
      "other"      - any other failure (retryable; keep credential)
    """
    provider = PROVIDER_ALIASES.get(str(provider_type or "").strip().lower(),
                                    str(provider_type or "").strip().lower())
    sender = _SENDERS.get(provider)
    if sender is None:
        return False, f"Unsupported sending provider: {provider_type}", "other"

    try:
        ok, message = sender(
            config or {},
            to_email,
            from_name,
            subject,
            html_content,
            text_content,
            reply_to=reply_to,
            cc=cc,
            bcc=bcc,
        )
        return ok, message, "none"
    except SendAuthError as e:
        return False, str(e), "auth"
    except SendConnectionError as e:
        return False, str(e), "connection"
    except SendError as e:
        return False, str(e), "other"
    except Exception as e:  # noqa: BLE001 - surface any failure to the caller
        return False, f"Send error ({provider}): {e}", "other"


def send_test_email_result(provider_type, config, test_email, from_name=None):
    """Send a small test email; returns ``(ok, message, kind)``.

    ``kind`` is ``"none" | "auth" | "connection" | "other"`` (see
    :func:`send_email_via_provider`).
    """
    subject = "FV-S Test Email"
    text = "This is a test email from FV-S. Your sending configuration is working correctly."
    html = (
        "<html><body style='font-family:sans-serif;'>"
        "<h3>FV-S Test Email</h3>"
        "<p>This is a test email from FV-S.</p>"
        "<p>Your sending configuration is working correctly.</p>"
        "</body></html>"
    )
    return send_email_via_provider(
        provider_type,
        config,
        test_email,
        from_name or "FV-S",
        subject,
        html,
        text,
    )


def send_test_email(provider_type, config, test_email, from_name=None):
    """Send a small test email (used by credential/mailbox test endpoints)."""
    ok, message, _kind = send_test_email_result(provider_type, config, test_email, from_name)
    return ok, message
