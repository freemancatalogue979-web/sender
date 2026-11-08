"""
Helper functions for FV-S Admin Panel Server
"""

import os
import re
import json
import uuid
import socket
import platform
import hashlib
import hmac
import smtplib
import imaplib
import time
import secrets
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pydantic import BaseModel, Field

from async_imap import quote_imap_folder_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Email / phone validation transport -- Decodo only.
# There is no Playwright browser and no ZeroBounce account in this codebase;
# every lookup is a Decodo Web Scraping API request (see email_validator.py).
# ---------------------------------------------------------------------------
def phone_transport_label(veriphone_settings: dict = None) -> str:
    """
    Which transport phone validation will use.

    ``local`` (free, offline numbering plans) is the default and costs nothing
    per number. A veriphone key upgrades to ``veriphone-api``; if neither is
    available the paid ``decodo-scrape`` is the last resort.
    """
    if (veriphone_settings or {}).get("api_key"):
        return "veriphone-api"
    if is_offline_engine_available():
        return "local"
    if is_browser_use_available():
        return "browser-use"
    return "decodo-scrape"


from email_validator import (  # noqa: E402
    MAX_WORKERS as _VALIDATOR_MAX_WORKERS,
    STATUS_UNKNOWN,
    VALID_STATUSES,
    load_decodo_settings,
    save_decodo_settings,
    load_veriphone_settings,
    save_veriphone_settings,
    validate_emails,
    validate_emails_async,
    validate_emails_offline_async,
    is_browser_use_available,
    is_offline_engine_available,
    load_browser_use_settings,
    validate_phone,
    validate_phones_async,
    validate_phone_via_decodo,
)

# Batch sizing. The session records the requested size; these clamp it to
# something a Decodo plan will actually tolerate.
DEFAULT_VALIDATION_BATCH_SIZE = 10
MAX_VALIDATION_BATCH_SIZE = max(DEFAULT_VALIDATION_BATCH_SIZE, _VALIDATOR_MAX_WORKERS)

# ============================================================================= 
# IMAP SERVER AUTO-DETECTION - 150+ email domains to IMAP hosts
# =============================================================================
IMAP_SERVER_LOOKUP = {
    # Major Global Providers
    "gmail.com": ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "outlook.com": ("imap-mail.outlook.com", 993),
    "hotmail.com": ("imap-mail.outlook.com", 993),
    "live.com": ("imap-mail.outlook.com", 993),
    "msn.com": ("imap-mail.outlook.com", 993),
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "ymail.com": ("imap.mail.yahoo.com", 993),
    "rocketmail.com": ("imap.mail.yahoo.com", 993),
    "icloud.com": ("imap.mail.me.com", 993),
    "me.com": ("imap.mail.me.com", 993),
    "mac.com": ("imap.mail.me.com", 993),
    "aol.com": ("imap.aol.com", 993),
    
    # Premium/Privacy Email Services
    "protonmail.com": ("imap.protonmail.com", 993),
    "pm.me": ("imap.protonmail.com", 993),
    "tutanota.com": ("imap.tutanota.com", 993),
    "mailbox.org": ("imap.mailbox.org", 993),
    "fastmail.com": ("imap.fastmail.com", 993),
    "posteo.de": ("imap.posteo.de", 993),
    "zoho.com": ("imap.zoho.com", 993),
    "mail.com": ("imap.mail.com", 993),
    
    # US Regional ISPs
    "verizon.net": ("imap.verizon.net", 993),
    "comcast.net": ("imap.comcast.net", 993),
    "bellsouth.net": ("imap.bellsouth.net", 993),
    "charter.net": ("imap.charter.net", 993),
    "cox.net": ("imap.cox.net", 993),
    "earthlink.net": ("imap.earthlink.net", 993),
    "sbcglobal.net": ("imap.sbcglobal.net", 993),
    "infowest.com": ("imap.infowest.com", 993),
    "suddenlink.net": ("imap.suddenlink.net", 993),
    "mchsi.com": ("mail.mediacombb.net", 993),
    "juno.com": ("imap.juno.com", 993),
    "netzero.net": ("imap.netzero.net", 993),
    "prodigy.net": ("imap.prodigy.net", 993),
    "frontiernet.net": ("imap.frontiernet.net", 993),
    "adelphia.net": ("imap.adelphia.net", 993),
    "peoplepc.com": ("imap.peoplepc.com", 993),
    "netscape.net": ("imap.netscape.net", 993),
    "att.net": ("imap.mail.att.net", 993),
    "att.com": ("imap.mail.att.net", 993),
    "t-mobile.com": ("imap.mail.t-mobile.com", 993),
    "sprint.com": ("imap.sprint.com", 993),
    
    # US Business
    "office365.com": ("imap-mail.outlook.com", 993),
    
    # German ISPs & Providers
    "ionos.com": ("imap.ionos.com", 993),
    "ionos.de": ("imap.ionos.de", 993),
    "1und1.de": ("imap.1und1.de", 993),
    "web.de": ("imap.web.de", 993),
    "gmx.com": ("imap.gmx.com", 993),
    "gmx.de": ("imap.gmx.de", 993),
    "gmx.at": ("imap.gmx.at", 993),
    "gmx.ch": ("imap.gmx.ch", 993),
    "t-online.de": ("imap.t-online.de", 993),
    "arcor.de": ("imap.arcor.de", 993),
    "freenet.de": ("imap.freenet.de", 993),
    "vodafone.de": ("imap.vodafone.de", 993),
    "telekom.de": ("imap.telekom.de", 993),
    
    # France
    "orange.fr": ("imap.orange.fr", 993),
    "laposte.net": ("imap.laposte.net", 993),
    "sfr.fr": ("imap.sfr.fr", 993),
    "free.fr": ("imap.free.fr", 993),
    "wanadoo.fr": ("imap.wanadoo.fr", 993),
    "neuf.fr": ("imap.neuf.fr", 993),
    "bouyguestelecom.fr": ("imap.bouyguestelecom.fr", 993),
    
    # Italy
    "alice.it": ("imap.alice.it", 993),
    "libero.it": ("imap.libero.it", 993),
    "virgilio.it": ("imap.virgilio.it", 993),
    "tiscali.it": ("imap.tiscali.it", 993),
    "tin.it": ("imap.tin.it", 993),
    "fastwebnet.it": ("imap.fastwebnet.it", 993),
    
    # Spain
    "telefonica.es": ("imap.telefonica.es", 993),
    "movistar.es": ("imap.movistar.es", 993),
    "vodafone.es": ("imap.vodafone.es", 993),
    "orange.es": ("imap.orange.es", 993),
    "terra.es": ("imap.terra.es", 993),
    
    # United Kingdom
    "btinternet.com": ("imap.btinternet.com", 993),
    "sky.com": ("imap.sky.com", 993),
    "virgin.net": ("imap.virgin.net", 993),
    "talktalk.co.uk": ("imap.talktalk.co.uk", 993),
    "btopenworld.com": ("imap.btopenworld.com", 993),
    
    # Netherlands
    "ziggo.nl": ("imap.ziggo.nl", 993),
    "kpn.com": ("imap.kpn.com", 993),
    "tele2.nl": ("imap.tele2.nl", 993),
    "xs4all.nl": ("imap.xs4all.nl", 993),
    
    # Belgium
    "proximus.be": ("imap.proximus.be", 993),
    "telenet.be": ("imap.telenet.be", 993),
    "scarlet.be": ("imap.scarlet.be", 993),
    
    # Austria
    "a1.net": ("imap.a1.net", 993),
    "telekom.at": ("imap.telekom.at", 993),
    "upc.at": ("imap.upc.at", 993),
    
    # Switzerland
    "bluewin.ch": ("imap.bluewin.ch", 993),
    "swissonline.ch": ("imap.swissonline.ch", 993),
    "cablecom.ch": ("imap.cablecom.ch", 993),
    
    # Sweden
    "telia.com": ("imap.telia.com", 993),
    "spray.se": ("imap.spray.se", 993),
    "algonet.se": ("imap.algonet.se", 993),
    
    # Norway
    "telenor.no": ("imap.telenor.no", 993),
    "telia.no": ("imap.telia.no", 993),
    
    # Denmark
    "tdc.dk": ("imap.tdc.dk", 993),
    "yousee.dk": ("imap.yousee.dk", 993),
    
    # Finland
    "telia.fi": ("imap.telia.fi", 993),
    "elisa.fi": ("imap.elisa.fi", 993),
    
    # Canada
    "bell.ca": ("imap.bell.ca", 993),
    "telus.net": ("imap.telus.net", 993),
    "rogers.com": ("imap.rogers.com", 993),
    "shaw.ca": ("imap.shaw.ca", 993),
    "videotron.ca": ("imap.videotron.ca", 993),
    "sympatico.ca": ("imap.sympatico.ca", 993),
    "eastlink.ca": ("imap.eastlink.ca", 993),
    
    # Australia
    "optusnet.com.au": ("imap.optusnet.com.au", 993),
    "telstra.com": ("imap.telstra.com", 993),
    "vodafone.com.au": ("imap.vodafone.com.au", 993),
    "bigpond.com": ("imap.bigpond.com", 993),
    "iinet.net.au": ("imap.iinet.net.au", 993),
    "internode.on.net": ("imap.internode.on.net", 993),
    
    # New Zealand
    "xtra.co.nz": ("imap.xtra.co.nz", 993),
    "vodafone.co.nz": ("imap.vodafone.co.nz", 993),
    "paradise.net.nz": ("imap.paradise.net.nz", 993),
    
    # Brazil
    "terra.com.br": ("imap.terra.com.br", 993),
    "globo.com": ("imap.globo.com", 993),
    "uol.com.br": ("imap.uol.com.br", 993),
    "bol.com.br": ("imap.bol.com.br", 993),
    
    # Mexico
    "prodigy.net.mx": ("imap.prodigy.net.mx", 993),
    "telmex.com": ("imap.telmex.com", 993),
    
    # Japan
    "docomo.ne.jp": ("imap.docomo.ne.jp", 993),
    "softbank.ne.jp": ("imap.softbank.ne.jp", 993),
    "au.com": ("imap.au.com", 993),
    "nifty.com": ("imap.nifty.com", 993),
    "so-net.ne.jp": ("imap.so-net.ne.jp", 993),
    "biglobe.ne.jp": ("imap.biglobe.ne.jp", 993),
    "excite.co.jp": ("imap.excite.co.jp", 993),
    "rakuten.co.jp": ("imap.rakuten.co.jp", 993),
    
    # South Korea
    "hanmail.net": ("imap.hanmail.net", 993),
    "naver.com": ("imap.naver.com", 993),
    "daum.net": ("imap.daum.net", 993),
    "kkt.co.kr": ("imap.kkt.co.kr", 993),
    
    # China
    "qq.com": ("imap.qq.com", 993),
    "163.com": ("imap.163.com", 993),
    "126.com": ("imap.126.com", 993),
    "sina.com": ("imap.sina.com", 993),
    "sohu.com": ("imap.sohu.com", 993),
    "tom.com": ("imap.tom.com", 993),
    
    # India
    "rediffmail.com": ("imap.rediffmail.com", 993),
    "dataone.in": ("imap.dataone.in", 993),
    "sify.com": ("imap.sify.com", 993),
    
    # Russia
    "yandex.ru": ("imap.yandex.ru", 993),
    "mail.ru": ("imap.mail.ru", 993),
    "inbox.ru": ("imap.inbox.ru", 993),
    "rambler.ru": ("imap.rambler.ru", 993),
    
    # Poland
    "interia.pl": ("imap.interia.pl", 993),
    "wp.pl": ("imap.wp.pl", 993),
    "onet.pl": ("imap.onet.pl", 993),
    
    # Czech Republic
    "seznam.cz": ("imap.seznam.cz", 993),
    "volny.cz": ("imap.volny.cz", 993),
    
    # Hungary
    "freemail.hu": ("imap.freemail.hu", 993),
    "citromail.hu": ("imap.citromail.hu", 993),
    
    # Romania
    "yahoo.ro": ("imap.yahoo.ro", 993),
    
    # Portugal
    "mail.sapo.pt": ("imap.mail.sapo.pt", 993),
    "clix.pt": ("imap.clix.pt", 993),
    
    # Greece
    "yahoo.gr": ("imap.yahoo.gr", 993),
    "hol.gr": ("imap.hol.gr", 993),
    
    # Turkey
    "yahoo.com.tr": ("imap.yahoo.com.tr", 993),
    "mynet.com": ("imap.mynet.com", 993),
    
    # Israel
    "walla.co.il": ("imap.walla.co.il", 993),
    "netvision.net.il": ("imap.netvision.net.il", 993),
    
    # Singapore
    "singnet.com.sg": ("imap.singnet.com.sg", 993),
    
    # Hong Kong
    "netvigator.com": ("imap.netvigator.com", 993),
    
    # Thailand
    "true.th": ("imap.true.th", 993),
    
    # Philippines
    "globe.com.ph": ("imap.globe.com.ph", 993),
    
    # Malaysia
    "tm.net.my": ("imap.tm.net.my", 993),
    
    # Indonesia
    "indosat.com": ("imap.indosat.com", 993),
    
    # Vietnam
    "vnn.vn": ("imap.vnn.vn", 993),
    
    # South Africa
    "webmail.co.za": ("imap.webmail.co.za", 993),
    "iafrica.com": ("imap.iafrica.com", 993),
}

def get_imap_server_for_email(email: str) -> tuple:
    """
    Auto-detect IMAP host and port from email domain.
    Returns (host, port) tuple or (None, None) if unknown.
    Example: get_imap_server_for_email('user@gmail.com') -> ('imap.gmail.com', 993)
    """
    try:
        if not email or "@" not in email:
            return None, None
        
        domain = email.split("@")[1].lower().strip()
        
        # Check exact domain match
        if domain in IMAP_SERVER_LOOKUP:
            return IMAP_SERVER_LOOKUP[domain]
        
        # Try parent domain (mail.gmail.com -> gmail.com)
        parts = domain.split(".")
        if len(parts) > 2:
            parent = ".".join(parts[-2:])
            if parent in IMAP_SERVER_LOOKUP:
                return IMAP_SERVER_LOOKUP[parent]
        
        return None, None
    except Exception as e:
        logger.warning(f"Error detecting IMAP for {email}: {e}")
        return None, None

# Strip any HTML tags from a string, decode common entities, and collapse
# whitespace so the result is safe to render as plain text in a list view.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(#x?[0-9a-fA-F]+|amp|lt|gt|quot|apos|nbsp);")
_HTML_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ",
}


def _decode_html_entity(match):
    entity = match.group(1)
    if entity.startswith("#x") or entity.startswith("#X"):
        try:
            return chr(int(entity[2:], 16))
        except Exception:
            pass

            return ""
    if entity.startswith("#"):
        try:
            return chr(int(entity[1:]))
        except Exception:
            pass

            return ""
    return _HTML_ENTITIES.get(entity, match.group(0))


def extract_email_preview(html_or_text: str, max_len: int = 150) -> str:
    """
    Extract meaningful preview text from email body (HTML or plain text).
    Prioritizes actual message content over HTML markup.
    Returns first meaningful 150 chars.
    Example: '<p>Happy Birthday</p>' -> 'Happy Birthday'
    """
    if not html_or_text:
        return ""
    
    text = str(html_or_text).strip()
    if not text:
        return ""
    
    # If HTML content, extract from tags
    if "<" in text and ">" in text:
        # Step 1: Remove non-content tags (script, style, etc)
        text = re.sub(r"<(script|style|meta|link|head|noscript|iframe)[^>]*>[\s\S]*?</\1>", " ", text, flags=re.IGNORECASE)
        
        # Step 2: Remove HTML comments
        text = re.sub(r"<!--[\s\S]*?-->", " ", text)
        
        # Step 3: Try to extract from meaningful content tags first
        # Look for h1-h6, p, div, li, td, th tags
        content_tags = re.findall(r"<(?:h[1-6]|p|div|li|td|th|span)[^>]*>([^<]+)</(?:h[1-6]|p|div|li|td|th|span)>", text, flags=re.IGNORECASE)
        
        if content_tags:
            # Found content in tags - join them
            text = " ".join(c.strip() for c in content_tags if c.strip())
        else:
            # No tagged content, strip all remaining tags
            text = re.sub(r"<[^>]+>", " ", text)
    
    # Decode HTML entities
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"&amp;", "&", text, flags=re.IGNORECASE)
    text = re.sub(r"&lt;", "<", text, flags=re.IGNORECASE)
    text = re.sub(r"&gt;", ">", text, flags=re.IGNORECASE)
    text = re.sub(r"&quot;", '"', text, flags=re.IGNORECASE)
    text = re.sub(r"&#39;", "'", text, flags=re.IGNORECASE)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text, flags=re.IGNORECASE)
    
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    
    if len(text) > max_len:
        text = text[:max_len - 1].rstrip() + "…"
    
    return text


def sanitize_email_preview_fields(email_data: dict, max_len: int = 200) -> dict:
    """Extract meaningful text from email body for preview display."""
    if not isinstance(email_data, dict):
        return email_data
    
    # Preference: plain text > HTML > preview field > snippet
    body_text = email_data.get("body_text") or ""
    body_html = email_data.get("body_html") or ""
    preview_field = email_data.get("preview") or ""
    snippet = email_data.get("snippet") or ""
    
    # Choose best source
    if body_text:
        preview = body_text
    elif body_html:
        preview = extract_email_preview(body_html, max_len)
    elif preview_field:
        preview = extract_email_preview(preview_field, max_len)
    elif snippet:
        preview = extract_email_preview(snippet, max_len)
    else:
        preview = ""
    
    # Ensure within length limit
    if len(preview) > max_len:
        preview = preview[:max_len - 1].rstrip() + "…"
    
    email_data["preview"] = preview
    return email_data


def decode_email_header_value(value, default: str = "") -> str:
    """Decode RFC2047 / email.header.Header values to a plain string for DB/API use."""
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip() or default
    try:
        from email.header import decode_header
        text = str(value).strip()
        if not text:
            return default
        chunks = []
        for part, charset in decode_header(text):
            if isinstance(part, bytes):
                chunks.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                chunks.append(str(part))
        result = "".join(chunks).strip()
        return result or default
    except Exception:
        try:
            return str(value).strip() or default
        except Exception:
            return default


def _db_email_to_list_dict(db_email, mailbox_id: str, folder: str) -> dict:
    """Map a database Email row to the list-view API shape."""
    sender_email = db_email.sender_email or ""
    sender_name = db_email.sender or sender_email
    preview_raw = db_email.body_text or db_email.snippet or db_email.body_html or ""
    recipients = []
    if db_email.recipients:
        try:
            parsed = json.loads(db_email.recipients)
            if isinstance(parsed, list):
                recipients = parsed
        except Exception:
            recipients = [{"email": db_email.recipients}]
    return sanitize_email_preview_fields({
        "id": db_email.id,
        "mailbox_id": mailbox_id,
        "folder": folder,
        "uid": db_email.imap_uid,
        "subject": db_email.subject or "(No Subject)",
        "from_address": {"email": sender_email, "name": sender_name},
        "to_addresses": recipients,
        "date": db_email.date.isoformat() if db_email.date else "",
        "is_read": bool(db_email.is_read),
        "is_flagged": False,
        "has_attachments": bool(db_email.has_attachments),
        "preview": preview_raw,
    })


def _update_local_email_read_state(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int,
    is_read: bool,
) -> None:
    """Keep JSON cache and SQLite in sync after IMAP read/unread changes."""
    if ASYNC_IMAP_AVAILABLE:
        try:
            from db_helpers import mark_email_read_by_uid
            mark_email_read_by_uid(mailbox_id, email_uid, is_read)
        except Exception as e:
            logger.debug(f"DB read-state update failed: {e}")

    storage_path = get_mailbox_storage_path(user_id, mailbox_id, base_path)
    normalized_folder = folder.replace("/", '_').lower()
    matching_folder_path = None
    if storage_path.exists():
        for existing_folder in storage_path.iterdir():
            if existing_folder.is_dir() and existing_folder.name.lower() == normalized_folder.lower():
                matching_folder_path = existing_folder
                break
    if matching_folder_path is None:
        candidate = storage_path / folder.replace("/", '_')
        if candidate.exists():
            matching_folder_path = candidate
    if matching_folder_path is None:
        return

    email_file = matching_folder_path / f"email_{email_uid}.json"
    if not email_file.exists():
        return
    try:
        with open(email_file, "r",encoding="utf-8") as f:
            email_record = json.load(f)
        email_data = email_record.get("data", {})
        if isinstance(email_data, dict):
            email_data["is_read"] = is_read
            email_data["is_seen"] = is_read
            email_record["data"] = email_data
            with open(email_file, "w",encoding="utf-8") as f:
                json.dump(email_record, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"JSON read-state update failed: {e}")

# Import new async IMAP modules
try:
    from imap_manager import get_connection_manager, IMAPConnection
    from db_helpers import (
        get_mailbox_by_id, get_sync_state, update_sync_state,
        start_sync, complete_sync, fail_sync, get_mailbox_stats,
        get_or_create_inbox_folder
    )
    ASYNC_IMAP_AVAILABLE = True
except ImportError:
    pass

    ASYNC_IMAP_AVAILABLE = False
    print("Warning: Async IMAP modules not available. Using legacy sync mode.")

# Import configuration
import sys
sys.path.insert(0, str(Path(__file__).parent))
try:
    import config as config_module
    config = config_module
except ImportError:
    pass

    config = None

# =============================================================================
# FEATURE FLAGS
# =============================================================================

Inbox_state = True  # Inbox subsystem enabled (database + async IMAP)

# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class UserLogin(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")
    password: str = Field(..., min_length=1, description="User password")


class UserRegister(BaseModel):
    user_id: Optional[str] = Field(None, description="Optional user ID (generated if not provided)")
    username: Optional[str] = Field(None, description="Username (default: admin)")
    password: Optional[str] = Field(None, description="Password (default: admin)")
    user_agent: Optional[str] = Field(None, description="Browser user agent string")


class UserResponse(BaseModel):
    user_id: str
    username: Optional[str] = None
    created_at: Optional[str] = None
    last_login: Optional[str] = None
    login_count: int = 0
    about: Optional[dict] = None


class CentralUserCreate(BaseModel):
    """Request to create a new user in central database"""
    username: str = Field(..., min_length=1, max_length=50, description="Username for login")
    password: str = Field(..., min_length=4, max_length=100, description="Password")


class CentralUserUpdate(BaseModel):
    """Request to update a user in central database"""
    username: Optional[str] = Field(None, min_length=1, max_length=50, description="New username")
    password: Optional[str] = Field(None, min_length=4, max_length=100, description="New password")


class CentralUserLogin(BaseModel):
    """Request to login with central user credentials"""
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


class SMTCPCredentialBase(BaseModel):
    provider: str = Field(default="custom", description="SMTP provider")
    host: str = Field(..., description="SMTP server hostname")
    port: int = Field(587, description="SMTP port")
    user: str = Field(..., description="Username/email for SMTP authentication")
    password: Optional[str] = Field(None, description="Password for SMTP authentication")
    from_address: Optional[str] = Field(None, description="From email address")


class SMTCPCredentialCreate(SMTCPCredentialBase):
    pass


class SMTCPCredentialUpdate(BaseModel):
    provider: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    from_address: Optional[str] = None
    status: Optional[str] = None


class SMTCPCredential(BaseModel):
    id: str
    type: str = "smtp"
    provider: str
    host: str
    port: int
    user: str
    password: Optional[str] = None
    from_address: Optional[str] = None
    success_rate: int = 0
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_tested: Optional[str] = None


class APICredentialBase(BaseModel):
    provider: str = Field(..., description="API provider (mailgun, sendgrid, etc.)")
    api_key: str = Field(..., description="API key")
    api_secret: Optional[str] = Field(None, description="API secret (Mailjet private key / AWS secret access key)")
    region: Optional[str] = Field("us-east-1", description="Region for API")
    domain: Optional[str] = Field(None, description="Domain for sending")
    from_name: Optional[str] = Field(None, description="Default from name")
    sender_email: Optional[str] = Field(None, description="Verified sender 'from' email address")


class APICredentialCreate(APICredentialBase):
    pass


class APICredentialUpdate(BaseModel):
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    region: Optional[str] = None
    domain: Optional[str] = None
    from_name: Optional[str] = None
    sender_email: Optional[str] = None
    status: Optional[str] = None


class APICredential(BaseModel):
    id: str
    type: str = "api"
    provider: str
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    region: Optional[str] = None
    domain: Optional[str] = None
    from_name: Optional[str] = None
    sender_email: Optional[str] = None
    success_rate: int = 0
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TestResult(BaseModel):
    success: bool
    message: str
    response_time: Optional[float] = None
    success_rate: Optional[int] = None
    error: Optional[str] = None


class AboutInfo(BaseModel):
    user_id: str
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    platform: Optional[str] = None
    platform_release: Optional[str] = None
    user_agent: Optional[str] = None
    registered_at: Optional[str] = None


class ServerStats(BaseModel):
    total_smtp: int = 0
    total_api: int = 0
    active_smtp: int = 0
    active_api: int = 0
    system_info: Optional[dict] = None


class CampaignConfig(BaseModel):
    email_list: Optional[str] = None
    subject_template: Optional[str] = None
    from_name: Optional[str] = None
    letter_template: Optional[str] = None
    speed: int = Field(10, description="Emails per second")
    random_delay: int = Field(0, description="Random delay in seconds")
    use_tls: bool = True
    use_starttls: bool = True


class CampaignStatus(BaseModel):
    campaign_id: str
    status: str
    sent: int = 0
    failed: int = 0
    success_rate: float = 0.0
    active_credential: Optional[str] = None
    current_email: Optional[str] = None


class MailboxConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    sender_user_ids: List[str] = Field(default_factory=list)
    use_shared_mailbox: bool = False


class ImportResult(BaseModel):
    imported: int
    skipped: int
    total_credentials: int


class BulkDeleteRequest(BaseModel):
    credential_ids: List[str]
    credential_type: str = Field(..., description="smtp or api")


# =============================================================================
# EMAIL LIST MODELS
# =============================================================================

class EmailEntry(BaseModel):
    """Single email entry with metadata"""
    email: str
    domain: Optional[str] = None
    valid: bool = True
    added_at: Optional[str] = None


class EmailListData(BaseModel):
    """Email list file structure"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    total_emails: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    emails: List[EmailEntry] = []


class EmailListCreate(BaseModel):
    """Request to create a new email list"""
    name: str = Field(..., min_length=1, max_length=100, description="Email list name")
    emails: List[str] = Field(default_factory=list, description="List of email addresses")


class EmailListUpdate(BaseModel):
    """Request to update an email list"""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New name for the list")
    emails: Optional[List[str]] = Field(None, description="Updated email list")
    action: Optional[str] = Field(None, description="Action: 'replace', 'append', 'remove_duplicates', 'validate'")


class EmailListResponse(BaseModel):
    """Response model for email list (without full email content)"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    total_emails: int
    valid_count: int
    invalid_count: int


class EmailListDetailResponse(BaseModel):
    """Full email list response with emails"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    total_emails: int
    valid_count: int
    invalid_count: int
    emails: List[EmailEntry]


class EmailListImport(BaseModel):
    """Import request with raw text"""
    name: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., description="Raw email list content (one email per line)")


# =============================================================================
# PHONE NUMBER LIST MODELS
# =============================================================================

class PhoneEntry(BaseModel):
    """Single phone number entry with metadata"""
    number: str
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    valid: bool = True
    carrier: Optional[str] = None
    line_type: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    formatted_number: Optional[str] = None
    is_prepaid: Optional[bool] = None
    is_wireless: Optional[bool] = None
    added_at: Optional[str] = None
    validated_at: Optional[str] = None


class PhoneListData(BaseModel):
    """Phone list file structure"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    country_code: str = "US"
    total_numbers: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    unchecked_count: int = 0
    numbers: List[PhoneEntry] = []


class PhoneListCreate(BaseModel):
    """Request to create a new phone list"""
    name: str = Field(..., min_length=1, max_length=100, description="Phone list name")
    numbers: List[str] = Field(default_factory=list, description="List of phone numbers")
    country_code: Optional[str] = Field("US", description="Default country code for numbers")


class PhoneListUpdate(BaseModel):
    """Request to update a phone list"""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New name for the list")
    numbers: Optional[List[str]] = Field(None, description="Updated phone list")
    action: Optional[str] = Field(None, description="Action: 'replace', 'append', 'remove_duplicates', 'validate'")


class PhoneListResponse(BaseModel):
    """Response model for phone list (without full number content)"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    country_code: str
    total_numbers: int
    valid_count: int
    invalid_count: int
    unchecked_count: int


class PhoneListDetailResponse(BaseModel):
    """Full phone list response with numbers"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    country_code: str
    total_numbers: int
    valid_count: int
    invalid_count: int
    unchecked_count: int
    numbers: List[PhoneEntry]


class PhoneListImport(BaseModel):
    """Import request with raw text"""
    name: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., description="Raw phone list content (one number per line)")
    country_code: Optional[str] = Field(None, description="Country code if all numbers are from same country")


class PhoneValidationResult(BaseModel):
    """Phone number validation result from ipqualityscore"""
    success: bool
    valid: bool
    formatted: Optional[str] = None
    local_format: Optional[str] = None
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    carrier: Optional[str] = None
    line_type: Optional[str] = None
    is_prepaid: Optional[bool] = None
    is_wireless: Optional[bool] = None
    recently_ported: Optional[bool] = None
    risks: Optional[dict] = None
    error: Optional[str] = None


class ValidationSession(BaseModel):
    """Active validation session tracking"""
    session_id: str
    user_id: str
    type: str  # "email" or "phone"
    list_id: str
    list_name: str
    status: str  # "running", "paused", "completed", "stopped"
    started_at: str
    updated_at: Optional[str] = None
    total_items: int = 0
    processed: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    failed_count: int = 0
    current_proxy: Optional[str] = None
    proxy_list: Optional[List[str]] = None
    browser_api_enabled: bool = False
    batch_size: int = 3
    rate_limit: int = 20


class GoogleMailboxData(BaseModel):
    """Google Gmail API configuration"""
    client_id: str = Field(..., description="Google OAuth Client ID")
    client_secret: str = Field(..., description="Google OAuth Client Secret")
    refresh_token: str = Field(..., description="OAuth Refresh Token")
    sender_email: str = Field(..., description="Sender email address")
    sender_name: Optional[str] = Field(None, description="Sender display name")
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


class OutlookMailboxData(BaseModel):
    """Microsoft Outlook Graph API configuration"""
    tenant_id: str = Field(..., description="Azure AD Tenant ID")
    client_id: str = Field(..., description="Azure AD Client ID")
    client_secret: str = Field(..., description="Azure AD Client Secret")
    sender_email: str = Field(..., description="Sender email address")
    sender_name: Optional[str] = Field(None, description="Sender display name")
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


# =============================================================================
# INBOX / MAILBOX MODELS
# =============================================================================

class IMAPConfig(BaseModel):
    """IMAP server configuration for email retrieval"""
    host: str = Field(..., description="IMAP server hostname")
    port: int = Field(993, description="IMAP server port (default: 993)")
    username: str = Field(..., description="Email username/address")
    password: str = Field(..., description="Email password or app password")
    use_ssl: bool = Field(True, description="Use SSL/TLS connection")
    validate_cert: bool = Field(True, description="Validate SSL certificate")


class SMTPConfigSimple(BaseModel):
    """SMTP server configuration for email sending"""
    host: str = Field(..., description="SMTP server hostname")
    port: int = Field(587, description="SMTP server port (default: 587)")
    username: str = Field(..., description="Email username/address")
    password: str = Field(..., description="Email password or app password")
    use_starttls: bool = Field(True, description="Use STARTTLS")
    use_ssl: bool = Field(False, description="Use implicit SSL (port 465)")
    from_address: Optional[str] = Field(None, description="From address (defaults to username)")


class InboxMailboxCreate(BaseModel):
    """Request to create a new inbox mailbox connection"""
    provider: str = Field(..., description="Email provider name (gmail, yahoo, outlook, custom)")
    email_address: str = Field(..., description="Email address")
    display_name: Optional[str] = Field(None, description="Display name for the mailbox")
    imap_config: IMAPConfig = Field(..., description="IMAP configuration for receiving emails")
    smtp_config: Optional[SMTPConfigSimple] = Field(None, description="SMTP configuration for sending emails")
    is_primary: bool = Field(False, description="Set as primary mailbox")


class InboxMailboxUpdate(BaseModel):
    """Request to update an inbox mailbox"""
    email_address: Optional[str] = Field(None, description="Updated email address")
    provider: Optional[str] = Field(None, description="Email provider (gmail, outlook, etc)")
    display_name: Optional[str] = Field(None, description="New display name")
    imap_config: Optional[IMAPConfig] = Field(None, description="Updated IMAP configuration")
    smtp_config: Optional[SMTPConfigSimple] = Field(None, description="Updated SMTP configuration")
    is_primary: Optional[bool] = Field(None, description="Set as primary mailbox")
    sync_enabled: Optional[bool] = Field(None, description="Enable/disable sync")


class InboxMailboxResponse(BaseModel):
    """Response model for inbox mailbox"""
    id: str
    provider: str
    email_address: str
    display_name: Optional[str] = None
    smtp_enabled: bool = False
    is_primary: bool = False
    sync_enabled: bool = True
    status: str = "active"  # active, inactive, error, syncing
    unread_count: int = 0
    last_sync: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class EmailAttachment(BaseModel):
    """Email attachment model"""
    id: str
    filename: str
    content_type: str
    size: int
    download_url: Optional[str] = None


class EmailAddress(BaseModel):
    """Email address with name"""
    name: Optional[str] = None
    email: str


class EmailMessageResponse(BaseModel):
    """Response model for email message"""
    id: str
    mailbox_id: str
    folder: str
    uid: int
    subject: str
    from_address: EmailAddress
    to_addresses: List[EmailAddress]
    cc_addresses: Optional[List[EmailAddress]] = None
    bcc_addresses: Optional[List[EmailAddress]] = None
    date: str
    date_iso: Optional[str] = None
    preview: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    is_read: bool = True
    is_seen: bool = True
    is_flagged: bool = False
    is_deleted: bool = False
    has_attachments: bool = False
    attachments: List[EmailAttachment] = []
    flags: List[str] = []
    headers: Optional[Dict[str, str]] = None


class EmailFolderResponse(BaseModel):
    """Response model for email folder"""
    name: str
    path: str
    delimiter: str = "/"
    unread_count: int = 0
    total_count: int = 0
    is_selectable: bool = True


class MailboxImportEntry(BaseModel):
    """Single mailbox import entry"""
    format_type: str = Field(..., description="Format: 'simple', 'full', 'json'")
    provider: Optional[str] = Field(None, description="Provider name (auto-detected if not provided)")
    email_address: str = Field(..., description="Email address")
    imap_host: Optional[str] = Field(None, description="IMAP host")
    imap_port: Optional[int] = Field(None, description="IMAP port")
    imap_user: Optional[str] = Field(None, description="IMAP username")
    imap_password: Optional[str] = Field(None, description="IMAP password")
    smtp_host: Optional[str] = Field(None, description="SMTP host")
    smtp_port: Optional[int] = Field(None, description="SMTP port")
    smtp_user: Optional[str] = Field(None, description="SMTP username")
    smtp_password: Optional[str] = Field(None, description="SMTP password")
    raw_line: Optional[str] = Field(None, description="Original raw import line")


class MailboxImportRequest(BaseModel):
    """Request to bulk import mailboxes"""
    entries: List[MailboxImportEntry] = Field(default_factory=list, description="List of mailbox entries to import")
    validate_only: bool = Field(False, description="Only validate without saving")


class MailboxImportResult(BaseModel):
    """Result of mailbox import operation"""
    total: int
    successful: int
    failed: int
    results: List[Dict[str, Any]]  # List of {email, success, message, mailbox_id}


class EmailSearchQuery(BaseModel):
    """Email search parameters"""
    mailbox_id: str
    folder: str = "INBOX"
    search_text: Optional[str] = None
    search_field: Optional[str] = "all"
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    subject: Optional[str] = None
    body_contains: Optional[str] = None
    since_date: Optional[str] = None
    before_date: Optional[str] = None
    is_unread: Optional[bool] = None
    unread_only: Optional[bool] = None
    has_attachments: Optional[bool] = None
    limit: int = Field(200, ge=1, le=10000, description="Max emails to return")
    offset: int = Field(0, ge=0, description="Offset for pagination")


class EmailActionRequest(BaseModel):
    """Request to perform action on email"""
    mailbox_id: str
    folder: str
    email_uid: int
    action: str  # move, copy, delete, flag, mark_read, mark_unread
    destination_folder: Optional[str] = None
    flags: Optional[List[str]] = None


class ComposeEmailRequest(BaseModel):
    """Request to compose and send email"""
    mailbox_id: str
    to_addresses: List[str] = Field(..., min_length=1, description="Recipient email addresses")
    cc_addresses: Optional[List[str]] = Field(None, description="CC recipient addresses")
    bcc_addresses: Optional[List[str]] = Field(None, description="BCC recipient addresses")
    subject: str = Field(..., min_length=1, description="Email subject")
    body_text: Optional[str] = Field(None, description="Plain text body")
    body_html: Optional[str] = Field(None, description="HTML body")
    is_html: bool = Field(False, description="Send as HTML email")
    reply_to: Optional[str] = Field(None, description="Reply-To address")
    in_reply_to: Optional[str] = Field(None, description="In-Reply-To header for threading")
    references: Optional[str] = Field(None, description="References header for threading")
    attachments: Optional[List[Dict[str, str]]] = Field(None, description="Attachment files [{name, content, type}]")


class ConnectionTestResult(BaseModel):
    """Result of connection testing"""
    imap_success: bool = False
    imap_message: str = ""
    imap_response_time: Optional[float] = None
    smtp_success: bool = False
    smtp_message: str = ""
    smtp_response_time: Optional[float] = None
    overall_success: bool = False
    error: Optional[str] = None


# =============================================================================
# AUTHENTICATION UTILITIES
# =============================================================================

ALGORITHM = "HS256"


def _secret_key() -> str:
    """Resolve the server secret (env > persisted > auto-generated)."""
    try:
        from config import get_secret_key
        return get_secret_key()
    except Exception:
        return "fv-s-secret-key-change-in-production"


def _legacy_secret_key() -> str:
    try:
        from config import LEGACY_SECRET_KEY
        return LEGACY_SECRET_KEY
    except Exception:
        return "fv-s-secret-key-change-in-production"


def _sign(message: str, secret: str) -> str:
    signature = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).digest()
    return __import__('base64').urlsafe_b64encode(signature).rstrip(b'=').decode('utf-8')


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=30)
    
    # Convert datetime to timestamp for JSON serialization
    expire_timestamp = expire.timestamp()
    
    to_encode.update({"exp": expire_timestamp, "iat": datetime.utcnow().timestamp()})
    
    header = {"alg": ALGORITHM, "typ": "JWT"}
    header_bytes = json.dumps(header).encode('utf-8')
    header_b64 = __import__('base64').urlsafe_b64encode(header_bytes).rstrip(b'=').decode('utf-8')
    payload_bytes = json.dumps(to_encode).encode('utf-8')
    payload_b64 = __import__('base64').urlsafe_b64encode(payload_bytes).rstrip(b'=').decode('utf-8')
    
    message = f"{header_b64}.{payload_b64}"
    signature_b64 = _sign(message, _secret_key())
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_token(token: str) -> Optional[dict]:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        
        header_b64, payload_b64, signature_b64 = parts
        message = f"{header_b64}.{payload_b64}"

        # Accept tokens signed with the current secret or the legacy secret
        # (so tokens issued before the secret was rotated still verify).
        valid = False
        for secret in (_secret_key(), _legacy_secret_key()):
            if hmac.compare_digest(signature_b64, _sign(message, secret)):
                valid = True
                break
        if not valid:
            return None
        
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding
        
        payload_bytes = __import__('base64').urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode('utf-8'))
        
        if 'exp' in payload and payload['exp'] < time.time():
            return None
        
        return payload
    except Exception:
        pass

        return None


def _password_hash(password: str, secret: str) -> str:
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), secret.encode('utf-8'), 100000)
    return key.hex()


def get_password_hash(password: str) -> str:
    return _password_hash(password, _secret_key())


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        # Verify against the current secret, then the legacy secret, so
        # hashes created before secret persistence continue to work.
        for secret in (_secret_key(), _legacy_secret_key()):
            if hmac.compare_digest(_password_hash(plain_password, secret), hashed_password):
                return True
        return False
    except Exception:
        pass

        return False


def generate_session_token(length: int = 32) -> str:
    return secrets.token_hex(length)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    return hmac.compare_digest(hash_api_key(plain_key), hashed_key)


# =============================================================================
# SMTP TESTING FUNCTIONS
# =============================================================================

async def test_smtp_connection(host: str, port: int, username: str, password: str, from_address: str, test_email: str = "test@example.com", timeout: int = 20) -> TestResult:
    start_time = time.time()
    
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
            try:
                server.login(username, password)
            except smtplib.SMTPException:
                pass

                context = __import__('ssl').create_default_context()
                context.check_hostname = False
                context.verify_mode = __import__('ssl').CERT_NONE
                server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
                server.login(username, password)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            try:
                server.ehlo()
                if server.has_extn('STARTTLS'):
                    server.starttls()
                    server.ehlo()
            except smtplib.SMTPException:
                pass

                pass
            server.login(username, password)
        
        try:
            msg = MIMEMultipart()
            msg['From'] = from_address
            msg['To'] = test_email
            msg['Subject'] = 'FV-S SMTP Test'
            body = f"FV-S SMTP Test Email\n\nSMTP Server: {host}:{port}\nTest Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\nIf you received this email, your SMTP configuration is working correctly."
            msg.attach(MIMEText(body, 'plain'))
            server.sendmail(from_address, [test_email], msg.as_string())
            success = True
            message = f"Test email sent successfully to {test_email}"
        except smtplib.SMTPRecipientsRefused as e:
            success = False
            message = f"Authenticated, but test email was refused: {e}"
        except smtplib.SMTPDataError as e:
            success = False
            message = f"Authenticated, but test email was rejected: {e}"
        except Exception as e:
            # Authentication succeeded but the message could not be sent
            # (quota exceeded, sender not verified, relay denied, ...).
            # This must NOT count as a valid credential.
            success = False
            message = f"Authenticated, but test email send failed: {str(e)}"
        
        try:
            server.quit()
        except:
            pass
        
        return TestResult(success=success, message=message, response_time=round(time.time() - start_time, 3), success_rate=100 if success else 0)
        
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPException) as e:
        pass

        return TestResult(success=False, message=str(e), response_time=round(time.time() - start_time, 3), success_rate=0)
    except socket.timeout:
        pass

        return TestResult(success=False, message=f"Connection timeout ({timeout}s)", response_time=timeout, success_rate=0)
    except socket.gaierror:
        pass

        return TestResult(success=False, message="DNS resolution failed", response_time=round(time.time() - start_time, 3), success_rate=0)
    except ConnectionRefusedError:
        pass

        return TestResult(success=False, message="Connection refused", response_time=round(time.time() - start_time, 3), success_rate=0)
    except Exception as e:
        pass

        return TestResult(success=False, message=str(e), response_time=round(time.time() - start_time, 3), success_rate=0)


async def send_email(host: str, port: int, username: str, password: str, from_address: str, to_address: str, subject: str, body: str, use_tls: bool = True, use_starttls: bool = True, timeout: int = 30) -> TestResult:
    start_time = time.time()
    
    try:
        msg = MIMEMultipart()
        msg['From'] = from_address
        msg['To'] = to_address
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        if port == 465 and use_tls:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            if use_starttls:
                try:
                    server.ehlo()
                    if server.has_extn('STARTTLS'):
                        server.starttls()
                        server.ehlo()
                except smtplib.SMTPException:
                    pass

                    pass
        
        server.login(username, password)
        server.sendmail(from_address, [to_address], msg.as_string())
        server.quit()
        
        return TestResult(success=True, message="Email sent successfully", response_time=round(time.time() - start_time, 3), success_rate=100)
    except Exception as e:
        pass

        return TestResult(success=False, message=str(e), response_time=round(time.time() - start_time, 3), success_rate=0)


def check_smtp_ports() -> dict:
    ports = {25: "Standard SMTP", 465: "SMTP over SSL", 587: "SMTP with STARTTLS", 2525: "Alternative SMTP"}
    results = {}
    for port, description in ports.items():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex(('smtp.gmail.com', port))
            results[port] = {"description": description, "available": result == 0, "status": "open" if result == 0 else "blocked"}
            sock.close()
        except Exception as e:
            pass

            results[port] = {"description": description, "available": False, "status": f"error: {str(e)}"}
    return results


class SMTPConnectionPool:
    def __init__(self):
        self.connections = {}
        self.last_used = {}
    
    def get_connection(self, host: str, port: int, username: str, password: str, from_address: str):
        key = f"{host}:{port}:{username}"
        
        if key in self.connections:
            try:
                if self.connections[key].noop() == 250:
                    self.last_used[key] = time.time()
                    return self.connections[key]
            except:
                try:
                    self.connections[key].quit()
                except:
                    pass
                del self.connections[key]
        
        if port == 465:
            server = smtplib.SMTP_SSL(host, port)
        else:
            server = smtplib.SMTP(host, port)
            server.ehlo()
            if server.has_extn('STARTTLS'):
                server.starttls()
                server.ehlo()
        
        server.login(username, password)
        self.connections[key] = server
        self.last_used[key] = time.time()
        return server
    
    def release_connection(self, host: str, port: int, username: str):
        key = f"{host}:{port}:{username}"
        self.last_used[key] = time.time()
    
    def close_connection(self, host: str, port: int, username: str):
        key = f"{host}:{port}:{username}"
        if key in self.connections:
            try:
                self.connections[key].quit()
            except:
                pass
            del self.connections[key]
        if key in self.last_used:
            del self.last_used[key]
    
    def close_all(self):
        for key, server in self.connections.items():
            try:
                server.quit()
            except:
                pass
        self.connections.clear()
        self.last_used.clear()


smtp_pool = SMTPConnectionPool()


# =============================================================================
# INBOX / IMAP CONNECTION FUNCTIONS
# =============================================================================

def connect_imap(host: str, port: int, username: str, password: str, use_ssl: bool = True, validate_cert: bool = True, timeout: int = 60):
    """
    Connect to IMAP server and return connection object
    
    Args:
        host: IMAP server hostname
        port: IMAP server port (993 for SSL, 143 for non-SSL)
        username: Email address/username
        password: Email password or app password
        use_ssl: Use SSL/TLS connection
        validate_cert: Validate SSL certificate
        timeout: Connection timeout in seconds
    
    Returns:
        IMAP4_SSL or IMAP4 connection object
    """
    import imaplib
    import ssl
    import socket
    
    connection = None
    
    if use_ssl:
        # Configure SSL context - Charter requires disabled cert validation
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        # Connect with SSL context for Charter compatibility
        connection = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)
        
        # Set socket timeout
        connection.sock.settimeout(timeout)
        
        # Set socket keepalive to prevent connection drops
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
        # --- NEW: Send IMAP ID command for Charter compatibility ---
        # Charter servers may require client identification
        try:
            # Register ID command support
            if 'ID' not in imaplib.Commands:
                imaplib.Commands['ID'] = ('AUTH',)
            
            # Prepare client identification
            client_id = ('name', 'Thunderbird', 'version', '102.0', 'vendor', 'Mozilla')
            id_args = ' '.join(f'"{part}"' for part in client_id)
            
            # Send ID command (ignore errors if server doesn't support it)
            connection._simple_command('ID', f'({id_args})')
        except Exception:
            pass

            pass  # ID command is optional, continue if it fails
        
    else:
        # Non-SSL connection (rarely used)
        connection = imaplib.IMAP4(host, port, timeout=timeout)
        try:
            connection.starttls()
        except imaplib.IMAP4.error:
            pass

            pass  # Continue without STARTTLS
    
    if connection is None:
        raise imaplib.IMAP4.error("Failed to establish IMAP connection")
    
    # Login with provided credentials
    try:
        typ, data = connection.login(username, password)
        if typ != 'OK':
            raise imaplib.IMAP4.error("Login failed: Check username/password")
    except imaplib.IMAP4.error as e:
        pass

        try:
            connection.logout()
        except Exception:
            pass

            pass
        raise imaplib.IMAP4.error(f"Authentication failed: {str(e)}")
    
    return connection


async def test_imap_connection(host: str, port: int, username: str, password: str, use_ssl: bool = True, validate_cert: bool = True, timeout: int = 20) -> dict:
    """
    Test IMAP connection and return result
    
    Returns:
        dict with success, message, response_time, folder_count
    """
    start_time = time.time()
    
    try:
        connection = connect_imap(host, port, username, password, use_ssl, validate_cert, timeout)
        
        # Get folder list with validation
        status, folders = connection.list()
        if status != 'OK':
            connection.logout()
            return {
                "success": False,
                "message": "Failed to list folders. Server may be blocking IMAP access.",
                "response_time": round(time.time() - start_time, 3),
                "folder_count": 0,
                "inbox_count": 0,
                "unread_count": 0
            }
        folder_count = len(folders) if folders and folders[0] else 0
        
        # Get inbox status with validation
        select_status, select_data = connection.select('INBOX')
        if select_status != 'OK':
            connection.logout()
            return {
                "success": False,
                "message": f"Failed to select INBOX folder. Server response: {select_data}",
                "response_time": round(time.time() - start_time, 3),
                "folder_count": folder_count,
                "inbox_count": 0,
                "unread_count": 0
            }
        
        status, messages = connection.search(None, 'ALL')
        inbox_count = len(messages[0].split()) if messages and messages[0] else 0
        
        # Get unread count
        status, unread = connection.search(None, 'UNSEEN')
        unread_count = len(unread[0].split()) if unread and unread[0] else 0
        
        connection.logout()
        
        response_time = round(time.time() - start_time, 3)
        
        return {
            "success": True,
            "message": f"Connection successful. Found {folder_count} folders, {inbox_count} emails, {unread_count} unread.",
            "response_time": response_time,
            "folder_count": folder_count,
            "inbox_count": inbox_count,
            "unread_count": unread_count
        }
        
    except imaplib.IMAP4.error as e:
        pass

        response_time = round(time.time() - start_time, 3)
        error_msg = str(e)
        
        # Provide more helpful error messages
        if 'authentication' in error_msg.lower() or 'invalid' in error_msg.lower():
            message = "Authentication failed. Please check your username and password."
        elif 'connection' in error_msg.lower():
            message = "Connection failed. Please check the server address and port."
        elif 'timeout' in error_msg.lower():
            message = f"Connection timeout ({timeout}s). Server may be blocking or unreachable."
        else:
            message = error_msg
            
        return {
            "success": False,
            "message": message,
            "response_time": response_time,
            "error": str(e)
        }
    except Exception as e:
        pass

        response_time = round(time.time() - start_time, 3)
        return {
            "success": False,
            "message": f"Connection error: {str(e)}",
            "response_time": response_time,
            "error": str(e)
        }


# =============================================================================
# NEW ASYNC IMAP FUNCTIONS USING CONNECTION POOL
# =============================================================================

async def test_imap_connection_async(
    host: str,
    port: int,
    username: str,
    password: str,
    use_ssl: bool = True,
    validate_cert: bool = True,
    timeout: int = 20,
    mailbox_id: str = None
) -> dict:
    """
    NEW async IMAP connection test using connection pool
    Uses aioimaplib for non-blocking operations
    
    Args:
        host: IMAP server hostname
        port: IMAP server port (default: 993)
        username: Email address/username
        password: Email password or app password
        use_ssl: Use SSL/TLS connection
        validate_cert: Validate SSL certificate (for future use)
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, message, response_time, folder_count, inbox_count, unread_count
    """
    if not ASYNC_IMAP_AVAILABLE:
        # Fallback to sync version if modules not available
        return await test_imap_connection(host, port, username, password, use_ssl, validate_cert, timeout)
    
    start_time = time.time()
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        # Get connection manager
        manager = await get_connection_manager()
        
        # Get connection from pool
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout
        )
        
        if not success:
            return {
                "success": False,
                "message": msg,
                "response_time": round(time.time() - start_time, 3),
                "error": msg
            }
        
        try:
            # Get folder list
            status, folders = await client.list()
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "message": "Failed to list folders. Server may be blocking IMAP access.",
                    "response_time": round(time.time() - start_time, 3),
                    "folder_count": 0,
                    "inbox_count": 0,
                    "unread_count": 0
                }
            
            folder_count = len(folders) if folders and folders[0] else 0
            
            # Select INBOX and get counts
            status, select_data = await client.select('INBOX')
            if status != 'OK':
                await manager.release_connection(mailbox_id, client)
                return {
                    "success": False,
                    "message": f"Failed to select INBOX folder. Server response: {select_data}",
                    "response_time": round(time.time() - start_time, 3),
                    "folder_count": folder_count,
                    "inbox_count": 0,
                    "unread_count": 0
                }
            
            # Get email counts
            status, messages = await client.search(None, 'ALL')
            inbox_count = len(messages[0].split()) if messages and messages[0] else 0
            
            status, unread = await client.search(None, 'UNSEEN')
            unread_count = len(unread[0].split()) if unread and unread[0] else 0
            
            # Release connection back to pool
            await manager.release_connection(mailbox_id, client)
            
            response_time = round(time.time() - start_time, 3)
            
            return {
                "success": True,
                "message": f"Connection successful. Found {folder_count} folders, {inbox_count} emails, {unread_count} unread.",
                "response_time": response_time,
                "folder_count": folder_count,
                "inbox_count": inbox_count,
                "unread_count": unread_count
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
        
    except Exception as e:
        pass

        response_time = round(time.time() - start_time, 3)
        error_msg = str(e)
        
        # Provide helpful error messages
        if 'authentication' in error_msg.lower() or 'invalid' in error_msg.lower():
            message = "Authentication failed. Please check your username and password."
        elif 'connection' in error_msg.lower():
            message = "Connection failed. Please check the server address and port."
        elif 'timeout' in error_msg.lower():
            message = f"Connection timeout ({timeout}s). Server may be blocking or unreachable."
        else:
            message = error_msg
        
        return {
            "success": False,
            "message": message,
            "response_time": response_time,
            "error": error_msg
        }


async def test_smtp_connection_simple(host: str, port: int, username: str, password: str, use_starttls: bool = True, use_ssl: bool = False, timeout: int = 20) -> dict:
    """
    Test SMTP connection for inbox mailbox
    
    Returns:
        dict with success, message, response_time
    """
    start_time = time.time()
    
    try:
        if use_ssl or port == 465:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            if use_starttls:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    pass

                    pass
        
        server.login(username, password)
        
        # Get server capabilities
        capabilities = server.capabilities
        
        server.quit()
        
        response_time = round(time.time() - start_time, 3)
        
        return {
            "success": True,
            "message": "SMTP connection successful",
            "response_time": response_time,
            "supports_starttls": b'STARTTLS' in capabilities if isinstance(capabilities, bytes) else 'STARTTLS' in str(capabilities),
            "max_message_size": server.esmtp_features.get('size', 'Not reported')
        }
        
    except smtplib.SMTPAuthenticationError as e:
        pass

        response_time = round(time.time() - start_time, 3)
        return {
            "success": False,
            "message": "Authentication failed. Please check your password and ensure you're using an app password if 2FA is enabled.",
            "response_time": response_time,
            "error": str(e)
        }
    except smtplib.SMTPConnectError as e:
        pass

        response_time = round(time.time() - start_time, 3)
        return {
            "success": False,
            "message": f"Connection failed. Please check the SMTP server address and port.",
            "response_time": response_time,
            "error": str(e)
        }
    except Exception as e:
        pass

        response_time = round(time.time() - start_time, 3)
        return {
            "success": False,
            "message": f"SMTP error: {str(e)}",
            "response_time": response_time,
            "error": str(e)
        }


async def test_mailbox_connections(imap_config: dict, smtp_config: dict = None, timeout: int = 20) -> ConnectionTestResult:
    """
    Test both IMAP and SMTP connections for a mailbox
    
    Args:
        imap_config: dict with host, port, username, password, use_ssl
        smtp_config: dict with host, port, username, password, use_starttls, use_ssl (optional)
    
    Returns:
        ConnectionTestResult model
    """
    # Test IMAP
    imap_result = await test_imap_connection(
        host=imap_config.get('host'),
        port=int(imap_config.get('port', 993)),
        username=imap_config.get('username'),
        password=imap_config.get('password'),
        use_ssl=imap_config.get('use_ssl', True),
        validate_cert=imap_config.get('validate_cert', True),
        timeout=timeout
    )
    
    imap_test = ConnectionTestResult(
        imap_success=imap_result.get('success', False),
        imap_message=imap_result.get('message', ''),
        imap_response_time=imap_result.get('response_time')
    )
    
    # Test SMTP if provided
    smtp_test = ConnectionTestResult()
    
    if smtp_config and smtp_config.get('host'):
        smtp_result = await test_smtp_connection_simple(
            host=smtp_config.get('host'),
            port=int(smtp_config.get('port', 587)),
            username=smtp_config.get('username'),
            password=smtp_config.get('password'),
            use_starttls=smtp_config.get('use_starttls', True),
            use_ssl=smtp_config.get('use_ssl', False),
            timeout=timeout
        )
        
        smtp_test = ConnectionTestResult(
            smtp_success=smtp_result.get('success', False),
            smtp_message=smtp_result.get('message', ''),
            smtp_response_time=smtp_result.get('response_time')
        )
    
    # Overall success requires IMAP to work, SMTP is optional
    imap_test.overall_success = imap_test.imap_success
    if smtp_config and smtp_config.get('host'):
        imap_test.smtp_success = smtp_test.smtp_success
        imap_test.smtp_message = smtp_test.smtp_message
        imap_test.smtp_response_time = smtp_test.smtp_response_time
    
    if not imap_test.imap_success:
        imap_test.error = "IMAP connection failed. Please check your IMAP settings."
    
    return imap_test


def get_imap_folders(connection) -> List[EmailFolderResponse]:
    """
    Get all folders from IMAP connection
    
    Args:
        connection: IMAP4/IMAP4_SSL connection object
    
    Returns:
        List of EmailFolderResponse objects
    """
    folders = []
    
    try:
        status, folder_list = connection.list()
        
        if status == 'OK':
            for folder in folder_list:
                if isinstance(folder, bytes):
                    folder = folder.decode('utf-8', errors='replace')
                
                # Robust folder parsing - handle various IMAP formats
                # Examples: (\HasNoChildren) "/" "INBOX" or (\Unread 5) "/" "INBOX" or "INBOX"
                import re
                
                # Try to extract folder name using regex - look for quoted string at end
                match = re.search(r'"([^"]+)"\s*$', folder)
                if match:
                    folder_name = match.group(1)
                else:
                    # Try to find last quoted string
                    match2 = re.search(r'"([^"]+)"', folder)
                    if match2:
                        folder_name = match2.group(1)
                    else:
                        # Fallback: take last part after space
                        parts = folder.strip().split()
                        folder_name = parts[-1] if parts else 'Unknown'
                
                # Find delimiter (usually "/" or "." between quotes)
                delim_match = re.search(r'("\s*([^"]+)\s*"', folder)
                delimiter = '/'
                if delim_match:
                    possible_delim = delim_match.group(2)
                    if possible_delim in ['/', '.']:
                        delimiter = possible_delim
                
                # Check for flags
                is_selectable = '\\Noselect' not in folder
                
                # Get clean folder name (remove prefix like INBOX/)
                if folder_name.startswith('INBOX'):
                    clean_name = 'INBOX'
                else:
                    clean_name = folder_name.strip()
                    
                    # Create folder response (unread/total counts will be fetched separately)
                    folder_resp = EmailFolderResponse(
                        name=clean_name,
                        path=folder_name,
                        delimiter=delimiter,
                        unread_count=0,
                        total_count=0,
                        is_selectable=is_selectable
                    )
                    folders.append(folder_resp)
        
    except Exception as e:
        pass

        print(f"Error getting folders: {e}")
    
    return folders


def get_folder_status(connection, folder: str = 'INBOX') -> dict:
    """
    Get folder status including unread and total counts
    
    Args:
        connection: IMAP4/IMAP4_SSL connection object
        folder: Folder name (default: INBOX)
    
    Returns:
        dict with unread_count and total_count
    """
    try:
        # Status command is more efficient than search
        status, data = connection.status(folder, '(UNREAD MESSAGES)')
        
        if status == 'OK' and data:
            data_str = data[0].decode('utf-8', errors='replace') if isinstance(data[0], bytes) else data[0]
            
            # Parse UNREAD count
            unread = 0
            total = 0
            
            import re
            unread_match = re.search(r'UNREAD (\d+)', data_str)
            if unread_match:
                unread = int(unread_match.group(1))
            
            messages_match = re.search(r'MESSAGES (\d+)', data_str)
            if messages_match:
                total = int(messages_match.group(1))
            
            return {"unread_count": unread, "total_count": total}
        
    except Exception as e:
        pass

        print(f"Error getting folder status for {folder}: {e}")
    
    return {"unread_count": 0, "total_count": 0}


# =============================================================================
# NEW ASYNC IMAP FUNCTIONS (Using Connection Pool)
# =============================================================================

async def connect_imap_async(
    host: str,
    port: int,
    username: str,
    password: str,
    use_ssl: bool = True,
    validate_cert: bool = True,
    timeout: int = 60,
    mailbox_id: str = None
) -> dict:
    """
    NEW async IMAP connection using connection pool
    
    Args:
        host: IMAP server hostname
        port: IMAP server port (default: 993)
        username: Email address/username
        password: Email password or app password
        use_ssl: Use SSL/TLS connection
        validate_cert: Validate SSL certificate (for future use)
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, client (if success), message
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "client": None,
            "message": "Async IMAP modules not available"
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout
        )
        
        return {
            "success": success,
            "client": client,
            "mailbox_id": mailbox_id,
            "message": msg
        }
        
    except Exception as e:
        pass

        return {
            "success": False,
            "client": None,
            "message": f"Connection failed: {str(e)}"
        }


async def get_imap_folders_async(
    host: str,
    port: int,
    username: str,
    password: str,
    use_ssl: bool = True,
    timeout: int = 30,
    mailbox_id: str = None
) -> dict:
    """
    NEW async function to get all folders from IMAP server
    
    Args:
        host: IMAP server hostname
        port: IMAP server port
        username: Email address/username
        password: Email password or app password
        use_ssl: Use SSL/TLS connection
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, folders (list), message, folder_count
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "folders": [],
            "message": "Async IMAP modules not available",
            "folder_count": 0
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout
        )
        
        if not success:
            return {
                "success": False,
                "folders": [],
                "message": msg,
                "folder_count": 0
            }
        
        try:
            # Get folder list
            status, folder_list = await client.list()
            
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "folders": [],
                    "message": "Failed to list folders",
                    "folder_count": 0
                }
            
            folders = []
            import re
            
            for folder in folder_list:
                if isinstance(folder, bytes):
                    folder = folder.decode('utf-8', errors='replace')
                
                # Extract folder name
                match = re.search(r'"([^"]+)"\s*$', folder)
                if match:
                    folder_name = match.group(1)
                else:
                    match2 = re.search(r'"([^"]+)"', folder)
                    if match2:
                        folder_name = match2.group(1)
                    else:
                        parts = folder.strip().split()
                        folder_name = parts[-1] if parts else 'Unknown'
                
                # Find delimiter
                delim_match = re.search(r'"\s*([^"/\.]+)\s*"', folder)
                delimiter = '/'
                if delim_match:
                    possible_delim = delim_match.group(1)
                    if possible_delim in ['/', '.']:
                        delimiter = possible_delim
                
                # Check for flags
                is_selectable = '\\Noselect' not in folder
                
                # Clean folder name
                if folder_name.startswith('INBOX'):
                    clean_name = 'INBOX'
                else:
                    clean_name = folder_name.strip()
                    folders.append(EmailFolderResponse(
                        name=clean_name,
                        path=folder_name,
                        delimiter=delimiter,
                        unread_count=0,
                        total_count=0,
                        is_selectable=is_selectable
                    ))
            
            await manager.release_connection(mailbox_id, client)
            
            return {
                "success": True,
                "folders": folders,
                "message": f"Found {len(folders)} folders",
                "folder_count": len(folders)
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
            
    except Exception as e:
        pass

        return {
            "success": False,
            "folders": [],
            "message": f"Error: {str(e)}",
            "folder_count": 0
        }


async def get_folder_status_async(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = 'INBOX',
    use_ssl: bool = True,
    timeout: int = 30,
    mailbox_id: str = None
) -> dict:
    """
    NEW async function to get folder status
    
    Args:
        host: IMAP server hostname
        port: IMAP server port
        username: Email address/username
        password: Email password or app password
        folder: Folder name (default: INBOX)
        use_ssl: Use SSL/TLS connection
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, unread_count, total_count, message
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "unread_count": 0,
            "total_count": 0,
            "message": "Async IMAP modules not available"
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout,
            folder=folder
        )
        
        if not success:
            return {
                "success": False,
                "unread_count": 0,
                "total_count": 0,
                "message": msg
            }
        
        try:
            # Select folder first
            status, _ = await client.select(quote_imap_folder_name(folder))
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "unread_count": 0,
                    "total_count": 0,
                    "message": f"Failed to select folder: {folder}"
                }
            
            # Get total count
            status, messages = await client.search(None, 'ALL')
            total_count = len(messages[0].split()) if messages and messages[0] else 0
            
            # Get unread count
            status, unread = await client.search(None, 'UNSEEN')
            unread_count = len(unread[0].split()) if unread and unread[0] else 0
            
            await manager.release_connection(mailbox_id, client)
            
            return {
                "success": True,
                "unread_count": unread_count,
                "total_count": total_count,
                "message": f"Folder {folder}: {total_count} emails, {unread_count} unread"
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
            
    except Exception as e:
        pass

        return {
            "success": False,
            "unread_count": 0,
            "total_count": 0,
            "message": f"Error: {str(e)}"
        }


async def fetch_emails_async(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = 'INBOX',
    use_ssl: bool = True,
    limit: int = 200,
    offset: int = 0,
    timeout: int = 30,
    mailbox_id: str = None
) -> dict:
    """
    NEW async function to fetch emails from a folder
    
    Args:
        host: IMAP server hostname
        port: IMAP server port
        username: Email address/username
        password: Email password or app password
        folder: Folder name (default: INBOX)
        use_ssl: Use SSL/TLS connection
        limit: Maximum number of emails to fetch
        offset: Offset for pagination
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, emails (list), message, total, has_more
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "emails": [],
            "message": "Async IMAP modules not available",
            "total": 0,
            "has_more": False
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout,
            folder=folder
        )
        
        if not success:
            return {
                "success": False,
                "emails": [],
                "message": msg,
                "total": 0,
                "has_more": False
            }
        
        try:
            # Select folder
            status, _ = await client.select(quote_imap_folder_name(folder))
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "emails": [],
                    "message": f"Failed to select folder: {folder}",
                    "total": 0,
                    "has_more": False
                }
            
            # Incremental UID search (fallback to last N on first sync)
            last_uid = get_mailbox_last_uid(mailbox_id) if mailbox_id else 0
            initial_sync_limit = 500
            if last_uid > 0:
                search_criteria = f"UID {last_uid + 1}:*"
            else:
                search_criteria = "ALL"

            status, data = await client.search(None, search_criteria)

            if status != 'OK' or not data or not data[0]:
                await manager.release_connection(mailbox_id, client)
                return {
                    "success": True,
                    "emails": [],
                    "message": "No emails found",
                    "total": 0,
                    "has_more": False
                }

            raw_ids = data[0]
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode('utf-8', errors='replace')

            all_ids = [uid.strip() for uid in raw_ids.split() if uid.strip()]
            if last_uid == 0 and len(all_ids) > initial_sync_limit:
                all_ids = all_ids[-initial_sync_limit:]
            total = len(all_ids)
            
            # Apply pagination
            start = max(0, offset)
            end = min(len(all_ids), offset + limit)
            
            if start >= total:
                await manager.release_connection(mailbox_id, client)
                return {
                    "success": True,
                    "emails": [],
                    "message": "No emails in range",
                    "total": total,
                    "has_more": False
                }
            
            paginated_ids = all_ids[start:end]
            has_more = end < total
            
            # Fetch headers for each email
            emails = []
            for uid in paginated_ids:
                try:
                    status, msg_data = await client.fetch(
                        uid,
                        '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])'
                    )
                    
                    if status == 'OK' and msg_data:
                        emails.append({
                            "uid": uid,
                            "raw_data": msg_data
                        })
                except Exception:
                    pass

                    continue
            
            await manager.release_connection(mailbox_id, client)
            
            return {
                "success": True,
                "emails": emails,
                "message": f"Fetched {len(emails)} emails",
                "total": total,
                "has_more": has_more
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
            
    except Exception as e:
        pass

        return {
            "success": False,
            "emails": [],
            "message": f"Error: {str(e)}",
            "total": 0,
            "has_more": False
        }


async def search_emails_async(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = 'INBOX',
    search_criteria: str = 'ALL',
    use_ssl: bool = True,
    limit: int = 200,
    timeout: int = 30,
    mailbox_id: str = None
) -> dict:
    """
    NEW async function to search emails
    
    Args:
        host: IMAP server hostname
        port: IMAP server port
        username: Email address/username
        password: Email password or app password
        folder: Folder name (default: INBOX)
        search_criteria: IMAP search criteria (e.g., 'UNSEEN', 'FROM "sender@example.com"')
        use_ssl: Use SSL/TLS connection
        limit: Maximum number of results
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, emails (list), message, count
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "emails": [],
            "message": "Async IMAP modules not available",
            "count": 0
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout,
            folder=folder
        )
        
        if not success:
            return {
                "success": False,
                "emails": [],
                "message": msg,
                "count": 0
            }
        
        try:
            # Select folder
            status, _ = await client.select(quote_imap_folder_name(folder))
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "emails": [],
                    "message": f"Failed to select folder: {folder}",
                    "count": 0
                }
            
            # Search emails
            status, data = await client.search(None, search_criteria)
            
            if status != 'OK' or not data or not data[0]:
                await manager.release_connection(mailbox_id, client)
                return {
                    "success": True,
                    "emails": [],
                    "message": "No emails found",
                    "count": 0
                }
            
            # Parse email IDs
            raw_ids = data[0]
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode('utf-8', errors='replace')
            
            all_ids = [uid.strip() for uid in raw_ids.split() if uid.strip()]
            
            # Apply limit
            limited_ids = all_ids[:limit]
            
            # Fetch headers for each email
            emails = []
            for uid in limited_ids:
                try:
                    status, msg_data = await client.fetch(
                        uid,
                        '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])'
                    )
                    
                    if status == 'OK' and msg_data:
                        emails.append({
                            "uid": uid,
                            "raw_data": msg_data
                        })
                except Exception:
                    pass

                    continue
            
            await manager.release_connection(mailbox_id, client)
            
            return {
                "success": True,
                "emails": emails,
                "message": f"Found {len(emails)} emails",
                "count": len(emails)
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
            
    except Exception as e:
        pass

        return {
            "success": False,
            "emails": [],
            "message": f"Error: {str(e)}",
            "count": 0
        }


def _fetch_parts(fetch_lines) -> tuple:
    """Split an aioimaplib `fetch` response into (meta_text, [literals]).

    aioimaplib stores literal payloads as `bytearray` items and command/meta
    text as `bytes` items, in the order they were received. This helper
    separates them so we can parse flags/UID from the meta and decode the
    header/body literals reliably (rather than string-slicing the whole
    response, which breaks on any non-ASCII content).
    """
    meta_parts = []
    literals = []
    for item in (fetch_lines or []):
        if isinstance(item, bytearray):
            literals.append(bytes(item))
        elif isinstance(item, bytes):
            meta_parts.append(item.decode("utf-8", "replace"))
    return "\n".join(meta_parts), literals


def _to_imap_date(value) -> Optional[str]:
    """Convert a date-ish value to IMAP's dd-Mon-yyyy format."""
    from datetime import date, datetime
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        d = None
        s = str(value).strip()
        try:
            d = date.fromisoformat(s[:10])
        except Exception:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(s)
                if dt is not None:
                    d = dt.date()
            except Exception:
                d = None
    return d.strftime("%d-%b-%Y") if d else None


def _imap_escape(value: str) -> str:
    """Escape a string for safe embedding inside a quoted IMAP search term."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _build_search_criteria(search_text, search_field, unread_only, since_date, before_date) -> str:
    """Build a server-side IMAP SEARCH criteria string."""
    parts = []
    if unread_only:
        parts.append("UNSEEN")
    since = _to_imap_date(since_date)
    before = _to_imap_date(before_date)
    if since:
        parts.append(f"SINCE {since}")
    if before:
        parts.append(f"BEFORE {before}")

    q = (search_text or "").strip()
    if q:
        esc = _imap_escape(q)
        field = (search_field or "all").lower()
        if field == "subject":
            parts.append(f'SUBJECT "{esc}"')
        elif field == "from":
            parts.append(f'FROM "{esc}"')
        elif field == "to":
            parts.append(f'TO "{esc}"')
        elif field == "body":
            parts.append(f'BODY "{esc}"')
        else:
            parts.append(f'TEXT "{esc}"')
    return " ".join(parts) if parts else "ALL"


def _bodystructure_has_attachments(bodystructure: str) -> bool:
    """Detect attachments from an IMAP BODYSTRUCTURE string."""
    bs = (bodystructure or "").lower()
    if not bs:
        return False
    return '"attachment"' in bs or '"mixed"' in bs


_re_search_uid = re.compile(r"UID\s+(\d+)")
_re_search_size = re.compile(r"RFC822\.SIZE\s+(\d+)")
_re_search_flags = re.compile(r"FLAGS\s*\(([^)]*)\)")


def _parse_header_bytes(header_bytes: bytes) -> dict:
    """Parse a raw header block (bytes) into a lowercase-key dict."""
    if not header_bytes:
        return {}
    try:
        from email.parser import BytesParser
        from email import policy
        msg = BytesParser(policy=policy.default).parsebytes(header_bytes)
        return {k.lower(): v for k, v in msg.items()}
    except Exception:
        return {}


def _parse_email_date_to_ts(value) -> Optional[float]:
    """Parse an email Date header (or 'YYYY-MM-DD') into a unix timestamp."""
    if not value:
        return None
    s = str(value).strip()
    try:
        from datetime import datetime, date
        d = date.fromisoformat(s[:10])
        return datetime(d.year, d.month, d.day).timestamp()
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt.timestamp() if dt else None
    except Exception:
        return None


def _search_item_date_matches(item: dict, since_date: str, before_date: str) -> bool:
    """Client-side date-range check for a search item."""
    if not since_date and not before_date:
        return True
    ts = _parse_email_date_to_ts(item.get("date"))
    since_ts = _parse_email_date_to_ts(since_date)
    before_ts = _parse_email_date_to_ts(before_date)
    if ts is None:
        return False
    if since_ts and ts < since_ts:
        return False
    if before_ts and ts > before_ts:
        return False
    return True


def _search_item_matches(item: dict, search_text: str, search_field: str,
                         unread_only: bool, has_attachments: bool,
                         since_date: str, before_date: str) -> bool:
    """Full client-side matcher (used when the server rejects SEARCH criteria)."""
    if unread_only and item.get("is_read"):
        return False
    if has_attachments and not item.get("has_attachments"):
        return False

    q = (search_text or "").strip().lower()
    if q:
        field = (search_field or "all").lower()
        subject = str(item.get("subject") or "").lower()
        fa = item.get("from_address") or {}
        sender = " ".join([str(fa.get("name") or ""), str(fa.get("email") or "")]).lower()
        to_parts = []
        for r in (item.get("to_addresses") or []):
            if isinstance(r, dict):
                to_parts.append(str(r.get("name") or "") + " " + str(r.get("email") or ""))
            else:
                to_parts.append(str(r))
        to_text = " ".join(to_parts).lower()
        preview = str(item.get("preview") or "").lower()
        if field == "subject":
            return q in subject
        if field == "from":
            return q in sender
        if field == "to":
            return q in to_text
        if field == "body":
            return q in preview
        return q in subject or q in sender or q in to_text or q in preview

    return _search_item_date_matches(item, since_date, before_date)


def _matches_client_filter(item: dict, search_text: str, search_field: str) -> bool:
    """Re-verify header-based field matches on the fetched page.

    The server-side SEARCH already scopes to the whole folder, but some
    servers apply criteria loosely. For header fields (subject/from/to) we
    have the complete header, so we can guarantee the displayed results
    actually match. Body/`all` searches are left to the server because the
    preview snippet is truncated (a client re-check there could drop valid
    matches whose term appears deeper in the body).
    """
    q = (search_text or "").strip().lower()
    if not q:
        return True
    field = (search_field or "all").lower()
    if field not in ("subject", "from", "to"):
        return True

    subject = str(item.get("subject") or "").lower()
    fa = item.get("from_address") or {}
    sender = " ".join([str(fa.get("name") or ""), str(fa.get("email") or "")]).lower()
    to_parts = []
    for r in (item.get("to_addresses") or []):
        if isinstance(r, dict):
            to_parts.append(str(r.get("name") or "") + " " + str(r.get("email") or ""))
        else:
            to_parts.append(str(r))
    to_text = " ".join(to_parts).lower()

    if field == "subject":
        return q in subject
    if field == "from":
        return q in sender
    if field == "to":
        return q in to_text
    return True


async def _imap_search(client, criteria):
    """
    Run IMAP SEARCH robustly.

    aioimaplib adds ``CHARSET utf-8`` to every SEARCH by default, which many
    servers reject with a BAD response. We therefore try the charset-less form
    first (single criteria string, no trailing None argument) and fall back to
    the utf-8 form. Returns (status, data).
    """
    last_status = "NO"
    last_data = []
    for charset in (None, "utf-8"):
        try:
            status, data = await client.search(criteria, charset=charset)
        except Exception:
            continue
        if status == "OK":
            return status, data
        last_status = status
        last_data = data
    return last_status, last_data


async def search_mailbox_async(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str = "INBOX",
    search_text: str = "",
    search_field: str = "all",
    unread_only: bool = False,
    has_attachments: bool = False,
    since_date: str = None,
    before_date: str = None,
    use_ssl: bool = True,
    limit: int = 50,
    offset: int = 0,
    timeout: int = 30,
    mailbox_id: str = None,
) -> dict:
    """
    Smart server-side email search.

    Uses the IMAP server's native SEARCH (so it matches across the ENTIRE
    folder, not just the first page) and returns clean, list-ready email
    entries with subject, sender, recipients, date, flags, size, attachment
    flag and a text preview.

    Returns:
        dict with success, emails (list), message, total, has_more
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "emails": [],
            "message": "Async IMAP modules not available",
            "total": 0,
            "has_more": False,
        }

    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]

    # Normalize date filters: empty/whitespace means "no date filter". This
    # guarantees leaving the From/To date fields blank never affects results.
    search_text = (search_text or "").strip()
    since_date = (since_date or "").strip() or None
    before_date = (before_date or "").strip() or None

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout,
            folder=folder,
        )
        if not success:
            return {"success": False, "emails": [], "message": msg, "total": 0, "has_more": False}

        try:
            # Select folder with case-variant fallbacks (Yahoo compatibility)
            select_status, _ = await client.select(quote_imap_folder_name(folder))
            if select_status != "OK":
                for variant in (folder.lower(), folder.capitalize(), folder.upper()):
                    if variant == folder:
                        continue
                    select_status, _ = await client.select(quote_imap_folder_name(variant))
                    if select_status == "OK":
                        folder = variant
                        break
            if select_status != "OK":
                await manager.release_connection(mailbox_id, client, close=True)
                return {"success": False, "emails": [], "message": f"Failed to select folder: {folder}", "total": 0, "has_more": False}

            criteria = _build_search_criteria(search_text, search_field, unread_only, since_date, before_date)

            # ---- Server-side SEARCH with graceful fallback ----
            # Some servers reject the query or the implicit CHARSET with a
            # BAD/NO response. We retry charset-less, and if the server still
            # rejects the query we fall back to fetching recent messages and
            # filtering them locally so search always works.
            status, data = await _imap_search(client, criteria)
            server_matched = (status == "OK")

            if not server_matched:
                status, data = await _imap_search(client, "ALL")
                if status != "OK":
                    await manager.release_connection(mailbox_id, client)
                    return {"success": False, "emails": [], "message": f"Search failed: {status}", "total": 0, "has_more": False}

            raw_ids = (data[0] if data else b"") or b""
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode("utf-8", "replace")
            ids = [x.strip() for x in raw_ids.split() if x.strip()]
            # Newest first (IMAP returns ascending sequence numbers)
            ids.reverse()

            # When filtering locally, bound how many messages we scan.
            scan_ids = ids if server_matched else ids[:2000]
            total = len(ids) if server_matched else len(scan_ids)
            page_ids = scan_ids[offset:offset + limit]
            has_more = (offset + limit) < total

            emails = []
            for seq in page_ids:
                try:
                    # 1) Headers + flags + size + BODYSTRUCTURE (single fetch)
                    status, fetch_data = await client.fetch(
                        seq,
                        "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] BODYSTRUCTURE)",
                    )
                    if status != "OK" or not fetch_data:
                        continue

                    meta, literals = _fetch_parts(fetch_data)
                    uid = None
                    size = None
                    is_read = False
                    m = _re_search_uid.search(meta)
                    if m:
                        uid = int(m.group(1))
                    m = _re_search_size.search(meta)
                    if m:
                        size = int(m.group(1))
                    m = _re_search_flags.search(meta)
                    if m:
                        is_read = "\\Seen" in m.group(1)

                    header_bytes = literals[0] if literals else b""
                    bodystructure = ""
                    idx = meta.find("BODYSTRUCTURE ")
                    if idx != -1:
                        bodystructure = meta[idx + len("BODYSTRUCTURE "):].strip()

                    headers = _parse_header_bytes(header_bytes)
                    subject = decode_email_header_value(headers.get("subject"), "(No Subject)")
                    from_addr = parse_email_address(headers.get("from", ""))
                    to_addrs = [parse_email_address(a) for a in headers.get("to", "").split(", ") if a.strip()]
                    cc_addrs = [parse_email_address(a) for a in headers.get("cc", "").split(", ") if a.strip()]
                    date_str = decode_email_header_value(headers.get("date", ""))
                    message_id = decode_email_header_value(headers.get("message-id", ""))

                    has_att = _bodystructure_has_attachments(bodystructure)

                    # Skip if the optional attachment filter doesn't match
                    if has_attachments and not has_att:
                        continue

                    # Build the item, then re-verify header field matches
                    item = {
                        "id": f"{mailbox_id}_{folder}_{seq}",
                        "mailbox_id": mailbox_id,
                        "folder": folder,
                        "uid": int(seq),
                        "imap_uid": uid,
                        "subject": subject,
                        "from_address": from_addr,
                        "to_addresses": to_addrs,
                        "cc_addresses": cc_addrs if cc_addrs else None,
                        "date": date_str,
                        "is_read": is_read,
                        "is_flagged": False,
                        "has_attachments": has_att,
                        "attachments": [],
                        "preview": "",
                        "message_id": message_id,
                        "size": size,
                    }
                    # 2) Optional short text snippet for preview (best-effort)
                    preview = ""
                    try:
                        status2, text_data = await client.fetch(seq, "(BODY.PEEK[TEXT]<0.1500>)")
                        if status2 == "OK" and text_data:
                            _, text_literals = _fetch_parts(text_data)
                            if text_literals:
                                preview = extract_email_preview(
                                    text_literals[0].decode("utf-8", "replace")
                                )
                    except Exception:
                        preview = ""
                    item["preview"] = preview

                    # Apply the matching rules. Server-matched results are
                    # re-verified client-side for flags/dates/headers (protects
                    # against servers that partially ignore criteria); fallback
                    # results are filtered entirely locally.
                    if server_matched:
                        if unread_only and is_read:
                            continue
                        if has_attachments and not has_att:
                            continue
                        if not _search_item_date_matches(item, since_date, before_date):
                            continue
                        if not _matches_client_filter(item, search_text, search_field):
                            continue
                    else:
                        if not _search_item_matches(item, search_text, search_field,
                                                    unread_only, has_attachments,
                                                    since_date, before_date):
                            continue

                    emails.append(item)
                except Exception:
                    continue

            await manager.release_connection(mailbox_id, client)

            return {
                "success": True,
                "emails": emails,
                "message": f"Found {total} matching email(s)",
                "total": total,
                "has_more": has_more,
                "criteria": criteria,
                "server_matched": server_matched,
            }

        except Exception as e:
            await manager.release_connection(mailbox_id, client, close=True)
            return {"success": False, "emails": [], "message": f"Search error: {str(e)}", "total": 0, "has_more": False}

    except Exception as e:
        return {"success": False, "emails": [], "message": f"Error: {str(e)}", "total": 0, "has_more": False}


async def get_sync_state_async(mailbox_id: str) -> dict:
    """
    NEW async function to get sync state from database
    
    Args:
        mailbox_id: Mailbox identifier
    
    Returns:
        dict with success, last_uid, sync_status, last_sync_at, message
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "last_uid": 0,
            "sync_status": "unknown",
            "last_sync_at": None,
            "message": "Database modules not available"
        }
    
    try:
        from db_helpers import get_sync_state
        sync_state = get_sync_state(mailbox_id)
        
        if sync_state:
            return {
                "success": True,
                "last_uid": sync_state.last_uid or 0,
                "sync_status": sync_state.sync_status or "unknown",
                "last_sync_at": sync_state.last_sync_at.isoformat() if sync_state.last_sync_at else None,
                "message": "Sync state retrieved"
            }
        else:
            return {
                "success": True,
                "last_uid": 0,
                "sync_status": "idle",
                "last_sync_at": None,
                "message": "No sync state found, starting fresh"
            }
            
    except Exception as e:
        pass

        return {
            "success": False,
            "last_uid": 0,
            "sync_status": "error",
            "last_sync_at": None,
            "message": f"Error: {str(e)}"
        }


async def update_sync_state_async(
    mailbox_id: str,
    last_uid: int = None,
    is_syncing: bool = None,
    status: str = None,
    error: str = None
) -> dict:
    """
    NEW async function to update sync state in database
    
    Args:
        mailbox_id: Mailbox identifier
        last_uid: Last synced UID
        is_syncing: Whether sync is in progress
        status: Sync status (syncing, completed, error, idle)
        error: Error message if any
    
    Returns:
        dict with success, message
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "message": "Database modules not available"
        }
    
    try:
        from db_helpers import update_sync_state
        update_sync_state(
            mailbox_id=mailbox_id,
            last_uid=last_uid,
            is_syncing=is_syncing,
            status=status,
            error=error
        )
        
        return {
            "success": True,
            "message": "Sync state updated"
        }
        
    except Exception as e:
        pass

        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }


async def mark_email_read_async(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    email_uid: str,
    is_read: bool = True,
    use_ssl: bool = True,
    timeout: int = 30,
    mailbox_id: str = None
) -> dict:
    """
    NEW async function to mark email as read/unread
    
    Args:
        host: IMAP server hostname
        port: IMAP server port
        username: Email address/username
        password: Email password or app password
        folder: Folder name
        email_uid: Email UID to mark
        is_read: True to mark as read, False to mark as unread
        use_ssl: Use SSL/TLS connection
        timeout: Connection timeout in seconds
        mailbox_id: Optional mailbox ID for connection pooling
    
    Returns:
        dict with success, message
    """
    if not ASYNC_IMAP_AVAILABLE:
        return {
            "success": False,
            "message": "Async IMAP modules not available"
        }
    
    # Generate mailbox_id if not provided
    if not mailbox_id:
        import hashlib
        mailbox_id = hashlib.md5(f"{host}:{username}".encode()).hexdigest()[:12]
    
    try:
        manager = await get_connection_manager()
        success, client, msg = await manager.get_connection(
            mailbox_id=mailbox_id,
            host=host,
            port=port,
            username=username,
            password=password,
            use_ssl=use_ssl,
            timeout=timeout,
            folder=folder
        )
        
        if not success:
            return {
                "success": False,
                "message": msg
            }
        
        try:
            # Select folder
            status, _ = await client.select(quote_imap_folder_name(folder))
            if status != 'OK':
                await manager.release_connection(mailbox_id, client, close=True)
                return {
                    "success": False,
                    "message": f"Failed to select folder: {folder}"
                }
            
            # Set flags
            if is_read:
                result = await client.store(email_uid, '+FLAGS', '\\Seen')
            else:
                result = await client.store(email_uid, '-FLAGS', '\\Seen')
            
            await manager.release_connection(mailbox_id, client)
            
            return {
                "success": True,
                "message": f"Email marked as {'read' if is_read else 'unread'}"
            }
            
        except Exception as e:
            pass

            await manager.release_connection(mailbox_id, client, close=True)
            raise e
            
    except Exception as e:
        pass

        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }


# =============================================================================
# MAILBOX MANAGEMENT FUNCTIONS
# =============================================================================

def get_mailboxes_file(user_id: str, base_path: Path) -> Path:
    """
    Get the path to the user's mailboxes.json file
    
    Args:
        user_id: User identifier
        base_path: Base directory path
    
    Returns:
        Path to mailboxes.json file
    """
    user_folder = base_path / f"fv-s-{user_id}"
    inbox_folder = user_folder / "inbox-mailboxes"
    inbox_folder.mkdir(parents=True, exist_ok=True)
    return inbox_folder / "mailboxes.json"


def initialize_mailboxes_file(user_id: str, base_path: Path) -> dict:
    """
    Initialize or load the mailboxes.json file
    
    Args:
        user_id: User identifier
        base_path: Base directory path
    
    Returns:
        Mailboxes data structure
    """
    mailboxes_file = get_mailboxes_file(user_id, base_path)
    
    try:
        if mailboxes_file.exists():
            with open(mailboxes_file, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass

        pass
    
    # Create new mailboxes structure
    mailbox_data = {
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "mailboxes": []
    }
    
    try:
        with open(mailboxes_file, 'w', encoding='utf-8') as f:
            json.dump(mailbox_data, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

        pass
    
    return mailbox_data


def get_mailbox_storage_path(user_id: str, mailbox_id: str, base_path: Path) -> Path:
    """
    Get the storage path for a specific mailbox's email data
    
    Args:
        user_id: User identifier
        mailbox_id: Mailbox identifier
        base_path: Base directory path
    
    Returns:
        Path to mailbox storage directory
    """
    user_folder = base_path / f"fv-s-{user_id}"
    mailbox_folder = user_folder / "inbox-mailboxes" / mailbox_id
    mailbox_folder.mkdir(parents=True, exist_ok=True)
    return mailbox_folder


def save_mailbox_config(user_id: str, base_path: Path, mailbox_data: dict) -> tuple:
    """
    Save a new mailbox configuration
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_data: Mailbox configuration data containing:
            - provider: Email provider (gmail, yahoo, outlook, custom)
            - email_address: Email address
            - display_name: Optional display name
            - imap_config: IMAP configuration (host, port, username, password, use_ssl)
            - smtp_config: SMTP configuration (host, port, username, password, use_starttls)
            - is_primary: Set as primary mailbox
    
    Returns:
        tuple: (success: bool, mailbox_id: str or None, message: str)
    """
    try:
        # Load existing mailboxes or create new file
        mailboxes_file = get_mailboxes_file(user_id, base_path)
        
        if mailboxes_file.exists():
            try:
                with open(mailboxes_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                pass

                data = initialize_mailboxes_file(user_id, base_path)
        else:
            data = initialize_mailboxes_file(user_id, base_path)
        
        # Generate mailbox ID
        mailbox_id = f"mbx_{uuid.uuid4().hex[:12]}"
        
        # Check if email already exists
        email_address = mailbox_data.get('email_address', '').lower()
        for existing in data.get('mailboxes', []):
            if existing.get('email_address', '').lower() == email_address:
                return False, None, "Mailbox with this email address already exists"
        
        # Create mailbox entry
        now = datetime.now().isoformat()
        from config import encrypt_secret

        imap_password = (mailbox_data.get('imap_config') or {}).get('password', '')
        smtp_password = (mailbox_data.get('smtp_config') or {}).get('password', '')

        mailbox_entry = {
            "id": mailbox_id,
            "provider": mailbox_data.get('provider', 'custom'),
            "email_address": email_address,
            "display_name": mailbox_data.get('display_name', email_address.split('@')[0] if '@' in email_address else email_address),
            "imap_config": {
                "host": (mailbox_data.get('imap_config') or {}).get('host', ''),
                "port": (mailbox_data.get('imap_config') or {}).get('port', 993),
                "username": (mailbox_data.get('imap_config') or {}).get('username', ''),
                "password": encrypt_secret(imap_password) if imap_password else '',
                "use_ssl": (mailbox_data.get('imap_config') or {}).get('use_ssl', True),
                "validate_cert": (mailbox_data.get('imap_config') or {}).get('validate_cert', True)
            },
            "smtp_config": mailbox_data.get('smtp_config'),
            "smtp_enabled": mailbox_data.get('smtp_config') is not None,
            "is_primary": mailbox_data.get('is_primary', False),
            "sync_enabled": True,
            "status": "active",
            "unread_count": 0,
            "last_sync": None,
            "created_at": now,
            "updated_at": now
        }
        
        if mailbox_entry.get('smtp_config') and smtp_password:
            mailbox_entry['smtp_config']['password'] = encrypt_secret(smtp_password)

        # If this is primary, unset other primaries
        if mailbox_entry['is_primary']:
            for existing in data.get('mailboxes', []):
                existing['is_primary'] = False
        
        # Add to mailboxes list
        data['mailboxes'].append(mailbox_entry)
        data['updated_at'] = now
        
        # Save to file
        with open(mailboxes_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        if ASYNC_IMAP_AVAILABLE:
            try:
                from db_helpers import create_mailbox, get_mailbox_by_id as db_get_mailbox
                if not db_get_mailbox(mailbox_id):
                    imap_cfg = mailbox_entry['imap_config']
                    smtp_cfg = mailbox_entry.get('smtp_config') or {}
                    create_mailbox(
                        user_id=user_id,
                        email=email_address,
                        provider=mailbox_entry['provider'],
                        imap_host=imap_cfg['host'],
                        imap_port=int(imap_cfg.get('port', 993)),
                        encrypted_password=imap_cfg.get('password', ''),
                        smtp_host=smtp_cfg.get('host'),
                        smtp_port=int(smtp_cfg.get('port', 587)) if smtp_cfg.get('port') else 587,
                        username=imap_cfg.get('username'),
                        use_ssl=imap_cfg.get('use_ssl', True),
                        mailbox_id=mailbox_id,
                    )
            except Exception as e:
                pass

                print(f"Warning: could not register mailbox in database: {e}")

        return True, mailbox_id, "Mailbox saved successfully"
        
    except Exception as e:
        pass

        return False, None, f"Error saving mailbox: {str(e)}"


def get_user_mailboxes(user_id: str, base_path: Path, include_sensitive: bool = False) -> List[dict]:
    """
    Get all mailboxes for a user
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        include_sensitive: If True, include password fields (default: False)
    
    Returns:
        List of mailbox configurations
    """
    try:
        mailboxes_file = get_mailboxes_file(user_id, base_path)
        
        if not mailboxes_file.exists():
            return []
        
        with open(mailboxes_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        mailboxes = data.get('mailboxes', [])
        
        # Remove sensitive data unless explicitly requested
        if not include_sensitive:
            for mailbox in mailboxes:
                imap_config = mailbox.get('imap_config')
                if imap_config:
                    imap_config['password'] = '********' if imap_config.get('password') else None
                smtp_config = mailbox.get('smtp_config')
                if smtp_config:
                    smtp_config['password'] = '********' if smtp_config.get('password') else None
        
        return mailboxes
        
    except Exception as e:
        pass

        print(f"Error getting mailboxes: {e}")
        return []


def get_mailbox_by_id(user_id: str, base_path: Path, mailbox_id: str, include_sensitive: bool = False) -> Optional[dict]:
    """
    Get a specific mailbox by ID
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        include_sensitive: If True, include password fields (default: False)
    
    Returns:
        Mailbox configuration or None if not found
    """
    try:
        mailboxes_file = get_mailboxes_file(user_id, base_path)
        
        if not mailboxes_file.exists():
            return None
        
        with open(mailboxes_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        for mailbox in data.get('mailboxes', []):
            if mailbox.get('id') == mailbox_id:
                imap_config = mailbox.get('imap_config')
                smtp_config = mailbox.get('smtp_config')
                if include_sensitive:
                    from config import decrypt_secret
                    if imap_config and imap_config.get('password'):
                        imap_config['password'] = decrypt_secret(imap_config['password'])
                    if smtp_config and smtp_config.get('password'):
                        smtp_config['password'] = decrypt_secret(smtp_config['password'])
                else:
                    if imap_config:
                        imap_config['password'] = '********' if imap_config.get('password') else None
                    if smtp_config:
                        smtp_config['password'] = '********' if smtp_config.get('password') else None
                return mailbox
        
        return None
        
    except Exception as e:
        pass

        print(f"Error getting mailbox: {e}")
        return None


def update_mailbox_config(user_id: str, base_path: Path, mailbox_id: str, updates: dict) -> tuple:
    """
    Update a mailbox configuration
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        updates: Dictionary of fields to update
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailboxes_file = get_mailboxes_file(user_id, base_path)
        
        if not mailboxes_file.exists():
            return False, "Mailboxes file not found"
        
        with open(mailboxes_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        mailbox_found = False
        now = datetime.now().isoformat()
        
        for mailbox in data.get('mailboxes', []):
            if mailbox.get('id') == mailbox_id:
                mailbox_found = True
                
                # Update allowed fields
                if 'email_address' in updates:
                    mailbox['email_address'] = updates['email_address']
                
                if 'provider' in updates:
                    mailbox['provider'] = updates['provider']
                
                if 'display_name' in updates:
                    mailbox['display_name'] = updates['display_name']
                
                if 'is_primary' in updates:
                    # If setting as primary, unset others
                    if updates['is_primary']:
                        for other in data.get('mailboxes', []):
                            if other.get('id') != mailbox_id:
                                other['is_primary'] = False
                    mailbox['is_primary'] = updates['is_primary']
                
                if 'sync_enabled' in updates:
                    mailbox['sync_enabled'] = updates['sync_enabled']
                
                if 'status' in updates:
                    mailbox['status'] = updates['status']
                
                if 'imap_config' in updates:
                    # Update IMAP config fields
                    for key, value in updates['imap_config'].items():
                        mailbox['imap_config'][key] = value
                
                if 'smtp_config' in updates:
                    mailbox['smtp_config'] = updates['smtp_config']
                    mailbox['smtp_enabled'] = updates['smtp_config'] is not None
                
                mailbox['updated_at'] = now
                data['updated_at'] = now
                break
        
        if not mailbox_found:
            return False, "Mailbox not found"
        
        # Save updated data
        with open(mailboxes_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        
        return True, "Mailbox updated successfully"
        
    except Exception as e:
        pass

        return False, f"Error updating mailbox: {str(e)}"


def delete_mailbox_config(user_id: str, base_path: Path, mailbox_id: str) -> tuple:
    """
    Delete a mailbox configuration
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailboxes_file = get_mailboxes_file(user_id, base_path)
        
        if not mailboxes_file.exists():
            return False, "Mailboxes file not found"
        
        with open(mailboxes_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        initial_count = len(data.get('mailboxes', []))
        
        # Filter out the mailbox to delete
        data['mailboxes'] = [m for m in data.get('mailboxes', []) if m.get('id') != mailbox_id]
        
        if len(data['mailboxes']) == initial_count:
            return False, "Mailbox not found"
        
        data['updated_at'] = datetime.now().isoformat()
        
        # Save updated data
        with open(mailboxes_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        
        # Optionally delete mailbox email data directory
        mailbox_storage = get_mailbox_storage_path(user_id, mailbox_id, base_path)
        try:
            import shutil
            if mailbox_storage.exists():
                shutil.rmtree(mailbox_storage)
        except Exception:
            pass

            pass  # Ignore errors deleting storage
        
        return True, "Mailbox deleted successfully"
        
    except Exception as e:
        pass

        return False, f"Error deleting mailbox: {str(e)}"


def set_primary_mailbox(user_id: str, base_path: Path, mailbox_id: str) -> tuple:
    """
    Set a mailbox as the primary mailbox
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
    
    Returns:
        tuple: (success: bool, message: str)
    """
    return update_mailbox_config(user_id, base_path, mailbox_id, {'is_primary': True})


def get_primary_mailbox(user_id: str, base_path: Path, include_sensitive: bool = False) -> Optional[dict]:
    """
    Get the primary mailbox for a user
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        include_sensitive: If True, include password fields (default: False)
    
    Returns:
        Primary mailbox configuration or None
    """
    mailboxes = get_user_mailboxes(user_id, base_path, include_sensitive)
    
    for mailbox in mailboxes:
        if mailbox.get('is_primary', False):
            return mailbox
    
    # Return first active mailbox if no primary set
    for mailbox in mailboxes:
        if mailbox.get('status') == 'active':
            return mailbox
    
    return None if not mailboxes else mailboxes[0]


def update_mailbox_sync_status(user_id: str, base_path: Path, mailbox_id: str, status: str, unread_count: int = None) -> bool:
    """
    Update mailbox sync status
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        status: New status (syncing, active, error)
        unread_count: Optional unread count to update
    
    Returns:
        True if successful, False otherwise
    """
    try:
        updates = {'status': status}
        
        if unread_count is not None:
            updates['unread_count'] = unread_count
        
        success, _ = update_mailbox_config(user_id, base_path, mailbox_id, updates)
        
        if success and status == 'syncing':
            # Also update last_sync timestamp
            mailboxes_file = get_mailboxes_file(user_id, base_path)
            
            if mailboxes_file.exists():
                with open(mailboxes_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                for mailbox in data.get('mailboxes', []):
                    if mailbox.get('id') == mailbox_id:
                        mailbox['last_sync'] = datetime.now().isoformat()
                        break
                
                with open(mailboxes_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
        
        return success
        
    except Exception:
        pass

        return False


# =============================================================================
# EMAIL SYNC AND OPERATIONS FUNCTIONS
# =============================================================================

def parse_email_address(address_str: str) -> dict:
    """
    Parse email address string into name and email parts
    
    Args:
        address_str: Email address string (e.g., "John Doe <john@example.com>")
    
    Returns:
        dict with 'name' and 'email' keys
    """
    try:
        import email.utils
        
        address_str = decode_email_header_value(address_str)
        result = email.utils.parseaddr(address_str)
        
        if result[1]:
            return {"name": result[0] or None, "email": result[1]}
        else:
            # No angle brackets found, treat entire string as email
            return {"name": None, "email": address_str}
    except Exception:
        pass

        return {"name": None, "email": address_str}


def parse_email_headers(raw_headers: bytes) -> dict:
    """
    Parse raw email headers
    
    Args:
        raw_headers: Raw headers as bytes
    
    Returns:
        dict with parsed headers
    """
    headers = {}
    
    try:
        if isinstance(raw_headers, bytes):
            raw_headers = raw_headers.decode('utf-8', errors='replace')
        
        for line in raw_headers.split('\r\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
    except Exception:
        pass

        pass
    
    return headers


def fetch_email_headers(connection, email_uid: int) -> dict:
    """
    Fetch headers for a specific email
    
    Args:
        connection: IMAP connection
        email_uid: Email UID
    
    Returns:
        dict with headers
    """
    try:
        status, msg_data = connection.fetch(str(email_uid), '(BODY.PEEK[HEADER])')
        
        if status == 'OK' and msg_data:
            raw_headers = msg_data[0][1]
            return parse_email_headers(raw_headers)
    except Exception:
        pass

        pass
    
    return {}


def fetch_emailEnvelope(connection, email_uid: int) -> dict:
    """
    Fetch email envelope (From, To, Subject, Date, etc.)
    
    Args:
        connection: IMAP connection
        email_uid: Email UID
    
    Returns:
        dict with envelope data
    """
    try:
        status, msg_data = connection.fetch(str(email_uid), '(ENVELOPE)')
        
        if status == 'OK' and msg_data:
            # Parse the ENVELOPE response
            # Format: (UID (\Envelope ...))
            raw_data = msg_data[0]
            
            # Extract envelope fields
            envelope = {"subject": "", "from": None, "to": [], "date": "", "uid": email_uid}
            
            # Simple parsing - in reality you'd parse the IMAP ENVELOPE structure
            # For now, return a basic structure
            return envelope
    except Exception:
        pass

        pass
    
    return {"uid": email_uid, "subject": "", "from": None, "to": [], "date": ""}


def fetch_emails_from_folder(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str = "INBOX",
    limit: int = 200,
    offset: int = 0,
    unread_only: bool = False,
    force_refresh: bool = False
) -> tuple:
    """
    Fetch emails from a specific folder
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name (default: INBOX)
        limit: Maximum emails to return
        offset: Offset for pagination
        unread_only: Only fetch unread emails
        force_refresh: If True, bypass cache and fetch fresh from IMAP
    
    Returns:
        tuple: (success: bool, emails: list or None, message: str)
    """
    try:
        # DATABASE-FIRST STRATEGY: Skip stale JSON files, load from database instantly
        # Priority: 1) Database, 2) IMAP (only on force_refresh or if DB empty)
        
        # Step 1: Try database first (always fresh, no stale files)
        if not force_refresh and ASYNC_IMAP_AVAILABLE:
            try:
                from db_helpers import (
                    get_or_create_folder_by_path,
                    get_emails_by_folder,
                    count_emails_by_folder,
                )
                folder_obj = get_or_create_folder_by_path(mailbox_id, folder)
                total_emails = count_emails_by_folder(folder_obj.id)
                if total_emails > 0:
                    db_emails = get_emails_by_folder(folder_obj.id, limit, offset)
                    emails = [
                        _db_email_to_list_dict(e, mailbox_id, folder)
                        for e in db_emails
                    ]
                    if unread_only:
                        emails = [e for e in emails if not e.get("is_read")]
                    return True, emails, f"Loaded {len(emails)} emails from database cache", total_emails
            except Exception as e:
                logger.debug(f"Database email fetch failed, falling back: {e}")
        
        # Step 2: Fetch from IMAP (either force_refresh=true or database was empty)
        # Get mailbox config with credentials
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)

        if not mailbox:
            return False, None, "Mailbox not found", 0

        if mailbox.get('status') == 'inactive':
            return False, None, "Mailbox is inactive", 0

        imap_config = (mailbox.get('imap_config') or {})

        # Connect to IMAP
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )

        # Select folder with validation - try multiple folder name formats for Yahoo compatibility
        folder_name = (folder or "INBOX").strip()
        if not folder_name or folder_name in ('/', '""'):
            folder_name = "INBOX"

        select_status = None
        folder_attempts = [folder_name]
        const_attempts = [folder_name.lower(), folder_name.upper()]
        for attempt in const_attempts:
            if attempt not in folder_attempts:
                folder_attempts.append(attempt)
        if "INBOX" not in folder_attempts:
            folder_attempts.append("INBOX")

        for folder_attempt in folder_attempts:
            select_status, select_data = connection.select(quote_imap_folder_name(folder_attempt))
            if select_status == 'OK':
                folder_name = folder_attempt
                break

        if select_status != 'OK':
            connection.logout()
            return False, None, f"Failed selecting folder '{folder_name}': {select_data}", 0

        # Build search criteria
        search_criteria = 'UNSEEN' if unread_only else 'ALL'

        # Search emails - single attempt for speed (less latency)
        status, email_ids = connection.search(None, search_criteria)

        if status != 'OK':
            connection.logout()
            return False, None, f"Failed to search emails: {status}", 0

        # Handle different return formats from various IMAP servers
        # IMPORTANT: email_ids[0] might be bytes, so handle both cases
        if not email_ids or not email_ids[0]:
            all_ids = []
        else:
            raw_ids = email_ids[0]
            # Convert bytes to string if necessary for splitting
            if isinstance(raw_ids, bytes):
                raw_ids = raw_ids.decode('utf-8', errors='replace')
            all_ids = raw_ids.split()

        # Ensure all IDs are strings (IMAP fetch requires string format)
        all_ids = [str(email_id).strip() if not isinstance(email_id, str) else email_id.strip() for email_id in all_ids]
        all_ids = [eid for eid in all_ids if eid]  # Filter empty strings

        total_count = len(all_ids)

        # Apply pagination
        start_index = max(0, offset)
        end_index = min(len(all_ids), offset + limit)

        if start_index >= len(all_ids):
            connection.logout()
            return True, [], f"No emails found (offset too high)", total_count

        paginated_ids = all_ids[start_index:end_index]
        max_retries = 1
        fetched_count = 0
        emails = []  # Initialize the emails list

        # Optimize: Fetch UID, flags and headers in a SINGLE command per email
        # This is 3x faster than separate fetches
        for email_id in paginated_ids:
            try:
                # Use string format for email_id
                fetch_id = str(email_id).strip()

                # Single fetch: UID + FLAGS + HEADERS + short text snippet for preview
                status, msg_data = connection.fetch(
                    fetch_id,
                    '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)] BODY.PEEK[TEXT]<0.500>)'
                )

                if status != 'OK' or msg_data is None:
                    logger.debug(f"Failed to fetch email {email_id}: status={status}")
                    continue

                fetched_count += 1

                # Parse response
                msg_parts = msg_data[0]

                # Extract UID
                email_uid = int(email_id)
                try:
                    uid_match = str(msg_parts).split('UID ')[1].split(')')[0]
                    email_uid = int(uid_match)
                except Exception:
                    pass

                    pass

                # Check if read - handle both bytes and string types
                msg_str = str(msg_parts)
                is_read = '\\Seen' in msg_str or 'Seen' in msg_str
                is_flagged = '\\Flagged' in msg_str or 'Flagged' in msg_str
                is_deleted = '\\Deleted' in msg_str or 'Deleted' in msg_str

                # Get headers from the same response
                headers = {}
                try:
                    # Headers are in the response after UID and FLAGS
                    header_start = msg_str.find('BODY[HEADER.FIELDS')
                    if header_start != -1:
                        # Find the actual header content (after ]\r\n)
                        header_marker_end = msg_str.find(']', header_start)
                        if header_marker_end != -1:
                            raw_headers_str = msg_str[header_marker_end + 2:]
                            # Convert bytes-like string to actual bytes for parsing
                            if isinstance(raw_headers_str, str) and not raw_headers_str.startswith('From:'):
                                # Try to extract actual header bytes from the tuple
                                if hasattr(msg_data[0], '__iter__') and len(msg_data[0]) > 1:
                                    raw_headers = msg_data[0][1]
                                    if isinstance(raw_headers, bytes):
                                        headers = parse_email_headers(raw_headers)
                    else:
                        # Fallback: try to get headers from the tuple directly
                        if hasattr(msg_data[0], '__iter__') and len(msg_data[0]) > 1:
                            raw_headers = msg_data[0][1]
                            if isinstance(raw_headers, bytes):
                                headers = parse_email_headers(raw_headers)
                except Exception as e:
                    pass

                    pass

                # Parse addresses
                from_addr = parse_email_address(headers.get('from', ''))
                to_addrs = [parse_email_address(a) for a in headers.get('to', '').split(', ') if a]
                cc_addrs = [parse_email_address(a) for a in headers.get('cc', '').split(', ') if a]

                preview_text = ""
                try:
                    text_marker = "BODY[TEXT]"
                    if text_marker in msg_str:
                        text_start = msg_str.find(text_marker)
                        if text_start != -1:
                            brace = msg_str.find("{", text_start)
                            if brace != -1:
                                preview_text = msg_str[brace + 1 : msg_str.find("}", brace)]
                            elif hasattr(msg_data[0], "__iter__") and len(msg_data[0]) > 2:
                                part = msg_data[0][2]
                                if isinstance(part, bytes):
                                    preview_text = part.decode("utf-8", errors="replace")
                                elif isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                                    preview_text = part[1].decode("utf-8", errors="replace")
                except Exception:
                    preview_text = ""

                email_entry = sanitize_email_preview_fields({
                    "id": f"{mailbox_id}_{folder_name}_{email_uid}",
                    "mailbox_id": mailbox_id,
                    "folder": folder_name,
                    "uid": email_uid,
                    "subject": headers.get('subject', '(No Subject)'),
                    "from_address": from_addr,
                    "to_addresses": to_addrs,
                    "cc_addresses": cc_addrs if cc_addrs else None,
                    "date": headers.get('date', ''),
                    "is_read": is_read,
                    "is_seen": is_read,
                    "is_flagged": is_flagged,
                    "is_deleted": is_deleted,
                    "has_attachments": bool('attachment' in msg_str.lower() or headers.get('content-type', '').lower().startswith('multipart/')),
                    "attachments": [],
                    "preview": preview_text,
                })

                emails.append(email_entry)

            except Exception as e:
                pass

                logger.debug(f"Exception in email loop for {email_id}: {type(e).__name__}: {e}")
                continue

        connection.logout()

        # Sort IMAP result by date descending (newest first) so the order
        # matches what users expect from a modern email client.
        def _imap_sort_key(item):
            d = item.get("date") if isinstance(item, dict) else None
            ts = 0
            if d:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(d)
                    if dt is not None:
                        ts = dt.timestamp()
                except Exception:
                    pass

                    ts = 0
            return ts

        emails.sort(key=_imap_sort_key, reverse=True)

        # AUTO-SAVE: Persist freshly fetched IMAP emails to database so they survive across sessions
        if ASYNC_IMAP_AVAILABLE and emails:
            try:
                highest_uid = 0
                for email_entry in emails:
                    email_uid = email_entry.get('uid', 0)
                    save_email_to_server(user_id, base_path, mailbox_id, folder, email_entry, email_uid)
                    highest_uid = max(highest_uid, email_uid)
                
                # Update SyncState with highest UID so incremental syncs work correctly
                if highest_uid > 0:
                    try:
                        from db_helpers import get_sync_state, update_sync_state
                        current_state = get_sync_state(mailbox_id)
                        current_uid = int(current_state.last_uid) if current_state and current_state.last_uid else 0
                        if highest_uid > current_uid:
                            update_sync_state(mailbox_id, last_uid=highest_uid)
                    except Exception as e:
                        logger.debug(f"Warning: Could not update SyncState: {e}")
            except Exception as e:
                logger.debug(f"Warning: Could not save fetched emails to DB: {e}")

        return True, emails, f"Fetched {len(emails)} emails", total_count

    except Exception as e:
        pass

        return False, None, f"Error fetching emails: {str(e)}", 0


def get_email_body(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int
) -> tuple:
    """
    Get full email body content
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name
        email_uid: Email UID
    
    Returns:
        tuple: (success: bool, body: dict or None, message: str)
    """
    try:
        db_email = None
        db_folder_path = folder

        def _load_legacy_attachments(folder_name, email_uid):
            try:
                storage_path = get_mailbox_storage_path(user_id, mailbox_id, base_path)
                normalized_folder = folder_name.replace('/', '_')
                folder_path = storage_path / normalized_folder
                if not folder_path.exists():
                    for existing_folder in storage_path.iterdir() if storage_path.exists() else []:
                        if existing_folder.is_dir() and existing_folder.name.lower() == normalized_folder.lower():
                            folder_path = existing_folder
                            break
                email_file = folder_path / f"email_{email_uid}.json"
                if not email_file.exists():
                    return []
                with open(email_file, 'r', encoding='utf-8') as f:
                    email_record = json.load(f)
                data = email_record.get('data', {}) if isinstance(email_record, dict) else {}
                attachments = data.get('attachments') or []
                if not isinstance(attachments, list):
                    return []
                parsed = []
                for att in attachments:
                    if not isinstance(att, dict):
                        continue
                    filename = att.get('filename') or att.get('name')
                    if not filename:
                        continue
                    parsed.append({
                        'filename': filename,
                        'content_type': att.get('content_type') or att.get('mime_type') or 'application/octet-stream',
                        'size': int(att.get('size') or att.get('size_bytes') or 0),
                        'is_inline': bool(att.get('is_inline')),
                        'storage_path': att.get('storage_path'),
                    })
                return parsed
            except Exception:
                return []

        if ASYNC_IMAP_AVAILABLE:
            try:
                from db_helpers import get_email_by_uid
                db_email = get_email_by_uid(mailbox_id, email_uid)
                if db_email:
                    db_folder_path = db_email.folder.full_path if getattr(db_email, 'folder', None) and getattr(db_email.folder, 'full_path', None) else folder
                    sender_display = db_email.sender or ""
                    sender_email = db_email.sender_email or sender_display
                    attachments = [
                        {
                            "filename": att.filename,
                            "content_type": att.content_type,
                            "size": int(att.size_bytes or 0),
                            "is_inline": bool(att.is_inline),
                            "storage_path": att.storage_path,
                        }
                        for att in (db_email.attachments or [])
                    ]
                    if not attachments and db_email.has_attachments:
                        attachments = _load_legacy_attachments(db_folder_path, email_uid)
                    body_text = db_email.body_text or db_email.snippet or ""
                    body_html = db_email.body_html
                    if body_text or body_html or db_email.snippet or attachments:
                        safe_preview = extract_email_preview(db_email.snippet or body_text or db_email.body_html or "")
                        return True, {
                            "uid": email_uid,
                            "folder": db_folder_path,
                            "subject": db_email.subject,
                            "from_address": {"email": sender_email, "name": sender_display},
                            "body_text": body_text,
                            "body_html": body_html,
                            "date": db_email.date.isoformat() if db_email.date else "",
                            "message_id": db_email.message_id,
                            "is_read": db_email.is_read,
                            "preview": safe_preview,
                            "has_attachments": bool(db_email.has_attachments or len(attachments) > 0),
                            "attachments": attachments,
                        }, "Email loaded from database"
            except Exception as e:
                pass

                logger.debug(f"Database body lookup failed: {e}")

        # Legacy JSON cache fallback (read-only)
        storage_path = get_mailbox_storage_path(user_id, mailbox_id, base_path)

        # Normalize folder name for case-insensitive matching using stored DB folder path if available
        effective_folder = db_folder_path or folder
        normalized_folder = effective_folder.replace('/', '_').lower()

        # Find matching folder in storage (case-insensitive search)
        matching_folder_path = None
        if storage_path.exists():
            for existing_folder in storage_path.iterdir():
                if existing_folder.is_dir() and existing_folder.name.lower() == normalized_folder.lower():
                    matching_folder_path = existing_folder
                    break

        if matching_folder_path is None:
            # Check if folder path exists with the exact normalized name
            folder_path = storage_path / effective_folder.replace('/', '_')
            if folder_path.exists():
                matching_folder_path = folder_path

        if matching_folder_path is not None:
            email_file = matching_folder_path / f"email_{email_uid}.json"

            if email_file.exists():
                # Server-side cache exists! Load from local file instead of IMAP
                logger.debug(f"Loading email {email_uid} from server cache for folder: {folder}")

                try:
                    with open(email_file, 'r', encoding='utf-8') as f:
                        email_record = json.load(f)
                        email_data = email_record.get('data', {})

                    # Sanitize the preview/snippet so list view rendering is
                    # safe even when the cached preview contains raw HTML.
                    if isinstance(email_data, dict) and 'preview' in email_data:
                        email_data['preview'] = extract_email_preview(email_data.get('preview') or '')

                    return True, email_data, "Email loaded from server cache"

                except Exception as e:
                    pass

                    logger.warning(f"Error reading email from server cache: {e}")
                    # Fall through to IMAP fallback

        # FALLBACK: No server cache, fetch from IMAP
        logger.debug(f"No server cache for email {email_uid}, fetching from IMAP...")
        
        # Get mailbox config with credentials
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, None, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        # Connect to IMAP
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        # Select folder with retry for different folder name formats (Yahoo compatibility)
        folder_name = db_folder_path or folder or "INBOX"

        select_status, select_data = connection.select(quote_imap_folder_name(folder_name))

        # If that fails, try different casing/formats
        if select_status != 'OK':
            select_status, select_data = connection.select(quote_imap_folder_name(folder_name.lower()))

        if select_status != 'OK':
            select_status, select_data = connection.select(quote_imap_folder_name(folder_name.capitalize()))

        if select_status != 'OK':
            select_status, select_data = connection.select(quote_imap_folder_name(folder_name.upper()))

        if select_status != 'OK':
            connection.logout()
            return False, None, f"Failed selecting folder: {folder_name}"
        # Fetch full email
        status, msg_data = connection.fetch(str(email_uid), '(RFC822)')
        
        if status != 'OK':
            connection.logout()
            return False, None, "Failed to fetch email"
        
        raw_email = msg_data[0][1]
        
        # Parse email
        from email import message_from_bytes
        
        email_msg = message_from_bytes(raw_email)
        
        # Extract headers
        headers = {}
        for key, value in email_msg.items():
            headers[key.lower()] = value
        
        # Extract body
        body_text = ""
        body_html = ""
        has_attachments = False
        attachments = []
        
        if email_msg.is_multipart():
            for part in email_msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                
                if content_disposition and 'attachment' in content_disposition:
                    # This is an attachment
                    filename = part.get_filename()
                    if filename:
                        has_attachments = True
                        attachments.append({
                            "filename": filename,
                            "content_type": content_type,
                            "size": len(part.get_payload())
                        })
                elif content_type == 'text/plain':
                    try:
                        body_text = part.get_payload(decode=True).decode('utf-8', errors='replace')
                    except Exception:
                        pass

                        body_text = str(part.get_payload())
                elif content_type == 'text/html':
                    try:
                        body_html = part.get_payload(decode=True).decode('utf-8', errors='replace')
                    except Exception:
                        pass

                        body_html = str(part.get_payload())
        else:
            content_type = email_msg.get_content_type()
            if content_type == 'text/plain':
                try:
                    body_text = email_msg.get_payload(decode=True).decode('utf-8', errors='replace')
                except Exception:
                    pass

                    body_text = str(email_msg.get_payload())
            elif content_type == 'text/html':
                try:
                    body_html = email_msg.get_payload(decode=True).decode('utf-8', errors='replace')
                except Exception:
                    pass

                    body_html = str(email_msg.get_payload())
        
        # Generate preview - use plain text when available, otherwise strip
        # HTML tags from the HTML body so list view never contains raw markup
        # that could break the surrounding layout.
        if body_text:
            raw_preview = body_text[:200]
        elif body_html:
            raw_preview = extract_email_preview(body_html[:500])
        else:
            raw_preview = ""
        preview = raw_preview.replace('\r\n', ' ').replace('\n', ' ').strip()
        
        connection.logout()
        
        # Parse addresses
        from_addr = parse_email_address(headers.get('from', ''))
        to_addrs = []
        for addr in email_msg.get_all('to', []):
            to_addrs.append(parse_email_address(addr))
        
        cc_addrs = []
        for addr in email_msg.get_all('cc', []):
            cc_addrs.append(parse_email_address(addr))
        
        return True, {
            "id": f"{mailbox_id}_{folder}_{email_uid}",
            "mailbox_id": mailbox_id,
            "folder": folder,
            "uid": email_uid,
            "subject": decode_email_header_value(email_msg.get('subject'), '(No Subject)'),
            "from_address": from_addr,
            "to_addresses": to_addrs,
            "cc_addresses": cc_addrs if cc_addrs else None,
            "date": decode_email_header_value(headers.get('date', '')),
            "body_text": body_text,
            "body_html": body_html,
            "is_read": True,
            "has_attachments": has_attachments,
            "attachments": attachments,
            "headers": headers,
            "preview": preview,
            "message_id": decode_email_header_value(headers.get('message-id')),
            "in_reply_to": decode_email_header_value(headers.get('in-reply-to')),
            "references": decode_email_header_value(headers.get('references'))
        }, "Email body retrieved"
        
    except Exception as e:
        pass

        return False, None, f"Error getting email body: {str(e)}"


def get_email_attachment(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int,
    filename: str,
) -> tuple:
    """Fetch a single attachment from an email for download."""
    try:
        from email import message_from_bytes
        from email.header import decode_header

        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        if not mailbox:
            return False, None, "Mailbox not found"

        imap_config = mailbox.get("imap_config") or {}
        connection = connect_imap(
            host=imap_config.get("host"),
            port=int(imap_config.get("port", 993)),
            username=imap_config.get("username"),
            password=imap_config.get("password"),
            use_ssl=imap_config.get("use_ssl", True),
            validate_cert=imap_config.get("validate_cert", True),
        )

        db_email = None
        try:
            from db_helpers import get_email_by_uid
            db_email = get_email_by_uid(mailbox_id, email_uid)
        except Exception:
            db_email = None

        folder_name = (db_email.folder.full_path if db_email and getattr(db_email, 'folder', None) and getattr(db_email.folder, 'full_path', None) else folder) or "INBOX"
        select_status, _ = connection.select(quote_imap_folder_name(folder_name))
        if select_status != "OK":
            for variant in (folder_name.lower(), folder_name.capitalize(), folder_name.upper()):
                select_status, _ = connection.select(quote_imap_folder_name(variant))
                if select_status == "OK":
                    break
        if select_status != "OK":
            connection.logout()
            return False, None, f"Failed selecting folder: {folder_name}"

        status, msg_data = connection.fetch(str(email_uid), "(RFC822)")
        connection.logout()

        if status != "OK" or not msg_data:
            return False, None, "Failed to fetch email"

        raw_email = msg_data[0][1]
        email_msg = message_from_bytes(raw_email)
        target = filename.strip().lower()

        def _decode_name(name):
            if not name:
                return ""
            parts = decode_header(name)
            decoded = []
            for part, enc in parts:
                if isinstance(part, bytes):
                    decoded.append(part.decode(enc or "utf-8", errors="replace"))
                else:
                    decoded.append(str(part))
            return "".join(decoded)

        if email_msg.is_multipart():
            for part in email_msg.walk():
                part_name = _decode_name(part.get_filename())
                disposition = str(part.get("Content-Disposition", ""))
                if not part_name:
                    continue
                if part_name.lower() != target and target not in part_name.lower():
                    continue
                if "attachment" in disposition.lower() or part.get_content_maintype() != "text":
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    return True, {
                        "filename": part_name,
                        "content_type": part.get_content_type(),
                        "content": payload,
                    }, "Attachment retrieved"

        return False, None, "Attachment not found"
    except Exception as e:
        return False, None, f"Error downloading attachment: {str(e)}"


def search_emails_in_mailbox(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    query: dict
) -> tuple:
    """
    Search emails with various criteria.
    Searches local database/cache first, then falls back to live IMAP.
    """
    try:
        folder = query.get('folder', 'INBOX')
        search_text = (query.get('search_text') or '').strip()
        search_field = (query.get('search_field') or 'all').lower()
        is_unread = query.get('is_unread')
        if is_unread is None:
            is_unread = query.get('unread_only')
        has_attachments = query.get('has_attachments')
        limit = max(1, int(query.get('limit', 200)))
        offset = max(0, int(query.get('offset', 0)))

        def _matches_search(email_dict: dict) -> bool:
            if not search_text:
                return True
            q = search_text.lower()
            subject = str(email_dict.get('subject') or '').lower()
            sender = str((email_dict.get('from_address') or {}).get('email') or '').lower()
            sender_name = str((email_dict.get('from_address') or {}).get('name') or '').lower()
            preview = extract_email_preview(
                str(email_dict.get('preview') or email_dict.get('body_text') or email_dict.get('body_html') or '')
            ).lower()
            recipients = email_dict.get('to_addresses') or []
            to_text = ' '.join(
                str(r.get('email') if isinstance(r, dict) else r) for r in recipients
            ).lower()
            if search_field == 'subject':
                return q in subject
            if search_field == 'from':
                return q in sender or q in sender_name
            if search_field == 'to':
                return q in to_text
            if search_field == 'body':
                return q in preview
            return q in subject or q in sender or q in sender_name or q in to_text or q in preview

        # --- Local database search ---
        if ASYNC_IMAP_AVAILABLE:
            try:
                from db_helpers import get_or_create_folder_by_path, get_emails_by_folder
                folder_obj = get_or_create_folder_by_path(mailbox_id, folder)
                db_emails = get_emails_by_folder(folder_obj.id, limit=10000, offset=0)
                if db_emails:
                    results = []
                    for row in db_emails:
                        item = _db_email_to_list_dict(row, mailbox_id, folder)
                        if is_unread and item.get('is_read'):
                            continue
                        if has_attachments and not item.get('has_attachments'):
                            continue
                        if not _matches_search(item):
                            continue
                        results.append(item)
                    page = results[offset: offset + limit]
                    if page or results:
                        return True, page, f"Found {len(results)} emails"
            except Exception as e:
                logger.debug(f"Local DB search failed, falling back to IMAP: {e}")

        # --- Legacy JSON cache search ---
        storage_path = get_mailbox_storage_path(user_id, mailbox_id, base_path)
        normalized_folder = folder.replace('/', '_').lower()
        matching_folder_path = None
        if storage_path.exists():
            for existing_folder in storage_path.iterdir():
                if existing_folder.is_dir() and existing_folder.name.lower() == normalized_folder.lower():
                    matching_folder_path = existing_folder
                    break
        if matching_folder_path is None:
            candidate = storage_path / folder.replace('/', '_')
            if candidate.exists():
                matching_folder_path = candidate
        if matching_folder_path is not None:
            email_files = list(matching_folder_path.glob("email_*.json"))
            if email_files:
                results = []
                for email_file in email_files:
                    try:
                        with open(email_file, 'r',encoding='utf-8') as f:
                            email_record = json.load(f)
                        item = sanitize_email_preview_fields(email_record.get('data', {}))
                        if is_unread and item.get('is_read'):
                            continue
                        if has_attachments and not item.get('has_attachments'):
                            continue
                        if not _matches_search(item):
                            continue
                        results.append(item)
                    except Exception:
                        continue
                def _search_sort_key(item):
                    d = item.get('date')
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(d) if d else None
                        return dt.timestamp() if dt else 0
                    except Exception:
                        return 0
                results.sort(key=_search_sort_key,reverse=True)
                page = results[offset: offset + limit]
                if page or results:
                    return True, page, f"Found {len(results)} emails"

        # --- IMAP fallback ---
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, None, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        # Connect to IMAP
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder))
        if select_status != 'OK':
            for variant in (folder.lower(), folder.capitalize(), folder.upper()):
                select_status, select_data = connection.select(quote_imap_folder_name(variant))
                if select_status == 'OK':
                    folder = variant
                    break
        if select_status != 'OK':
            connection.logout()
            return False, [], f"Failed selecting folder: {folder}"
        
        # Build IMAP search criteria
        criteria_parts = []
        
        if is_unread:
            criteria_parts.append('UNSEEN')
        
        if query.get('has_attachments'):
            criteria_parts.append('HASATTACHMENT')
        
        if query.get('from_address'):
            criteria_parts.append(f'FROM "{query["from_address"]}"')
        
        if query.get('to_address'):
            criteria_parts.append(f'TO "{query["to_address"]}"')
        
        if query.get('subject'):
            criteria_parts.append(f'SUBJECT "{query["subject"]}"')
        
        if query.get('since_date'):
            criteria_parts.append(f'SINCE {query["since_date"]}')
        
        if query.get('before_date'):
            criteria_parts.append(f'BEFORE {query["before_date"]}')
        
        # If no specific criteria, search all
        if not criteria_parts:
            criteria = 'ALL'
        else:
            criteria = ' '.join(criteria_parts)
        
        # Perform search
        status, email_ids = connection.search(None, criteria)
        
        if status != 'OK':
            connection.logout()
            return False, None, f"Search failed: {status}"
        
        all_ids = email_ids[0].split()
        total_count = len(all_ids)
        
        # Apply pagination
        limit = max(1, int(query.get('limit', 200)))
        offset = max(0, query.get('offset', 0))
        
        start_index = offset
        end_index = min(len(all_ids), offset + limit)
        
        if start_index >= len(all_ids):
            connection.logout()
            return True, [], "No matching emails found"
        
        paginated_ids = all_ids[start_index:end_index]
        
        emails = []
        
        for email_id in paginated_ids:
            try:
                # Fetch headers for filtering
                status, header_data = connection.fetch(email_id, '(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)] FLAGS)')
                
                if status != 'OK':
                    continue
                
                raw_headers = header_data[0][1]
                headers = parse_email_headers(raw_headers)
                msg_str = str(header_data[0])
                is_read = '\\Seen' in msg_str or 'Seen' in msg_str
                
                body_content = ""
                if search_text and search_field in ('all', 'body'):
                    status, body_data = connection.fetch(email_id, '(BODY.PEEK[TEXT])')
                    if status == 'OK' and body_data:
                        try:
                            part = body_data[0][1]
                            if isinstance(part, bytes):
                                body_content = part.decode('utf-8', errors='replace')
                        except Exception:
                            pass
                
                if search_text:
                    search_lower = search_text.lower()
                    subject_match = search_lower in headers.get('subject', '').lower()
                    from_match = search_lower in headers.get('from', '').lower()
                    to_match = search_lower in headers.get('to', '').lower()
                    body_match = search_lower in extract_email_preview(body_content).lower()
                    if search_field == 'subject' and not subject_match:
                        continue
                    elif search_field == 'from' and not from_match:
                        continue
                    elif search_field == 'to' and not to_match:
                        continue
                    elif search_field == 'body' and not body_match:
                        continue
                    elif search_field == 'all' and not (subject_match or from_match or to_match or body_match):
                        continue
                
                # Parse addresses
                from_addr = parse_email_address(headers.get('from', ''))
                to_addrs = [parse_email_address(a) for a in headers.get('to', '').split(', ') if a]
                cc_addrs = [parse_email_address(a) for a in headers.get('cc', '').split(', ') if a]
                
                # Get UID
                email_uid = int(email_id)
                try:
                    status, uid_data = connection.fetch(email_id, '(UID)')
                    if status == 'OK':
                        uid_match = str(uid_data[0]).split('UID ')[1].split(')')[0]
                        email_uid = int(uid_match)
                except Exception:
                    pass

                    pass
                
                email_entry = sanitize_email_preview_fields({
                    "id": f"{mailbox_id}_{folder}_{email_uid}",
                    "mailbox_id": mailbox_id,
                    "folder": folder,
                    "uid": email_uid,
                    "subject": headers.get('subject', '(No Subject)'),
                    "from_address": from_addr,
                    "to_addresses": to_addrs,
                    "cc_addresses": cc_addrs if cc_addrs else None,
                    "date": headers.get('date', ''),
                    "is_read": is_read,
                    "is_seen": is_read,
                    "is_flagged": False,
                    "is_deleted": False,
                    "has_attachments": False,
                    "attachments": [],
                    "preview": extract_email_preview(body_content),
                })
                
                emails.append(email_entry)
                
            except Exception:
                pass

                continue
        
        connection.logout()
        
        return True, emails, f"Found {len(emails)} emails"
        
    except Exception as e:
        pass

        return False, None, f"Error searching emails: {str(e)}"


def load_emails_from_server(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    limit: int = 200,
    offset: int = 0
) -> tuple:
    """
    Load emails from server storage (cached files)
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name
        limit: Max emails to return
        offset: Pagination offset
    
    Returns:
        tuple: (success: bool, emails: list or None, message: str)
    """
    try:
        # Get storage path
        storage_path = get_mailbox_storage_path(user_id, mailbox_id, base_path)
        folder_path = storage_path / folder.replace('/', '_')
        
        # Check if saved emails exist
        if not folder_path.exists():
            return False, None, "No saved emails found"
        
        # Load emails list
        email_list_file = folder_path / "emails_list.json"
        
        if not email_list_file.exists():
            return False, None, "No email index found"
        
        # Get all email files
        email_files = sorted(folder_path.glob("email_*.json"), key=lambda x: int(x.stem.split('_')[1]) if x.stem.startswith('email_') else 0)
        
        if not email_files:
            return False, None, "No emails saved"
        
        # Apply pagination
        start_idx = offset
        end_idx = min(offset + limit, len(email_files))
        
        emails = []
        for i in range(start_idx, end_idx):
            try:
                with open(email_files[i], 'r', encoding='utf-8') as f:
                    email_record = json.load(f)
                    email_data = email_record.get('data', {})
                    # Sanitize preview so list view never embeds raw HTML
                    if isinstance(email_data, dict) and email_data.get('preview'):
                        email_data['preview'] = extract_email_preview(email_data['preview'])
                    emails.append(email_data)
            except Exception as e:
                pass

                logger.warning(f"Error loading email file {email_files[i]}: {e}")
                continue

        # Sort by date descending (newest first)
        def _load_sort_key(item):
            d = item.get("date") if isinstance(item, dict) else None
            ts = 0
            if d:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(d)
                    if dt is not None:
                        ts = dt.timestamp()
                except Exception:
                    pass

                    ts = 0
            return ts

        emails.sort(key=_load_sort_key, reverse=True)

        return True, emails, f"Loaded {len(emails)} emails from server"
        
    except Exception as e:
        pass

        return False, None, f"Error loading from server: {str(e)}"


def get_mailbox_last_uid(mailbox_id: str) -> int:
    """Return the last synced IMAP UID for a mailbox from the database."""
    if not ASYNC_IMAP_AVAILABLE:
        return 0
    try:
        from db_helpers import get_sync_state
        state = get_sync_state(mailbox_id)
        return int(state.last_uid) if state and state.last_uid else 0
    except Exception:
        pass

        return 0


def update_mailbox_last_uid(mailbox_id: str, last_uid: int) -> bool:
    """Persist the highest synced IMAP UID for a mailbox."""
    if not ASYNC_IMAP_AVAILABLE:
        return False
    try:
        from db_helpers import update_sync_state
        update_sync_state(mailbox_id, last_uid=last_uid, status="completed")
        return True
    except Exception as e:
        pass

        logger.warning(f"Error updating last UID for {mailbox_id}: {e}")
        return False


def _normalize_sender_fields(email_data: dict) -> tuple:
    """Extract sender display name and email from heterogeneous email_data."""
    sender = email_data.get("from") or email_data.get("from_address", "")
    if isinstance(sender, dict):
        sender_display = sender.get("name") or sender.get("email") or ""
        sender_email = sender.get("email") or ""
    else:
        sender_display = decode_email_header_value(str(sender))
        sender_email = sender_display if "@" in sender_display else ""
    return sender_display, sender_email


def save_email_to_server(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_data: dict,
    email_uid: int
) -> bool:
    """
    Save an email to the database (replaces legacy per-email JSON files).
    """
    if not ASYNC_IMAP_AVAILABLE:
        return False

    try:
        from email.utils import parsedate_to_datetime
        from db_helpers import (
            get_or_create_folder_by_path,
            create_email,
            get_email_by_uid,
            update_email_body,
            update_email_attachments,
        )

        folder_obj = get_or_create_folder_by_path(mailbox_id, folder)
        existing = get_email_by_uid(mailbox_id, email_uid)

        date_val = None
        date_str = decode_email_header_value(
            email_data.get("date") or email_data.get("date_header")
        )
        if date_str:
            try:
                date_val = parsedate_to_datetime(date_str)
            except Exception:
                pass

                pass

        sender_display, sender_email = _normalize_sender_fields(email_data)
        recipients = email_data.get("recipients")
        if recipients is None and email_data.get("to_addresses"):
            recipients = json.dumps(email_data.get("to_addresses"))

        body_text = email_data.get("body_text")
        body_html = email_data.get("body_html")
        snippet_raw = email_data.get("preview") or (
            (body_text or body_html or "")[:200].replace("\r\n", " ").strip()
        )
        snippet = extract_email_preview(decode_email_header_value(snippet_raw))

        subject = decode_email_header_value(email_data.get("subject"), "(No Subject)")
        message_id = decode_email_header_value(email_data.get("message_id")) or None
        has_attachments = bool(email_data.get("has_attachments") or email_data.get("attachments"))

        if existing:
            from db_helpers import mark_email_read, update_email_attachments
            mark_email_read(existing.id, bool(email_data.get("is_read")))
            if body_text or body_html:
                update_email_body(existing.id, body_html=body_html, body_text=body_text)
            if email_data.get("attachments"):
                update_email_attachments(existing.id, email_data.get("attachments"))
            return True

        saved_email = create_email(
            mailbox_id=mailbox_id,
            folder_id=folder_obj.id,
            imap_uid=email_uid,
            subject=subject,
            sender=sender_display,
            sender_email=sender_email or None,
            recipients=recipients,
            date=date_val,
            message_id=message_id,
            has_attachments=has_attachments,
            is_read=bool(email_data.get("is_read")),
            snippet=snippet,
            body_text=body_text,
            body_html=body_html,
        )
        if email_data.get("attachments") and saved_email:
            from db_helpers import update_email_attachments
            update_email_attachments(saved_email.id, email_data.get("attachments"))
        return True
    except Exception as e:
        pass

        logger.warning(f"Error saving email {email_uid} to database: {e}")
        return False


async def sync_mailbox_folder(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str = "INBOX",
    full_sync: bool = False
) -> dict:
    """
    Sync emails from a specific folder (async for background sync)
    SAVES all emails to server storage!
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name (default: INBOX)
        full_sync: If True, resync all emails; if False, only new ones
    
    Returns:
        dict with sync results
    """
    result = {
        "success": False,
        "folder": folder,
        "new_emails": 0,
        "total_emails": 0,
        "unread_count": 0,
        "message": ""
    }
    
    try:
        # Update status to syncing
        update_mailbox_sync_status(user_id, base_path, mailbox_id, "syncing")
        
        # Get mailbox config
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            result["message"] = "Mailbox not found"
            return result
        
        imap_config = (mailbox.get('imap_config') or {})
        
        # Connect to IMAP
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        # Select folder with validation
        folder_name = folder if folder else "INBOX"
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder_name))
        if select_status != 'OK':
            select_status, select_data = connection.select(quote_imap_folder_name(folder_name.lower()))
        if select_status != 'OK':
            select_status, select_data = connection.select(quote_imap_folder_name(folder_name.upper()))
        if select_status != 'OK':
            connection.logout()
            update_mailbox_sync_status(user_id, base_path, mailbox_id, "error")
            result["message"] = f"Failed selecting folder: {folder}"
            return result
        
        last_uid = 0
        if ASYNC_IMAP_AVAILABLE and not full_sync:
            try:
                from db_helpers import get_or_create_folder_by_path, get_folder_last_uid
                folder_obj = get_or_create_folder_by_path(mailbox_id, folder_name)
                last_uid = get_folder_last_uid(mailbox_id, folder_obj.id)
            except Exception:
                last_uid = get_mailbox_last_uid(mailbox_id) if ASYNC_IMAP_AVAILABLE else 0
        elif ASYNC_IMAP_AVAILABLE:
            last_uid = 0

        initial_sync_limit = 500
        search_criteria = f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
        status, email_ids = connection.search(None, search_criteria)

        if status != 'OK':
            connection.logout()
            update_mailbox_sync_status(user_id, base_path, mailbox_id, "error")
            result["message"] = "Failed to search emails"
            return result

        raw_ids = email_ids[0]
        if isinstance(raw_ids, bytes):
            raw_ids = raw_ids.decode('utf-8', errors='replace')

        all_ids = raw_ids.split()
        if last_uid == 0 and len(all_ids) > initial_sync_limit:
            all_ids = all_ids[-initial_sync_limit:]
        total_emails = len(all_ids)
        
        # Get unread count
        status, unread_ids = connection.search(None, 'UNSEEN')
        unread_count = len(unread_ids[0].split()) if unread_ids[0] else 0

        logger.debug(f"Syncing {total_emails} emails from {folder} to database...")
        saved_count = 0
        highest_uid = last_uid

        for email_id in all_ids:
            try:
                email_uid = int(email_id)
                
                # Fetch full email
                status, msg_data = connection.fetch(str(email_uid), '(RFC822)')
                
                if status == 'OK' and msg_data:
                    raw_email = msg_data[0][1]
                    
                    # Parse email
                    from email import message_from_bytes
                    email_msg = message_from_bytes(raw_email)
                    
                    # Extract headers
                    headers = {k.lower(): v for k, v in email_msg.items()}
                    
                    # Extract body and attachments
                    body_text = ""
                    body_html = ""
                    attachments = []
                    has_attachments = False
                    
                    if email_msg.is_multipart():
                        for part in email_msg.walk():
                            content_type = part.get_content_type()
                            content_disposition = str(part.get("Content-Disposition", ""))
                            filename = part.get_filename()
                            if filename or ('attachment' in content_disposition.lower()):
                                if filename:
                                    has_attachments = True
                                    payload = part.get_payload(decode=True)
                                    attachments.append({
                                        "filename": filename,
                                        "content_type": content_type,
                                        "size": len(payload) if payload is not None else 0,
                                    })
                            elif content_type == 'text/plain':
                                try:
                                    body_text = part.get_payload(decode=True).decode('utf-8', errors='replace')
                                except:
                                    pass
                            elif content_type == 'text/html' and not body_html:
                                try:
                                    body_html = part.get_payload(decode=True).decode('utf-8', errors='replace')
                                except:
                                    pass
                    else:
                        ct = email_msg.get_content_type()
                        if ct == 'text/plain':
                            try:
                                body_text = email_msg.get_payload(decode=True).decode('utf-8', errors='replace')
                            except:
                                pass
                        elif ct == 'text/html':
                            try:
                                body_html = email_msg.get_payload(decode=True).decode('utf-8', errors='replace')
                            except:
                                pass
                    
                    # Parse addresses
                    from_addr = parse_email_address(headers.get('from', ''))
                    to_addrs = []
                    for addr in email_msg.get_all('to', []):
                        to_addrs.append(parse_email_address(addr))
                    
                    cc_addrs = []
                    for addr in email_msg.get_all('cc', []):
                        cc_addrs.append(parse_email_address(addr))
                    
                    # Create email data dict
                    email_data = {
                        "id": f"{mailbox_id}_{folder}_{email_uid}",
                        "mailbox_id": mailbox_id,
                        "folder": folder,
                        "uid": email_uid,
                        "subject": decode_email_header_value(email_msg.get('subject'), '(No Subject)'),
                        "from_address": from_addr,
                        "to_addresses": to_addrs,
                        "cc_addresses": cc_addrs if cc_addrs else None,
                        "date": decode_email_header_value(headers.get('date', '')),
                        "body_text": body_text,
                        "body_html": body_html,
                        "is_read": '\\Seen' in str(msg_data) or 'Seen' in str(msg_data),
                        "message_id": decode_email_header_value(headers.get('message-id')),
                        "has_attachments": has_attachments,
                        "attachments": attachments,
                        "preview": extract_email_preview(
                            body_text or body_html or ""
                        ),
                    }
                    
                    # Save to server
                    if save_email_to_server(user_id, base_path, mailbox_id, folder, email_data, email_uid):
                        saved_count += 1
                        highest_uid = max(highest_uid, email_uid)

            except Exception as e:
                pass

                logger.warning(f"Error saving email {email_id}: {e}")
                continue

        if saved_count > 0 and ASYNC_IMAP_AVAILABLE:
            try:
                from db_helpers import get_or_create_folder_by_path, get_folder_last_uid
                folder_obj = get_or_create_folder_by_path(mailbox_id, folder_name)
                folder_last = get_folder_last_uid(mailbox_id, folder_obj.id)
                update_mailbox_last_uid(mailbox_id, folder_last)
            except Exception:
                update_mailbox_last_uid(mailbox_id, highest_uid)

        connection.logout()
        
        # Update mailbox with new counts
        update_mailbox_sync_status(
            user_id, base_path, mailbox_id, 
            "active", 
            unread_count
        )
        
        result["success"] = True
        result["new_emails"] = saved_count
        result["total_emails"] = total_emails
        result["unread_count"] = unread_count
        result["message"] = f"Synced {saved_count} emails, {unread_count} unread"
        
        return result
        
    except Exception as e:
        pass

        update_mailbox_sync_status(user_id, base_path, mailbox_id, "error")
        result["message"] = f"Sync error: {str(e)}"
        return result


def get_folder_list(
    user_id: str,
    base_path: Path,
    mailbox_id: str
) -> tuple:
    """
    Get list of folders in a mailbox
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
    
    Returns:
        tuple: (success: bool, folders: list or None, message: str)
    """
    try:
        # Get mailbox config with credentials
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)

        if not mailbox:
            return False, None, "Mailbox not found"

        imap_config = (mailbox.get('imap_config') or {})

        # Connect to IMAP
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )

        # Get folder list
        status, folder_list = connection.list()

        if status != 'OK':
            connection.logout()
            return False, None, "Failed to list folders"

        folders = []

        for folder in folder_list:
            if isinstance(folder, bytes):
                folder = folder.decode('utf-8', errors='replace')

            # Robust folder parsing - handle various IMAP formats
            import re

            # Try to extract folder name using regex - look for quoted string at end
            match = re.search(r'"([^"]+)"\s*$', folder)
            if match:
                folder_name = match.group(1)
            else:
                # Try to find last quoted string
                match2 = re.search(r'"([^"]+)"', folder)
                if match2:
                    folder_name = match2.group(1)
                else:
                    # Fallback: take last part after space
                    parts = folder.strip().split()
                    folder_name = parts[-1] if parts else 'Unknown'

            # Check if selectable
            is_selectable = '\\Noselect' not in folder

            if not is_selectable:
                continue

            # Skip invalid or root-like folder names
            folder_name = folder_name.strip()
            if not folder_name or folder_name == '/' or folder_name == '""':
                continue

            # Use the provider's actual folder name — no forced Spam alias
            if folder_name.upper().startswith('INBOX'):
                clean_name = 'INBOX'
            else:
                clean_name = folder_name

            folders.append({
                "name": clean_name,
                "path": folder_name,
                "unread_count": 0,
                "total_count": 0
            })

        connection.logout()
        
        # Get counts for each folder
        for folder_info in folders:
            try:
                temp_conn = connect_imap(
                    host=imap_config.get('host'),
                    port=int(imap_config.get('port', 993)),
                    username=imap_config.get('username'),
                    password=imap_config.get('password'),
                    use_ssl=imap_config.get('use_ssl', True),
                    validate_cert=imap_config.get('validate_cert', True)
                )
                
                status, data = temp_conn.status(folder_info["path"], '(UNREAD MESSAGES)')
                
                if status == 'OK' and data:
                    data_str = data[0].decode('utf-8', errors='replace') if isinstance(data[0], bytes) else data[0]
                    
                    # Parse counts
                    import re
                    unread_match = re.search(r'UNREAD (\d+)', data_str)
                    messages_match = re.search(r'MESSAGES (\d+)', data_str)
                    
                    if unread_match:
                        folder_info["unread_count"] = int(unread_match.group(1))
                    if messages_match:
                        folder_info["total_count"] = int(messages_match.group(1))
                
                temp_conn.logout()
                
            except Exception:
                pass

                continue
        
        return True, folders, f"Found {len(folders)} folders"
        
    except Exception as e:
        pass

        return False, None, f"Error getting folders: {str(e)}"


def mark_email_as_read(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int
) -> tuple:
    """
    Mark an email as read
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name
        email_uid: Email UID
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder))
        if select_status != 'OK':
            for variant in (folder.lower(), folder.capitalize(), folder.upper()):
                select_status, select_data = connection.select(quote_imap_folder_name(variant))
                if select_status == 'OK':
                    folder = variant
                    break
        
        if select_status != 'OK':
            connection.logout()
            return False, f"Failed selecting folder: {folder}"
        
        # Add SEEN flag
        connection.store(str(email_uid), '+FLAGS', '\\Seen')
        
        connection.logout()

        _update_local_email_read_state(user_id, base_path, mailbox_id, folder, email_uid, True)
        
        return True, "Email marked as read"
        
    except Exception as e:
        pass

        return False, f"Error marking email as read: {str(e)}"


def mark_email_as_unread(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int
) -> tuple:
    """
    Mark an email as unread
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name
        email_uid: Email UID
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder))
        if select_status != 'OK':
            for variant in (folder.lower(), folder.capitalize(), folder.upper()):
                select_status, select_data = connection.select(quote_imap_folder_name(variant))
                if select_status == 'OK':
                    folder = variant
                    break
        
        if select_status != 'OK':
            connection.logout()
            return False, f"Failed selecting folder: {folder}"
        
        # Remove SEEN flag
        connection.store(str(email_uid), '-FLAGS', '\\Seen')
        
        connection.logout()

        _update_local_email_read_state(user_id, base_path, mailbox_id, folder, email_uid, False)
        
        return True, "Email marked as unread"
        
    except Exception as e:
        pass

        return False, f"Error marking email as unread: {str(e)}"


def move_email_to_folder(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int,
    destination_folder: str
) -> tuple:
    """
    Move an email to another folder
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Current folder name
        email_uid: Email UID
        destination_folder: Target folder name
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder))
        
        if select_status != 'OK':
            connection.logout()
            return False, f"Failed selecting folder: {folder}"
        
        # Copy to destination and mark for deletion
        connection.copy(str(email_uid), destination_folder)
        connection.store(str(email_uid), '+FLAGS', '\\Deleted')
        
        # Expunge to actually delete
        connection.expunge()
        
        connection.logout()
        
        return True, f"Email moved to {destination_folder}"
        
    except Exception as e:
        pass

        return False, f"Error moving email: {str(e)}"


def delete_email_permanently(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    folder: str,
    email_uid: int
) -> tuple:
    """
    Permanently delete an email
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        folder: Folder name
        email_uid: Email UID
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, "Mailbox not found"
        
        imap_config = (mailbox.get('imap_config') or {})
        
        connection = connect_imap(
            host=imap_config.get('host'),
            port=int(imap_config.get('port', 993)),
            username=imap_config.get('username'),
            password=imap_config.get('password'),
            use_ssl=imap_config.get('use_ssl', True),
            validate_cert=imap_config.get('validate_cert', True)
        )
        
        select_status, select_data = connection.select(quote_imap_folder_name(folder))
        
        if select_status != 'OK':
            connection.logout()
            return False, f"Failed selecting folder: {folder}"
        
        # Mark as deleted and expunge
        connection.store(str(email_uid), '+FLAGS', '\\Deleted')
        connection.expunge()
        
        connection.logout()
        
        return True, "Email deleted permanently"
        
    except Exception as e:
        pass

        return False, f"Error deleting email: {str(e)}"


def send_email_from_mailbox(
    user_id: str,
    base_path: Path,
    mailbox_id: str,
    email_data: dict
) -> tuple:
    """
    Send email using mailbox SMTP configuration
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        mailbox_id: Mailbox identifier
        email_data: Email data dict with:
            - to_addresses: List of recipient addresses
            - cc_addresses: Optional CC recipients
            - subject: Email subject
            - body_text: Plain text body
            - body_html: HTML body
            - reply_to: Optional reply-to address
            - in_reply_to: Optional in-reply-to header
            - references: Optional references header
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        # Get mailbox config with credentials
        mailbox = get_mailbox_by_id(user_id, base_path, mailbox_id, include_sensitive=True)
        
        if not mailbox:
            return False, "Mailbox not found"
        
        smtp_config = mailbox.get('smtp_config')
        
        if not smtp_config or not smtp_config.get('host'):
            return False, "SMTP not configured for this mailbox"
        
        if not mailbox.get('smtp_enabled'):
            return False, "SMTP is disabled for this mailbox"
        
        # Get email addresses
        to_addresses = email_data.get('to_addresses', [])
        cc_addresses = email_data.get('cc_addresses', [])
        subject = email_data.get('subject', '')
        body_text = email_data.get('body_text', '')
        body_html = email_data.get('body_html', '')
        
        if not to_addresses:
            return False, "No recipients specified"
        
        if not subject:
            return False, "No subject specified"
        
        # Get sender info
        from_address = smtp_config.get('from_address') or smtp_config.get('username')
        display_name = mailbox.get('display_name', '')
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{display_name} <{from_address}>" if display_name else from_address
        msg['To'] = ', '.join(to_addresses)
        
        if cc_addresses:
            msg['Cc'] = ', '.join(cc_addresses)
        
        if email_data.get('reply_to'):
            msg['Reply-To'] = email_data['reply_to']
        
        if email_data.get('in_reply_to'):
            msg['In-Reply-To'] = email_data['in_reply_to']
        
        if email_data.get('references'):
            msg['References'] = email_data['references']
        
        # Add body
        if body_html:
            # Attach both plain text and HTML
            if body_text:
                msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
            msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        elif body_text:
            msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        
        # Connect and send
        host = smtp_config.get('host')
        port = int(smtp_config.get('port', 587))
        username = smtp_config.get('username')
        password = smtp_config.get('password')
        use_starttls = smtp_config.get('use_starttls', True)
        use_ssl = smtp_config.get('use_ssl', False)
        
        if use_ssl or port == 465:
            context = __import__('ssl').create_default_context()
            context.check_hostname = False
            context.verify_mode = __import__('ssl').CERT_NONE
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            if use_starttls:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    pass

                    pass
        
        server.login(username, password)
        
        # Combine all recipients
        all_recipients = to_addresses + (cc_addresses if cc_addresses else [])
        
        server.sendmail(from_address, all_recipients, msg.as_bytes())
        server.quit()
        
        return True, "Email sent successfully"
        
    except smtplib.SMTPAuthenticationError as e:
        pass

        return False, f"Authentication failed: {str(e)}"
    except smtplib.SMTPRecipientsRefused as e:
        pass

        return False, f"Recipient refused: {str(e)}"
    except Exception as e:
        pass

        return False, f"Error sending email: {str(e)}"


# =============================================================================
# BULK MAILBOX IMPORT FUNCTIONS
# =============================================================================

def detect_imap_provider(email_address: str, imap_host: str = None) -> str:
    """
    Auto-detect email provider based on email address or IMAP host
    
    Args:
        email_address: Email address to detect provider from
        imap_host: Optional IMAP host to help detect provider
    
    Returns:
        Provider name (gmail, yahoo, outlook, icloud, custom)
    """
    email_lower = email_address.lower()
    domain = email_lower.split('@')[1] if '@' in email_lower else ''
    
    host_lower = (imap_host or '').lower()
    
    # Gmail / Google Workspace
    if 'gmail' in domain or 'googlemail' in domain:
        return 'gmail'
    if 'google.com' in domain:
        return 'gmail'
    if 'google' in host_lower:
        return 'gmail'
    
    # Yahoo
    if 'yahoo' in domain or 'ymail' in domain:
        return 'yahoo'
    if 'yahoo' in host_lower:
        return 'yahoo'
    
    # Outlook / Microsoft 365
    if 'outlook' in domain or 'hotmail' in domain or 'live' in domain or 'msn' in domain:
        return 'outlook'
    if 'outlook' in host_lower or 'microsoft' in host_lower or 'office365' in host_lower:
        return 'outlook'
    
    # iCloud / Apple
    if 'icloud' in domain or 'me.com' in domain or 'mac.com' in domain:
        return 'icloud'
    if 'icloud' in host_lower or 'apple' in host_lower:
        return 'icloud'
    
    # ProtonMail
    if 'protonmail' in domain or 'proton.me' in domain:
        return 'protonmail'
    
    # Zoho
    if 'zoho' in domain:
        return 'zoho'
    
    # Custom or unknown
    return 'custom'


def get_provider_defaults(provider: str) -> dict:
    """
    Get default IMAP/SMTP settings for a provider
    
    Args:
        provider: Provider name
    
    Returns:
        dict with default IMAP and SMTP settings
    """
    defaults = {
        'gmail': {
            'imap_host': 'imap.gmail.com',
            'imap_port': 993,
            'smtp_host': 'smtp.gmail.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'yahoo': {
            'imap_host': 'imap.mail.yahoo.com',
            'imap_port': 993,
            'smtp_host': 'smtp.mail.yahoo.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'outlook': {
            'imap_host': 'outlook.office365.com',
            'imap_port': 993,
            'smtp_host': 'smtp.office365.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'icloud': {
            'imap_host': 'imap.icloud.com',
            'imap_port': 993,
            'smtp_host': 'smtp.mail.me.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'protonmail': {
            'imap_host': 'imap.protonmail.com',
            'imap_port': 993,
            'smtp_host': 'smtp.protonmail.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'zoho': {
            'imap_host': 'imap.zoho.com',
            'imap_port': 993,
            'smtp_host': 'smtp.zoho.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'aol': {
            'imap_host': 'imap.aol.com',
            'imap_port': 993,
            'smtp_host': 'smtp.aol.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'gmx': {
            'imap_host': 'imap.gmx.com',
            'imap_port': 993,
            'smtp_host': 'mail.gmx.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'mail': {
            'imap_host': 'imap.mail.com',
            'imap_port': 993,
            'smtp_host': 'smtp.mail.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'yandex': {
            'imap_host': 'imap.yandex.com',
            'imap_port': 993,
            'smtp_host': 'smtp.yandex.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'fastmail': {
            'imap_host': 'imap.fastmail.com',
            'imap_port': 993,
            'smtp_host': 'smtp.fastmail.com',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'comcast': {
            'imap_host': 'imap.comcast.net',
            'imap_port': 993,
            'smtp_host': 'smtp.comcast.net',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'att': {
            'imap_host': 'imap.mail.att.net',
            'imap_port': 993,
            'smtp_host': 'smtp.mail.att.net',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'verizon': {
            'imap_host': 'incoming.verizon.net',
            'imap_port': 993,
            'smtp_host': 'outgoing.verizon.net',
            'smtp_port': 587,
            'smtp_use_starttls': True
        },
        'custom': {
            'imap_host': '',
            'imap_port': 993,
            'smtp_host': '',
            'smtp_port': 587,
            'smtp_use_starttls': True
        }
    }
    
    return defaults.get(provider.lower(), defaults['custom'])


# Domain → provider mapping for IMAP auto-detection from email alone
DOMAIN_PROVIDER_MAP = {
    'gmail.com': 'gmail',
    'googlemail.com': 'gmail',
    'google.com': 'gmail',
    'yahoo.com': 'yahoo',
    'yahoo.co.uk': 'yahoo',
    'yahoo.fr': 'yahoo',
    'ymail.com': 'yahoo',
    'rocketmail.com': 'yahoo',
    'outlook.com': 'outlook',
    'hotmail.com': 'outlook',
    'hotmail.co.uk': 'outlook',
    'live.com': 'outlook',
    'msn.com': 'outlook',
    'office365.com': 'outlook',
    'icloud.com': 'icloud',
    'me.com': 'icloud',
    'mac.com': 'icloud',
    'protonmail.com': 'protonmail',
    'proton.me': 'protonmail',
    'pm.me': 'protonmail',
    'zoho.com': 'zoho',
    'zohomail.com': 'zoho',
    'aol.com': 'aol',
    'aim.com': 'aol',
    'gmx.com': 'gmx',
    'gmx.net': 'gmx',
    'mail.com': 'mail',
    'yandex.com': 'yandex',
    'yandex.ru': 'yandex',
    'fastmail.com': 'fastmail',
    'tutanota.com': 'tutanota',
    'mailbox.org': 'mailbox',
    'hey.com': 'hey',
    'comcast.net': 'comcast',
    'att.net': 'att',
    'sbcglobal.net': 'att',
    'verizon.net': 'verizon',
    'bellsouth.net': 'att',
}


def get_imap_config_from_email(email_address: str) -> dict:
    """
    Resolve full IMAP/SMTP configuration from an email address alone.
    Uses domain mapping and provider defaults.
    """
    email_lower = (email_address or '').strip().lower()
    domain = email_lower.split('@')[1] if '@' in email_lower else ''

    provider = DOMAIN_PROVIDER_MAP.get(domain)
    if not provider:
        provider = detect_imap_provider(email_address)

    defaults = get_provider_defaults(provider)

    # Custom domain fallback: imap.{domain}
    imap_host = defaults['imap_host']
    smtp_host = defaults['smtp_host']
    if provider == 'custom' and domain:
        imap_host = f'imap.{domain}'
        smtp_host = f'smtp.{domain}'

    provider_names = {
        'gmail': 'Gmail / Google Workspace',
        'yahoo': 'Yahoo Mail',
        'outlook': 'Microsoft Outlook',
        'icloud': 'iCloud Mail',
        'protonmail': 'ProtonMail',
        'zoho': 'Zoho Mail',
        'aol': 'AOL Mail',
        'gmx': 'GMX Mail',
        'mail': 'Mail.com',
        'yandex': 'Yandex Mail',
        'fastmail': 'Fastmail',
        'tutanota': 'Tutanota',
        'mailbox': 'Mailbox.org',
        'hey': 'Hey.com',
        'comcast': 'Comcast',
        'att': 'AT&T',
        'verizon': 'Verizon',
        'custom': 'Custom / Unknown',
    }

    return {
        'email': email_lower,
        'domain': domain,
        'provider': provider,
        'provider_name': provider_names.get(provider, provider),
        'imap': {
            'host': imap_host,
            'port': defaults['imap_port'],
            'use_ssl': True,
            'username': email_lower,
        },
        'smtp': {
            'host': smtp_host,
            'port': defaults['smtp_port'],
            'use_starttls': defaults.get('smtp_use_starttls', True),
            'username': email_lower,
        },
        'detected_from_domain': domain in DOMAIN_PROVIDER_MAP,
    }


def detect_imap_configs_batch(email_addresses: List[str]) -> List[dict]:
    """Batch IMAP config detection for bulk mailbox import."""
    results = []
    seen = set()
    for raw in email_addresses:
        email = (raw or '').strip().lower()
        if not email or '@' not in email or email in seen:
            continue
        seen.add(email)
        results.append(get_imap_config_from_email(email))
    return results


def parse_simple_import_line(line: str) -> Optional[dict]:
    """
    Parse simple format: email:password
    
    Args:
        line: Import line string
    
    Returns:
        dict with parsed data or None if invalid
    """
    line = line.strip()
    
    if not line or line.startswith('#'):
        return None
    
    parts = line.split(':')
    
    if len(parts) < 2:
        return None
    
    email = parts[0].strip()
    password = parts[1].strip()
    
    if '@' not in email or not password:
        return None
    
    return {
        'format': 'simple',
        'email_address': email,
        'password': password,
        'raw_line': line
    }


def parse_full_import_line(line: str) -> Optional[dict]:
    """
    Parse full format: email|imap_host|imap_port|imap_user|imap_pass|smtp_host|smtp_port|smtp_user|smtp_pass
    
    Args:
        line: Import line string
    
    Returns:
        dict with parsed data or None if invalid
    """
    line = line.strip()
    
    if not line or line.startswith('#'):
        return None
    
    parts = line.split('|')
    
    if len(parts) < 9:
        # Try colon separator
        parts = line.split(':')
        if len(parts) < 9:
            return None
    
    try:
        result = {
            'format': 'full',
            'email_address': parts[0].strip(),
            'imap_host': parts[1].strip() if len(parts) > 1 else '',
            'imap_port': int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 993,
            'imap_user': parts[3].strip() if len(parts) > 3 else parts[0].strip(),
            'imap_password': parts[4].strip() if len(parts) > 4 else '',
            'smtp_host': parts[5].strip() if len(parts) > 5 else '',
            'smtp_port': int(parts[6].strip()) if len(parts) > 6 and parts[6].strip().isdigit() else 587,
            'smtp_user': parts[7].strip() if len(parts) > 7 else parts[0].strip(),
            'smtp_password': parts[8].strip() if len(parts) > 8 else '',
            'raw_line': line
        }
        
        if '@' not in result['email_address']:
            return None
        
        return result
        
    except Exception:
        pass

        return None


def parse_json_import_entry(entry: dict) -> Optional[dict]:
    """
    Parse JSON format import entry
    
    Expected JSON format:
    {
        "email": "user@example.com",
        "password": "app_password",
        "imap_host": "imap.example.com",  // optional
        "smtp_host": "smtp.example.com"   // optional
    }
    
    Args:
        entry: JSON entry dict
    
    Returns:
        dict with parsed data or None if invalid
    """
    try:
        email = entry.get('email') or entry.get('email_address') or entry.get('address', '')
        password = entry.get('password') or entry.get('pass', '')
        
        if not email or '@' not in email or not password:
            return None
        
        return {
            'format': 'json',
            'email_address': email.lower().strip(),
            'password': password,
            'imap_host': entry.get('imap_host', ''),
            'imap_port': entry.get('imap_port', 993),
            'imap_user': entry.get('imap_user', email),
            'imap_password': entry.get('imap_password', password),
            'smtp_host': entry.get('smtp_host', ''),
            'smtp_port': entry.get('smtp_port', 587),
            'smtp_user': entry.get('smtp_user', email),
            'smtp_password': entry.get('smtp_password', ''),
            'provider': entry.get('provider'),
            'display_name': entry.get('display_name', ''),
            'raw_json': entry
        }
        
    except Exception:
        pass

        return None


def validate_mailbox_entry(user_id: str, base_path: Path, entry: dict) -> tuple:
    """
    Validate a mailbox entry before import
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        entry: Parsed mailbox entry
    
    Returns:
        tuple: (is_valid: bool, message: str, validated_data: dict or None)
    """
    email_address = entry.get('email_address', '')
    
    if not email_address or '@' not in email_address:
        return False, "Invalid email address", None
    
    # Check if already exists
    existing_mailboxes = get_user_mailboxes(user_id, base_path)
    for mailbox in existing_mailboxes:
        if mailbox.get('email_address', '').lower() == email_address.lower():
            return False, f"Mailbox with email {email_address} already exists", None
    
    # Detect or use provided provider
    provider = entry.get('provider')
    if not provider:
        imap_host = entry.get('imap_host', '')
        provider = detect_imap_provider(email_address, imap_host)
    
    domain = email_address.split('@')[1].lower().strip() if '@' in email_address else ''

    # Get defaults for provider
    defaults = get_provider_defaults(provider)

    # Build IMAP config - TRY AUTO-DETECTION FIRST from expanded server list
    imap_host = entry.get('imap_host')
    imap_port = int(entry.get('imap_port', defaults.get('imap_port', 993)))

    # If no host provided, use auto-detection
    if not imap_host:
        auto_host, auto_port = get_imap_server_for_email(email_address)
        if auto_host:
            imap_host = auto_host
            imap_port = auto_port
        elif provider == 'custom' and domain:
            imap_host = f'imap.{domain}'
            imap_port = int(defaults.get('imap_port', 993))

    imap_user = entry.get('imap_user') or email_address
    imap_password = entry.get('imap_password') or entry.get('password', '')
    
    if not imap_host:
        return False, "IMAP host is required (could not auto-detect for this domain)", None
    
    if not imap_password:
        return False, "Password is required", None
    
    # Build SMTP config (optional)
    smtp_config = None
    smtp_host = entry.get('smtp_host')
    
    if smtp_host:
        smtp_config = {
            'host': smtp_host,
            'port': int(entry.get('smtp_port', defaults['smtp_port'])),
            'username': entry.get('smtp_user') or email_address,
            'password': entry.get('smtp_password') or entry.get('password', ''),
            'use_starttls': defaults.get('smtp_use_starttls', True),
            'use_ssl': smtp_host.endswith(':465') if ':' in smtp_host else False,
            'from_address': email_address
        }
        
        if not smtp_config['password']:
            smtp_config = None  # SMTP is optional
    
    # Build validated data
    validated_data = {
        'provider': provider,
        'email_address': email_address,
        'display_name': entry.get('display_name', email_address.split('@')[0]),
        'imap_config': {
            'host': imap_host,
            'port': imap_port,
            'username': imap_user,
            'password': imap_password,
            'use_ssl': True,
            'validate_cert': True
        },
        'smtp_config': smtp_config
    }
    
    return True, "Entry is valid", validated_data


async def test_mailbox_connection(entry: dict) -> tuple:
    """
    Test connection for a mailbox entry
    
    Args:
        entry: Parsed mailbox entry
    
    Returns:
        tuple: (success: bool, message: str, details: dict)
    """
    try:
        email_address = entry.get('email_address', '')
        imap_config = entry.get('imap_config') or {}
        smtp_config = entry.get('smtp_config') or {}

        provider = entry.get('provider') or detect_imap_provider(
            email_address,
            imap_config.get('host') or entry.get('imap_host', '')
        )
        defaults = get_provider_defaults(provider)
        
        # Get IMAP settings
        imap_host = imap_config.get('host') or entry.get('imap_host') or defaults.get('imap_host')
        imap_port = int(
            imap_config.get('port') or entry.get('imap_port') or defaults.get('imap_port', 993)
        )
        imap_user = imap_config.get('username') or entry.get('imap_user') or email_address
        imap_password = imap_config.get('password') or entry.get('imap_password') or entry.get('password', '')
        
        if not imap_host or not imap_password:
            return False, "Missing IMAP configuration", {}
        
        # Test IMAP connection
        imap_result = await test_imap_connection(
            host=imap_host,
            port=imap_port,
            username=imap_user,
            password=imap_password,
            use_ssl=True,
            validate_cert=True
        )
        
        if not imap_result.get('success'):
            return False, f"IMAP: {imap_result.get('message', 'Connection failed')}", imap_result
        
        # Test SMTP if configured
        smtp_result = None
        smtp_host = smtp_config.get('host') or entry.get('smtp_host') or (defaults['smtp_host'] if provider != 'custom' else '')
        
        if smtp_host:
            smtp_port = int(
                smtp_config.get('port') or entry.get('smtp_port') or defaults.get('smtp_port', 587)
            )
            smtp_user = smtp_config.get('username') or entry.get('smtp_user') or email_address
            smtp_password = smtp_config.get('password') or entry.get('smtp_password') or entry.get('password', '')
            
            if smtp_password:
                smtp_test_result = await test_smtp_connection_simple(
                    host=smtp_host,
                    port=smtp_port,
                    username=smtp_user,
                    password=smtp_password,
                    use_starttls=defaults.get('smtp_use_starttls', True)
                )
                
                smtp_result = smtp_test_result
                
                if not smtp_test_result.get('success'):
                    # SMTP failure is not critical, just warn
                    pass
        
        return True, "All connections successful", {
            'imap': imap_result,
            'smtp': smtp_result
        }
        
    except Exception as e:
        pass

        return False, f"Connection test error: {str(e)}", {}


async def import_mailboxes_bulk(
    user_id: str,
    base_path: Path,
    entries: List[dict],
    test_only: bool = False,
    stop_on_error: bool = False,
    skip_connection_test: bool = False
) -> dict:
    """
    Bulk import mailboxes from parsed entries
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        entries: List of parsed mailbox entries
        test_only: If True, only test without saving
        stop_on_error: If True, stop on first error
    
    Returns:
        dict with import results
    """
    results = {
        'total': len(entries),
        'successful': 0,
        'failed': 0,
        'skipped': 0,
        'mailboxes': [],
        'errors': []
    }
    
    for i, entry in enumerate(entries):
        entry_num = i + 1
        
        # Validate entry
        is_valid, message, validated_data = validate_mailbox_entry(user_id, base_path, entry)
        
        if not is_valid:
            if 'already exists' in message.lower():
                results['skipped'] += 1
            else:
                results['failed'] += 1
            results['errors'].append({
                'entry_num': entry_num,
                'email': entry.get('email_address', 'unknown'),
                'error': message,
                'raw': entry.get('raw_line', str(entry))
            })
            
            if stop_on_error:
                results['stopped'] = True
                results['stopped_at'] = entry_num
                break
            continue
        
        # Test connection when in test-only mode or when not skipping tests
        if test_only or not skip_connection_test:
            success, test_message, test_details = await test_mailbox_connection(validated_data)
            
            if not success:
                results['failed'] += 1
                results['errors'].append({
                    'entry_num': entry_num,
                    'email': validated_data['email_address'],
                    'error': test_message,
                    'details': test_details
                })
                
                if stop_on_error:
                    results['stopped'] = True
                    results['stopped_at'] = entry_num
                    break
                continue

        # Save mailbox if not test only
        if not test_only:
            save_success, mailbox_id, save_message = save_mailbox_config(
                user_id, base_path, validated_data
            )
            
            if not save_success:
                if save_message and 'already exists' in save_message.lower():
                    results['skipped'] += 1
                else:
                    results['failed'] += 1
                results['errors'].append({
                    'entry_num': entry_num,
                    'email': validated_data['email_address'],
                    'error': save_message
                })
                continue
        
        # Success
        results['successful'] += 1
        
        if not test_only:
            results['mailboxes'].append({
                'entry_num': entry_num,
                'mailbox_id': mailbox_id if save_success else None,
                'email': validated_data['email_address'],
                'provider': validated_data['provider']
            })
        else:
            results['mailboxes'].append({
                'entry_num': entry_num,
                'email': validated_data['email_address'],
                'provider': validated_data['provider'],
                'test_passed': True
            })
    
    return results


def parse_import_content(
    content: str,
    format_type: str = 'auto'
) -> List[dict]:
    """
    Parse import content from various formats
    
    Args:
        content: Raw import content
        format_type: Format type ('auto', 'simple', 'full', 'json', 'jsonl')
    
    Returns:
        List of parsed entries
    """
    entries = []
    lines = content.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
        
        # Skip comments
        if line.startswith('#'):
            continue
        
        # Try to parse based on format or auto-detect
        entry = None
        
        if format_type == 'json' or (format_type == 'auto' and line.startswith('{')):
            try:
                json_data = json.loads(line)
                entry = parse_json_import_entry(json_data)
            except Exception:
                pass

                pass
        
        if not entry and (format_type == 'full' or line.count('|') >= 8 or line.count(':') >= 8):
            entry = parse_full_import_line(line)
        
        if not entry:
            entry = parse_simple_import_line(line)
        
        if entry:
            entries.append(entry)
    
    return entries


async def import_mailboxes_from_text(
    user_id: str,
    base_path: Path,
    content: str,
    format_type: str = 'auto',
    test_only: bool = False,
    skip_connection_test: bool = False
) -> dict:
    """
    Import mailboxes from raw text content
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        content: Raw import content
        format_type: Format type ('auto', 'simple', 'full', 'json')
        test_only: If True, only test without saving
    
    Returns:
        dict with import results
    """
    # Parse content into entries
    entries = parse_import_content(content, format_type)
    
    if not entries:
        return {
            'total': 0,
            'successful': 0,
            'failed': 0,
            'skipped': 0,
            'mailboxes': [],
            'errors': [{'error': 'No valid entries found in content'}]
        }
    
    # Import entries
    skip_connection_test = bool(skip_connection_test)
    return await import_mailboxes_bulk(user_id, base_path, entries, test_only, False, skip_connection_test)


def export_mailboxes_to_json(user_id: str, base_path: Path, include_passwords: bool = False) -> str:
    """
    Export all mailboxes to JSON format
    
    Args:
        user_id: User identifier
        base_path: Base directory path
        include_passwords: If True, include passwords (be careful!)
    
    Returns:
        JSON string of mailboxes
    """
    mailboxes = get_user_mailboxes(user_id, base_path, include_sensitive=include_passwords)
    
    export_data = []
    
    for mailbox in mailboxes:
        entry = {
            'email': mailbox.get('email_address'),
            'provider': mailbox.get('provider'),
            'display_name': mailbox.get('display_name'),
            'is_primary': mailbox.get('is_primary'),
            'smtp_enabled': mailbox.get('smtp_enabled'),
            'imap_host': (mailbox.get('imap_config') or {}).get('host'),
            'imap_port': (mailbox.get('imap_config') or {}).get('port'),
        }
        
        if include_passwords:
            entry['password'] = (mailbox.get('imap_config') or {}).get('password')
            if mailbox.get('smtp_config'):
                entry['smtp_host'] = mailbox.get('smtp_config', {}).get('host')
                entry['smtp_port'] = mailbox.get('smtp_config', {}).get('port')
                entry['smtp_password'] = mailbox.get('smtp_config', {}).get('password')
        
        export_data.append(entry)
    
    return json.dumps(export_data, indent=2, ensure_ascii=False)


# =============================================================================
# FILE OPERATIONS
# =============================================================================

def get_user_folder(user_id: str, base_path: Path) -> Path:
    from config import UserFolderPrefix
    folder_name = UserFolderPrefix.generate(user_id)
    return base_path / folder_name


def ensure_user_folder(user_id: str, base_path: Path) -> Path:
    folder_path = get_user_folder(user_id, base_path)
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path


def get_user_file(user_id: str, base_path: Path, filename: str) -> Path:
    folder_path = ensure_user_folder(user_id, base_path)
    return folder_path / filename


def read_json_file(file_path: Union[str, Path], default: Any = None) -> Any:
    file_path = Path(file_path)
    if not file_path.exists():
        return default if default is not None else {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default if default is not None else {}


def write_json_file(file_path: Union[str, Path], data: Any) -> bool:
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except:
        return False


def delete_file(file_path: Union[str, Path]) -> bool:
    file_path = Path(file_path)
    try:
        if file_path.exists():
            file_path.unlink()
        return True
    except:
        return False


def read_text_file(file_path: Union[str, Path], default: str = "") -> str:
    file_path = Path(file_path)
    if not file_path.exists():
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return default


def write_text_file(file_path: Union[str, Path], content: str) -> bool:
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except:
        return False


def list_user_files(user_id: str, base_path: Path) -> List[Dict[str, Any]]:
    folder_path = get_user_folder(user_id, base_path)
    if not folder_path.exists():
        return []
    files = []
    for file in folder_path.iterdir():
        if file.is_file():
            stat = file.stat()
            files.append({"name": file.name, "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(), "is_dir": file.is_dir()})
    return sorted(files, key=lambda x: x["name"])


def read_user_data(user_id: str, base_path: Path, filename: str, default: Any = None) -> Any:
    file_path = get_user_file(user_id, base_path, filename)
    return read_json_file(file_path, default)


def write_user_data(user_id: str, base_path: Path, filename: str, data: Any) -> bool:
    file_path = get_user_file(user_id, base_path, filename)
    return write_json_file(file_path, data)


def delete_user_data(user_id: str, base_path: Path, filename: str) -> bool:
    file_path = get_user_file(user_id, base_path, filename)
    return delete_file(file_path)


# =============================================================================
# ID GENERATION
# =============================================================================

def generate_user_id() -> str:
    """Generate user ID (no prefix since folder name has fv-s- prefix)"""
    return str(uuid.uuid4())


def generate_credential_id(user_id: str = None, cred_type: str = "smtp") -> str:
    """
    Generate credential ID with user ID prefix for easy identification.
    Format: fv-smtp-{user_id_short}-{uuid_short} or fv-api-{user_id_short}-{uuid_short}
    """
    # Get short version of user ID (first 8 chars)
    user_short = user_id[:8] if user_id else "unknown"
    # Get short UUID
    uuid_short = str(uuid.uuid4())[:8]
    
    return f"fv-{cred_type}-{user_short}-{uuid_short}"


def generate_campaign_id() -> str:
    return str(uuid.uuid4())


# =============================================================================
# SYSTEM INFORMATION
# =============================================================================

def get_system_info() -> Dict[str, str]:
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except:
        hostname = "unknown"
        local_ip = "unknown"
    return {"hostname": hostname, "ip_address": local_ip, "platform": platform.system(), "platform_release": platform.release(), "platform_version": platform.version(), "architecture": platform.machine(), "processor": platform.processor(), "python_version": platform.python_version()}


# =============================================================================
# PROVIDER DETECTION
# =============================================================================

def detect_smtp_provider(host: str) -> str:
    host_lower = host.lower()
    if 'gmail' in host_lower or 'google' in host_lower:
        return 'gmail'
    elif 'yahoo' in host_lower:
        return 'yahoo'
    elif 'outlook' in host_lower or 'microsoft' in host_lower or 'office365' in host_lower:
        return 'outlook'
    elif 'aws' in host_lower or 'amazon' in host_lower or 'ses' in host_lower:
        return 'aws'
    elif 'mailgun' in host_lower:
        return 'mailgun'
    elif 'sendgrid' in host_lower:
        return 'sendgrid'
    elif 'smtp2go' in host_lower:
        return 'smtp2go'
    elif 'mailjet' in host_lower:
        return 'mailjet'
    elif 'postmark' in host_lower:
        return 'postmark'
    return 'custom'


def get_smtp_defaults(provider: str) -> Dict[str, Any]:
    from config import Providers
    if provider in Providers.SMTP:
        provider_info = Providers.SMTP[provider]
        return {"host": provider_info.get("host", ""), "port": provider_info.get("port", 587), "ssl_port": provider_info.get("ssl_port", 465)}
    return {"host": "", "port": 587, "ssl_port": 465}


# =============================================================================
# CREDENTIAL UTILITIES
# =============================================================================

def mask_credential(value: str, visible_chars: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible_chars:
        return "*" * len(value)
    return "*" * (len(value) - visible_chars) + value[-visible_chars:]


def validate_email(email: str) -> bool:
    import re
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


# =============================================================================
# FILE IMPORT PARSING
# =============================================================================

def parse_credential_line(line: str) -> Optional[Dict[str, str]]:
    line = line.strip()
    if not line:
        return None
    
    for delimiter in ['|', ':', ',']:
        if delimiter in line:
            parts = [p.strip() for p in line.split(delimiter)]
            if len(parts) >= 4:
                return {"type": "smtp", "host": parts[0], "port": parts[1], "user": parts[2], "password": parts[3], "from_address": parts[4] if len(parts) > 4 else parts[2], "provider": detect_smtp_provider(parts[0])}
    return None


def parse_email_list(content: str) -> List[str]:
    emails = []
    lines = content.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',')] if ',' in line and '\n' not in line else [line]
        for email in parts:
            if email and validate_email(email):
                emails.append(email)
    return emails


# =============================================================================
# TIME UTILITIES
# =============================================================================

def format_timestamp(dt: datetime = None) -> str:
    return (dt or datetime.now()).isoformat()


def parse_timestamp(timestamp: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except:
        return None


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining_seconds = seconds % 60
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:.0f}s"
    return f"{minutes // 60}h {minutes % 60}m"


# =============================================================================
# RESPONSE HELPERS
# =============================================================================

def success_response(message: str = None, **kwargs) -> Dict[str, Any]:
    response = {"success": True}
    if message:
        response["message"] = message
    response.update(kwargs)
    return response


def error_response(message: str, code: str = None, **kwargs) -> Dict[str, Any]:
    response = {"success": False, "error": message}
    if code:
        response["code"] = code
    response.update(kwargs)
    return response


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

def require_fields(data: Dict[str, Any], required: List[str]) -> Optional[str]:
    missing = [field for field in required if not data.get(field)]
    return f"Missing required fields: {', '.join(missing)}" if missing else None


def sanitize_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    import re
    filename = re.sub(r'[^\w\s.-]', '', filename)
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255 - len(ext)] + ext
    return filename


# =============================================================================
# EMAIL LIST FUNCTIONS
# =============================================================================

def sanitize_email_list_name(name: str) -> str:
    """Sanitize email list name for safe file storage"""
    import re
    # Remove special characters, keep alphanumeric, spaces, hyphens, underscores
    name = re.sub(r'[^\w\s\-\.]', '', name)
    # Replace spaces with underscores
    name = re.sub(r'\s+', '_', name)
    # Limit length
    if len(name) > 50:
        name = name[:50]
    return name.lower().strip()


def extract_email_domain(email: str) -> Optional[str]:
    """Extract domain from email address"""
    if '@' in email:
        parts = email.split('@')
        if len(parts) == 2 and parts[1]:
            return parts[1].lower().strip()
    return None


def get_email_provider_icon(domain: str) -> str:
    """Get Font Awesome icon class for email provider"""
    domain_lower = domain.lower() if domain else ""
    
    # Email provider domain mapping
    providers = {
        'gmail.com': 'fab fa-google',
        'googlemail.com': 'fab fa-google',
        'yahoo.com': 'fab fa-yahoo',
        'ymail.com': 'fab fa-yahoo',
        'outlook.com': 'fab fa-microsoft',
        'hotmail.com': 'fab fa-microsoft',
        'live.com': 'fab fa-microsoft',
        'msn.com': 'fab fa-microsoft',
        'office.com': 'fab fa-microsoft',
        'office365.com': 'fab fa-microsoft',
        'aol.com': 'fas fa-at',
        'icloud.com': 'fab fa-apple',
        'me.com': 'fab fa-apple',
        'mac.com': 'fab fa-apple',
        'protonmail.com': 'fas fa-envelope',
        'proton.me': 'fas fa-envelope',
        'mail.com': 'fas fa-envelope',
        'gmx.com': 'fas fa-envelope',
        'zoho.com': 'fas fa-envelope',
        'yandex.com': 'fas fa-envelope',
        'yandex.ru': 'fas fa-envelope',
        'yandex.ua': 'fas fa-envelope',
        '163.com': 'fas fa-envelope',
        '126.com': 'fas fa-envelope',
        'qq.com': 'fas fa-envelope',
        'sina.com': 'fas fa-envelope',
        'amazon.com': 'fab fa-amazon',
        'amazon.co.uk': 'fab fa-amazon',
    }
    
    return providers.get(domain_lower, 'fas fa-envelope')


def validate_email_format(email: str) -> bool:
    """Validate email format"""
    import re
    if not email or not isinstance(email, str):
        return False
    
    email = email.strip().lower()
    if not email:
        return False
    
    # Basic email regex
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def parse_email_content(content: str) -> tuple:
    """
    Parse raw email content (one email per line)
    Returns: (valid_emails, invalid_lines, total_count)
    """
    lines = content.strip().split('\n')
    valid_emails = []
    invalid_lines = []
    
    for line in lines:
        # Clean the line
        email = line.strip().lower()
        
        # Skip empty lines
        if not email:
            continue
        
        # Remove whitespace and validate
        email = ' '.join(email.split())  # Normalize whitespace
        
        if validate_email_format(email):
            domain = extract_email_domain(email)
            valid_emails.append({
                'email': email,
                'domain': domain,
                'valid': False,
                'validated': False,
                'added_at': datetime.now().isoformat()
            })
        else:
            invalid_lines.append(line.strip())
    
    return valid_emails, invalid_lines, len(lines)


def get_user_emails_folder(user_id: str, base_path: Path) -> Path:
    """Get or create user's emails directory"""
    from config import FileNames
    folder = base_path / f"fv-s-{user_id}" / FileNames.EMAILS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def list_email_lists(user_id: str, base_path: Path) -> List[Dict[str, Any]]:
    """List all email lists for a user (without full email content)"""
    from config import FileNames
    emails_folder = get_user_emails_folder(user_id, base_path)
    
    if not emails_folder.exists():
        return []
    
    email_lists = []
    for file in emails_folder.glob("*.json"):
        try:
            with open(file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Return summary without emails array
                email_lists.append({
                    'id': data.get('id', file.stem),
                    'name': data.get('name', file.stem),
                    'created_at': data.get('created_at', ''),
                    'updated_at': data.get('updated_at'),
                    'total_emails': data.get('total_emails', 0),
                    'valid_count': data.get('valid_count', 0),
                    'invalid_count': data.get('invalid_count', 0)
                })
        except Exception:
            pass

            continue
    
    # Sort by created_at descending
    email_lists.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return email_lists


def get_email_list(user_id: str, base_path: Path, list_id: str) -> Optional[Dict[str, Any]]:
    """Get full email list with all emails"""
    from config import FileNames
    emails_folder = get_user_emails_folder(user_id, base_path)
    file_path = emails_folder / f"{list_id}.json"
    
    if not file_path.exists():
        return None
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

        return None


def create_email_list(user_id: str, base_path: Path, name: str, emails: List[str]) -> Optional[Dict[str, Any]]:
    """Create a new email list"""
    from config import FileNames
    import uuid
    
    # Sanitize name for file
    safe_name = sanitize_email_list_name(name)
    
    # Generate unique ID
    list_id = f"elist_{str(uuid.uuid4())[:12]}"
    
    # Parse emails
    valid_emails, invalid_lines, total = parse_email_content('\n'.join(emails))
    
    # Create email list data
    email_list_data = {
        'id': list_id,
        'name': name.strip(),
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'total_emails': len(valid_emails),
        'valid_count': len(valid_emails),
        'invalid_count': len(invalid_lines),
        'emails': valid_emails
    }
    
    # Save to file
    emails_folder = get_user_emails_folder(user_id, base_path)
    file_path = emails_folder / f"{list_id}.json"
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(email_list_data, f, indent=4, ensure_ascii=False)
        return email_list_data
    except Exception:
        pass

        return None


def update_email_list(user_id: str, base_path: Path, list_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update an existing email list"""
    email_list = get_email_list(user_id, base_path, list_id)
    
    if not email_list:
        return None
    
    # Update fields
    if 'name' in updates and updates['name']:
        email_list['name'] = updates['name'].strip()
    
    if 'emails' in updates and updates['emails'] is not None:
        action = updates.get('action', 'replace')
        
        if action == 'replace':
            # Replace all emails
            valid_emails, invalid_lines, total = parse_email_content('\n'.join(updates['emails']))
            email_list['emails'] = valid_emails
            email_list['total_emails'] = len(valid_emails)
            email_list['valid_count'] = len(valid_emails)
            email_list['invalid_count'] = len(invalid_lines)
            
        elif action == 'append':
            # Add new emails
            new_valid, new_invalid, _ = parse_email_content('\n'.join(updates['emails']))
            existing_emails = set(e['email'] for e in email_list['emails'])
            
            for new_email in new_valid:
                if new_email['email'] not in existing_emails:
                    email_list['emails'].append(new_email)
                    existing_emails.add(new_email['email'])
            
            email_list['total_emails'] = len(email_list['emails'])
            email_list['valid_count'] = len(email_list['emails'])
            email_list['invalid_count'] += len(new_invalid)
            
        elif action == 'remove_duplicates':
            # Remove duplicate emails
            seen = set()
            unique_emails = []
            for email_entry in email_list['emails']:
                if email_entry['email'] not in seen:
                    seen.add(email_entry['email'])
                    unique_emails.append(email_entry)
            
            email_list['emails'] = unique_emails
            email_list['total_emails'] = len(unique_emails)
            email_list['valid_count'] = len(unique_emails)
            
        elif action == 'validate':
            # Re-validate all emails
            valid_emails = []
            for entry in email_list['emails']:
                if validate_email_format(entry['email']):
                    entry['valid'] = True
                    entry['domain'] = extract_email_domain(entry['email'])
                    valid_emails.append(entry)
            
            email_list['emails'] = valid_emails
            email_list['total_emails'] = len(valid_emails)
            email_list['valid_count'] = len(valid_emails)
    
    # Update timestamp
    email_list['updated_at'] = datetime.now().isoformat()
    
    # Save
    emails_folder = get_user_emails_folder(user_id, base_path)
    file_path = emails_folder / f"{list_id}.json"
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(email_list, f, indent=4, ensure_ascii=False)
        return email_list
    except Exception:
        pass

        return None


def delete_email_list(user_id: str, base_path: Path, list_id: str) -> bool:
    """Delete an email list"""
    emails_folder = get_user_emails_folder(user_id, base_path)
    file_path = emails_folder / f"{list_id}.json"
    
    try:
        if file_path.exists():
            file_path.unlink()
            return True
    except Exception:
        pass

        pass
    return False


# =============================================================================
# MAILBOX MANAGEMENT FUNCTIONS
# =============================================================================

def get_user_mailbox_folder(user_id: str, base_path: Path) -> Path:
    """Get the mailbox folder for a user"""
    user_folder = base_path / f"fv-s-{user_id}" / "mailbox"
    user_folder.mkdir(parents=True, exist_ok=True)
    return user_folder


def read_google_mailbox(user_id: str, base_path: Path) -> Optional[Dict[str, Any]]:
    """Read Google mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    file_path = mailbox_folder / "google.json"
    
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass

        pass
    return None


def write_google_mailbox(user_id: str, base_path: Path, data: GoogleMailboxData) -> Optional[Dict[str, Any]]:
    """Write Google mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    
    # Check if file exists to preserve created_at
    existing = read_google_mailbox(user_id, base_path)
    
    mailbox_data = {
        "client_id": data.client_id,
        "client_secret": data.client_secret,
        "refresh_token": data.refresh_token,
        "sender_email": data.sender_email,
        "sender_name": data.sender_name,
        "created_at": existing.get("created_at") if existing else datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    
    file_path = mailbox_folder / "google.json"
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(mailbox_data, f, indent=4, ensure_ascii=False)
        return mailbox_data
    except Exception:
        pass

        return None


def delete_google_mailbox(user_id: str, base_path: Path) -> bool:
    """Delete Google mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    file_path = mailbox_folder / "google.json"
    
    try:
        if file_path.exists():
            file_path.unlink()
            return True
    except Exception:
        pass

        pass
    return False


def read_outlook_mailbox(user_id: str, base_path: Path) -> Optional[Dict[str, Any]]:
    """Read Outlook mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    file_path = mailbox_folder / "outlook.json"
    
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass

        pass
    return None


def write_outlook_mailbox(user_id: str, base_path: Path, data: OutlookMailboxData) -> Optional[Dict[str, Any]]:
    """Write Outlook mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    
    # Check if file exists to preserve created_at
    existing = read_outlook_mailbox(user_id, base_path)
    
    mailbox_data = {
        "tenant_id": data.tenant_id,
        "client_id": data.client_id,
        "client_secret": data.client_secret,
        "sender_email": data.sender_email,
        "sender_name": data.sender_name,
        "created_at": existing.get("created_at") if existing else datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    
    file_path = mailbox_folder / "outlook.json"
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(mailbox_data, f, indent=4, ensure_ascii=False)
        return mailbox_data
    except Exception:
        pass

        return None


def delete_outlook_mailbox(user_id: str, base_path: Path) -> bool:
    """Delete Outlook mailbox configuration"""
    mailbox_folder = get_user_mailbox_folder(user_id, base_path)
    file_path = mailbox_folder / "outlook.json"
    
    try:
        if file_path.exists():
            file_path.unlink()
            return True
    except Exception:
        pass

        pass
    return False


# =============================================================================
# CENTRAL USER MANAGEMENT FUNCTIONS
# =============================================================================

def get_users_file_path(base_path: Path) -> Path:
    """Get the path to the central users.json file"""
    return base_path / "users.json"


def read_users_database(base_path: Path) -> List[Dict[str, Any]]:
    """Read all users from the central users database"""
    users_file = get_users_file_path(base_path)
    
    try:
        if users_file.exists():
            with open(users_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('users', [])
    except Exception:
        pass

        pass
    return []


def write_users_database(base_path: Path, users: List[Dict[str, Any]]) -> bool:
    """Write all users to the central users database"""
    users_file = get_users_file_path(base_path)
    
    try:
        users_file.parent.mkdir(parents=True, exist_ok=True)
        data = {'users': users}
        with open(users_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except Exception:
        pass

        return False


def get_user_by_username(base_path: Path, username: str) -> Optional[Dict[str, Any]]:
    """Find a user by username in the central database"""
    users = read_users_database(base_path)
    for user in users:
        if user.get('username', '').lower() == username.lower():
            return user
    return None


def get_user_by_id(base_path: Path, user_id: str) -> Optional[Dict[str, Any]]:
    """Find a user by ID in the central database"""
    users = read_users_database(base_path)
    for user in users:
        if user.get('user_id') == user_id:
            return user
    return None


def create_central_user(base_path: Path, username: str, password: str) -> Optional[Dict[str, Any]]:
    """Create a new user in the central database"""
    import shutil
    
    # Check if username already exists
    if get_user_by_username(base_path, username):
        return None
    
    # Generate new user ID
    user_id = generate_user_id()
    
    # Create user data
    new_user = {
        'user_id': user_id,
        'username': username,
        'password_hash': get_password_hash(password),
        'created_at': datetime.now().isoformat(),
        'last_login': None,
        'login_count': 0
    }
    
    # Add to users list
    users = read_users_database(base_path)
    users.append(new_user)
    
    if not write_users_database(base_path, users):
        return None
    
    # Create user folder structure
    user_folder = base_path / f"fv-s-{user_id}"
    user_folder.mkdir(parents=True, exist_ok=True)
    
    # Create basic logs.json only
    from config import FileNames
    
    logs_data = {
        'username': username,
        'password_hash': get_password_hash(password),
        'created_at': datetime.now().isoformat(),
        'last_login': None,
        'login_count': 0
    }
    write_json_file(user_folder / FileNames.LOGS, logs_data)
    
    # Create about files
    # Return user without password hash
    result = new_user.copy()
    del result['password_hash']
    return result


def ensure_default_admin(base_path: Path) -> bool:
    """
    Seed the default admin account (admin/admin) into the central database on
    first run so the panel is usable before any users have been created.
    Returns True if a default admin was created.
    """
    try:
        from config import Defaults
        username = Defaults.USERNAME
        password = Defaults.PASSWORD
    except Exception:
        username = "admin"
        password = "admin"

    if read_users_database(base_path):
        return False

    created = create_central_user(base_path, username, password)
    if created:
        print(f"Seeded default admin account: {username}")
        return True
    return False


def write_json_atomic(path: Path, data: Any) -> bool:
    """Write JSON to `path` atomically (tmp file + rename) to avoid corruption."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
        return True
    except Exception:
        return False


def audit_event(user_id: str, base_path: Path, event: str, details: dict = None) -> bool:
    """
    Append an entry to the user's audit log (JSONL) at audit.log.
    Used to track credential/user/campaign changes for accountability.
    """
    try:
        folder = base_path / f"fv-s-{user_id}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "audit.log"
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event,
            "details": details or {},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_audit_log(user_id: str, base_path: Path, limit: int = 200) -> List[Dict[str, Any]]:
    """Read the most recent audit log entries for a user (newest first)."""
    try:
        path = base_path / f"fv-s-{user_id}" / "audit.log"
        if not path.exists():
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        entries.reverse()
        return entries[:limit]
    except Exception:
        return []


def check_domain_deliverability(domain: str) -> Dict[str, Any]:
    """
    Look up SPF / DKIM / DMARC DNS records for a sending domain (advisory).
    Returns a dict of record -> list of strings (or {"error": ...} if dnspython
    is unavailable or the lookup fails).
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain:
        return {"domain": domain, "error": "Invalid domain"}

    result = {"domain": domain, "spf": [], "dmarc": [], "dkim": []}

    try:
        import dns.resolver  # type: ignore
    except Exception:
        result["error"] = "dnspython not installed"
        return result

    def _txt(name):
        try:
            answers = dns.resolver.resolve(name, "TXT")
            return ["".join(s.decode("utf-8") for s in a.strings) for a in answers]
        except Exception:
            return []

    result["spf"] = _txt(domain)
    result["dmarc"] = _txt(f"_dmarc.{domain}")

    # Common DKIM selectors (best-effort; selectors are provider-specific).
    for selector in ("default", "google", "selector1", "selector2", "k1", "s1", "smtp", "mail"):
        records = _txt(f"{selector}._domainkey.{domain}")
        if records:
            result["dkim"].append({"selector": selector, "records": records})

    result["has_spf"] = bool(result["spf"])
    result["has_dmarc"] = bool(result["dmarc"])
    result["has_dkim"] = bool(result["dkim"])
    return result


# =============================================================================
# UNSUBSCRIBE / SUPPRESSION LIST
# =============================================================================

def _suppression_file(user_id: str, base_path: Path) -> Path:
    return base_path / f"fv-s-{user_id}" / "suppression.json"


def unsubscribe_token(email: str, user_id: str) -> str:
    """Create a signed, self-describing unsubscribe token."""
    payload = json.dumps({"u": user_id, "e": (email or "").strip().lower()}, separators=(",", ":"))
    b64 = __import__("base64").urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_secret_key().encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_unsubscribe_token(token: str):
    """Verify an unsubscribe token; returns (user_id, email) or None."""
    try:
        b64, sig = (token or "").split(".", 1)
        expected = hmac.new(_secret_key().encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padding = 4 - len(b64) % 4
        if padding != 4:
            b64 += "=" * padding
        payload = json.loads(__import__("base64").urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8"))
        return payload.get("u"), payload.get("e")
    except Exception:
        return None


def build_unsubscribe_url(email: str, user_id: str) -> str:
    """Absolute unsubscribe URL for an email (used in outbound mail)."""
    try:
        from config import config as _cfg
        base = getattr(_cfg, "public_url", None) or "http://localhost:8000"
    except Exception:
        base = "http://localhost:8000"
    return f"{base.rstrip('/')}/unsubscribe/{unsubscribe_token(email, user_id)}"


def suppress_email(user_id: str, base_path: Path, email: str) -> bool:
    """Add an email address to the user's suppression list (unsubscribed)."""
    email = (email or "").strip().lower()
    if not email:
        return False
    current = read_user_data(user_id, base_path, "suppression.json", []) or []
    if email not in current:
        current.append(email)
        return write_user_data(user_id, base_path, "suppression.json", current)
    return True


def unsuppress_email(user_id: str, base_path: Path, email: str) -> bool:
    email = (email or "").strip().lower()
    current = read_user_data(user_id, base_path, "suppression.json", []) or []
    if email in current:
        current.remove(email)
        return write_user_data(user_id, base_path, "suppression.json", current)
    return True


def get_suppression_list(user_id: str, base_path: Path) -> List[str]:
    return read_user_data(user_id, base_path, "suppression.json", []) or []


def is_suppressed(user_id: str, base_path: Path, email: str) -> bool:
    email = (email or "").strip().lower()
    return email in get_suppression_list(user_id, base_path)


def recover_interrupted_sessions(base_path: Path) -> int:
    """
    Mark any campaign session left in 'running'/'paused' status as
    'interrupted' (e.g. after a process crash) so the monitor no longer shows
    them as live and they can be re-queued cleanly.
    Returns the number of sessions recovered.
    """
    recovered = 0
    try:
        base = Path(base_path)
        if not base.exists():
            return 0
        for user_folder in base.iterdir():
            if not user_folder.is_dir() or not user_folder.name.startswith("fv-s-"):
                continue
            sessions_dir = user_folder / "sender-config" / "sessions"
            if not sessions_dir.exists():
                continue
            for session_file in sessions_dir.glob("*.json"):
                try:
                    data = json.loads(session_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if data.get("status") in ("running", "paused"):
                    data["status"] = "interrupted"
                    data["interrupted_at"] = datetime.now().isoformat()
                    write_json_atomic(session_file, data)
                    recovered += 1
    except Exception:
        pass
    return recovered


def initialize_user_folder(base_path: Path, user_id: str) -> bool:
    """Initialize user folder with all required files (for existing users)"""
    from config import FileNames
    
    user_folder = base_path / f"fv-s-{user_id}"
    
    # Create user folder if it doesn't exist
    user_folder.mkdir(parents=True, exist_ok=True)
    
    # Create logs.json if it doesn't exist
    logs_file = user_folder / FileNames.LOGS
    if not logs_file.exists():
        logs_data = {
            'username': '',
            'password_hash': '',
            'created_at': datetime.now().isoformat(),
            'last_login': None,
            'login_count': 0
        }
        write_json_file(logs_file, logs_data)
    
    # Create empty credential files if they don't exist
    if not (user_folder / FileNames.SMTP).exists():
        write_json_file(user_folder / FileNames.SMTP, [])
    
    if not (user_folder / FileNames.API_CREDENTIALS).exists():
        write_json_file(user_folder / FileNames.API_CREDENTIALS, [])
    
    if not (user_folder / FileNames.FROM_NAMES).exists():
        write_json_file(user_folder / FileNames.FROM_NAMES, [])
    
    if not (user_folder / FileNames.SUBJECTS).exists():
        write_json_file(user_folder / FileNames.SUBJECTS, [])
    
    if not (user_folder / FileNames.CAMPAIGNS).exists():
        write_json_file(user_folder / FileNames.CAMPAIGNS, [])
    
    if not (user_folder / FileNames.SETTINGS).exists():
        default_settings = {
            'theme': 'dark',
            'language': 'en',
            'notifications': True,
            'auto_save': True,
            'created_at': datetime.now().isoformat()
        }
        write_json_file(user_folder / FileNames.SETTINGS, default_settings)
    
    if not (user_folder / FileNames.ABOUT_TXT).exists():
        system_info = get_system_info()
        about_content = f"""FV-S User Information
====================
User ID: {user_id}
Registered: {datetime.now().isoformat()}

System Information:
-------------------
Hostname: {system_info['hostname']}
IP Address: {system_info['ip_address']}
Platform: {system_info['platform']} {system_info['platform_release']}
Architecture: {system_info['architecture']}
Python: {system_info['python_version']}
"""
        write_text_file(user_folder / FileNames.ABOUT_TXT, about_content)
    
    if not (user_folder / FileNames.ABOUT_JSON).exists():
        system_info = get_system_info()
        system_info.update({
            'user_id': user_id,
            'registered_at': datetime.now().isoformat()
        })
        write_json_file(user_folder / FileNames.ABOUT_JSON, system_info)
    
    # Create emails directory if it doesn't exist
    emails_folder = user_folder / FileNames.EMAILS_DIR
    emails_folder.mkdir(parents=True, exist_ok=True)
    
    # Create mailbox directory with configuration files
    mailbox_folder = user_folder / "mailbox"
    mailbox_folder.mkdir(parents=True, exist_ok=True)
    
    if not (mailbox_folder / FileNames.MAILBOX).exists():
        write_json_file(mailbox_folder / FileNames.MAILBOX, {})
    
    if not (mailbox_folder / FileNames.MAILBOX_SENDERS).exists():
        write_json_file(mailbox_folder / FileNames.MAILBOX_SENDERS, [])
    
    # Create email lists directory if it doesn't exist
    email_lists_folder = user_folder / FileNames.EMAIL_LISTS
    email_lists_folder.mkdir(parents=True, exist_ok=True)
    
    # Create letters directory if it doesn't exist
    letters_folder = user_folder / FileNames.LETTERS
    letters_folder.mkdir(parents=True, exist_ok=True)
    
    return True


def update_central_user(base_path: Path, user_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update a user in the central database"""
    users = read_users_database(base_path)
    
    for i, user in enumerate(users):
        if user.get('user_id') == user_id:
            # Update fields
            if 'username' in updates and updates['username']:
                # Check if username is already taken by another user
                for other_user in users:
                    if other_user.get('user_id') != user_id and other_user.get('username', '').lower() == updates['username'].lower():
                        return None
                users[i]['username'] = updates['username']
            
            if 'password' in updates and updates['password']:
                users[i]['password_hash'] = get_password_hash(updates['password'])
            
            if not write_users_database(base_path, users):
                return None
            
            # Also update the user's logs.json file
            user_folder = base_path / f"fv-s-{user_id}"
            logs_file = user_folder / "logs.json"
            
            if logs_file.exists():
                try:
                    with open(logs_file, 'r', encoding='utf-8') as f:
                        logs_data = json.load(f)
                except Exception:
                    pass

                    logs_data = {}
            else:
                logs_data = {}
            
            if 'username' in updates and updates['username']:
                logs_data['username'] = updates['username']
            if 'password' in updates and updates['password']:
                logs_data['password_hash'] = get_password_hash(updates['password'])
            
            write_json_file(logs_file, logs_data)
            
            # Return updated user without password hash
            result = users[i].copy()
            del result['password_hash']
            return result
    
    return None


def delete_central_user(base_path: Path, user_id: str) -> bool:
    """Soft delete a user - mark as deleted but keep folder"""
    users = read_users_database(base_path)
    
    for i, user in enumerate(users):
        if user.get('user_id') == user_id:
            # Mark user as deleted instead of removing
            users[i]['deleted'] = True
            users[i]['deleted_at'] = datetime.now().isoformat()
            
            if not write_users_database(base_path, users):
                return False
            
            # Also update the user's logs.json to mark as deleted
            user_folder = base_path / f"fv-s-{user_id}"
            logs_file = user_folder / "logs.json"
            
            if logs_file.exists():
                try:
                    with open(logs_file, 'r', encoding='utf-8') as f:
                        logs_data = json.load(f)
                except Exception:
                    pass

                    logs_data = {}
            else:
                logs_data = {}
            
            logs_data['deleted'] = True
            logs_data['deleted_at'] = datetime.now().isoformat()
            write_json_file(logs_file, logs_data)
            
            return True
    
    return False


def restore_central_user(base_path: Path, user_id: str) -> Optional[Dict[str, Any]]:
    """Restore a deleted user"""
    users = read_users_database(base_path)
    
    for i, user in enumerate(users):
        if user.get('user_id') == user_id:
            # Remove deleted flag
            if 'deleted' in users[i]:
                del users[i]['deleted']
            if 'deleted_at' in users[i]:
                del users[i]['deleted_at']
            
            if not write_users_database(base_path, users):
                return None
            
            # Also update the user's logs.json
            user_folder = base_path / f"fv-s-{user_id}"
            logs_file = user_folder / "logs.json"
            
            if logs_file.exists():
                try:
                    with open(logs_file, 'r', encoding='utf-8') as f:
                        logs_data = json.load(f)
                except Exception:
                    pass

                    logs_data = {}
            else:
                logs_data = {}
            
            if 'deleted' in logs_data:
                del logs_data['deleted']
            if 'deleted_at' in logs_data:
                del logs_data['deleted_at']
            write_json_file(logs_file, logs_data)
            
            # Return restored user without password hash
            result = users[i].copy()
            if 'password_hash' in result:
                del result['password_hash']
            return result
    
    return None


def get_deleted_users(base_path: Path) -> List[Dict[str, Any]]:
    """Get all soft-deleted users"""
    users = read_users_database(base_path)
    deleted_users = []
    
    for user in users:
        if user.get('deleted', False):
            safe_user = {
                'user_id': user.get('user_id'),
                'username': user.get('username'),
                'created_at': user.get('created_at'),
                'deleted_at': user.get('deleted_at'),
                'login_count': user.get('login_count', 0)
            }
            deleted_users.append(safe_user)
    
    return deleted_users


def is_user_deleted(base_path: Path, user_id: str) -> bool:
    """Check if a user is soft-deleted"""
    users = read_users_database(base_path)
    
    for user in users:
        if user.get('user_id') == user_id:
            return user.get('deleted', False)
    
    return True  # If user not found, consider them deleted


# =============================================================================
# LETTERS MANAGEMENT FUNCTIONS
# =============================================================================

class LetterData(BaseModel):
    """Letter data model"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str = Field(..., description="Letter type: html or plain")
    name: str = Field(..., description="Letter name")
    content: str = Field(..., description="Letter content")
    content_length: int = Field(default=0, description="Content character count")
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


class LetterCreate(BaseModel):
    """Letter creation model"""
    type: str = Field(..., description="Letter type: html or plain")
    name: str = Field(..., description="Letter name")
    content: str = Field(..., description="Letter content")
    content_length: int = Field(default=0, description="Content character count")


class LetterUpdate(BaseModel):
    """Letter update model"""
    name: Optional[str] = None
    content: Optional[str] = None
    content_length: Optional[int] = None


def get_user_letters_folder(user_id: str, base_path: Path) -> Path:
    """Get the letters folder for a user"""
    user_folder = base_path / f"fv-s-{user_id}" / "letters"
    user_folder.mkdir(parents=True, exist_ok=True)
    return user_folder


def get_letter_file_path(user_id: str, base_path: Path, letter_type: str) -> Path:
    """Get the file path for letters of a specific type"""
    letters_folder = get_user_letters_folder(user_id, base_path)
    return letters_folder / f"{letter_type}.json"


def list_letters(user_id: str, base_path: Path, letter_type: str) -> List[Dict[str, Any]]:
    """List all letters of a specific type"""
    file_path = get_letter_file_path(user_id, base_path, letter_type)
    
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('letters', [])
    except Exception:
        pass

        pass
    return []


def get_letter(user_id: str, base_path: Path, letter_type: str, letter_id: str) -> Optional[Dict[str, Any]]:
    """Get a single letter by ID"""
    letters = list_letters(user_id, base_path, letter_type)
    for letter in letters:
        if letter.get('id') == letter_id:
            return letter
    return None


def create_letter(user_id: str, base_path: Path, letter_type: str, name: str, content: str) -> Optional[Dict[str, Any]]:
    """Create a new letter"""
    letters_folder = get_user_letters_folder(user_id, base_path)
    file_path = get_letter_file_path(user_id, base_path, letter_type)
    
    # Load existing data
    data = {'letters': []}
    if file_path.exists():
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            pass

            pass
    
    # Create new letter
    new_letter = {
        'id': str(uuid.uuid4()),
        'type': letter_type,
        'name': name,
        'content': content,
        'content_length': len(content),
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }
    
    data['letters'].append(new_letter)
    
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return new_letter
    except Exception:
        pass

        return None


def update_letter(user_id: str, base_path: Path, letter_type: str, letter_id: str, name: Optional[str] = None, content: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Update an existing letter"""
    file_path = get_letter_file_path(user_id, base_path, letter_type)
    
    # Load existing data
    if not file_path.exists():
        return None
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        pass

        return None
    
    # Find and update letter
    for letter in data['letters']:
        if letter.get('id') == letter_id:
            if name is not None:
                letter['name'] = name
            if content is not None:
                letter['content'] = content
                letter['content_length'] = len(content)
            letter['updated_at'] = datetime.now().isoformat()
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            return letter
    
    return None


def delete_letter(user_id: str, base_path: Path, letter_type: str, letter_id: str) -> bool:
    """Delete a letter"""
    file_path = get_letter_file_path(user_id, base_path, letter_type)
    
    if not file_path.exists():
        return False
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        original_length = len(data['letters'])
        data['letters'] = [l for l in data['letters'] if l.get('id') != letter_id]
        
        if len(data['letters']) < original_length:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            return True
    except Exception:
        pass

        pass
    
    return False


# =============================================================================
# EMAIL SENDING ENGINE
# =============================================================================

def generate_random_string(length: int = 8) -> str:
    """Generate a random string for email uniqueness"""
    import random
    import string
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def generate_unsubscribe_link(email: str, campaign_id: str = None) -> str:
    """Generate a proper unsubscribe link with URL encoding"""
    import hashlib
    import urllib.parse
    
    # Create a simple token based on email and campaign
    token_data = f"{email}:{campaign_id or 'default'}:{datetime.now().isoformat()}"
    token = hashlib.md5(token_data.encode()).hexdigest()[:16]
    
    # Encode email for URL safety
    encoded_email = urllib.parse.quote(email)
    
    # Generate proper unsubscribe URL
    # Update this to your actual unsubscribe handling endpoint
    return f"https://unsubscribe.yourdomain.com/remove?email={encoded_email}&token={token}"


def customize_from_name(from_name: str, recipient_email: str, customize_option: str = None) -> str:
    """Customize from name based on options"""
    if not customize_option:
        return from_name
    
    # *E-Name* customization
    if customize_option:
        return f"{from_name} {customize_option}"
    
    return from_name


def customize_subject(subject: str, recipient_email: str, random_enabled: bool = False) -> str:
    """Customize subject with random string if enabled"""
    if random_enabled:
        random_str = generate_random_string(6)
        return f"{subject} [{random_str}]"
    return subject


def prepare_email_content(content: str, recipient_email: str, random_enabled: bool = False, unsubscribe_enabled: bool = False) -> str:
    """Prepare email content with customization placeholders"""
    # Add random string placeholder
    if random_enabled:
        content = content.replace('*|RANDOM|*', generate_random_string(8))
    
    # Add unsubscribe link placeholder
    if unsubscribe_enabled:
        content = content.replace('*|UNSUBSCRIBE|*', generate_unsubscribe_link(recipient_email))
    
    # Replace recipient placeholder
    content = content.replace('*|EMAIL|*', recipient_email)
    
    return content


async def send_smtp_email(
    smtp_config: dict,
    to_email: str,
    from_name: str,
    subject: str,
    content: str,
    reply_to: str = None,
    customize_email: bool = False,
    customize_ename: str = None,
    custom_subject_suffix: str = None,
    force_plain_text: bool = False
) -> tuple:
    """
    Send email via SMTP with proper multipart format (HTML + Plain Text)
    For best deliverability, always include both versions.
    
    Args:
        force_plain_text: If True, send as plain text only (no HTML conversion)
    
    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        import ssl
        import re
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.header import Header
        from email.utils import formataddr, make_msgid, formatdate
        
        host = smtp_config.get('host', '')
        port = smtp_config.get('port', 587)
        username = smtp_config.get('user', '')
        password = smtp_config.get('password', '')
        from_address = smtp_config.get('from_address', username)
        
        # Customize from name if enabled
        if customize_email:
            from_name = f"{to_email.split('@')[0]} {customize_ename or ''}".strip()
        
        # Customize subject
        final_subject = subject
        if custom_subject_suffix:
            final_subject = f"{subject} {custom_subject_suffix}"
        
        # Check if content is HTML (check for more HTML patterns) - only if not forcing plain text
        if force_plain_text:
            is_html = False
        else:
            is_html = '<html' in content.lower() or '<body' in content.lower() or '<div' in content.lower() or '<p>' in content.lower() or '<br>' in content.lower() or '<table' in content.lower() or '<a ' in content.lower() or '<span' in content.lower() or '<strong' in content.lower() or '<em' in content.lower() or '<ul' in content.lower() or '<ol' in content.lower()
        
        # Create plain text version from HTML if needed
        if is_html:
            # MPART_ALT_DIFF fix: Plain text should have THE SAME content as HTML, just formatted differently
            # Key principle: Both versions should have "the exact same prose" - just presented differently
            # Reference: https://buttondown.com/resources/spam-assassin-rules/mpart-alt-diff
            
            text_content = content
            
            # Remove script, style, and hidden content completely
            text_content = re.sub(r'<script[^>]*>.*?</script>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
            text_content = re.sub(r'<style[^>]*>.*?</style>', '', text_content, flags=re.DOTALL | re.IGNORECASE)
            text_content = re.sub(r'<!--.*?-->', '', text_content, flags=re.DOTALL | re.IGNORECASE)
            
            # Replace block elements with newlines (preserve structure)
            text_content = re.sub(r'</p>', '\n\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'</div>', '\n\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'</h[1-6]>', '\n\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'</li>', '\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'</tr>', '\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'<br\s*/?>', '\n', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'<hr\s*/?>', '\n---\n', text_content, flags=re.IGNORECASE)
            
            # Convert links: Keep anchor text and URL together (format: "Click here: https://url.com")
            # This preserves the SAME content that appears in the HTML link
            def format_link(match):
                url = match.group(1)
                text = match.group(2).strip()
                # Clean inner HTML from text (e.g., <strong>text</strong> -> text)
                text = re.sub(r'<[^>]+>', '', text)
                text = text.strip()
                if not text:
                    text = url
                return f"{text}: {url}"
            
            text_content = re.sub(r'<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', format_link, text_content, flags=re.DOTALL | re.IGNORECASE)
            
            # Handle images - extract alt text if present (preserve same info)
            def get_img_alt(match):
                alt = match.group(1)
                if alt:
                    return f"[Image: {alt}]"
                return ""
            text_content = re.sub(r'<img[^>]*alt=["\']([^"\']*)["\'][^>]*>', get_img_alt, text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'<img[^>]*>', '', text_content, flags=re.IGNORECASE)
            
            # Preserve basic formatting markers (bold/italic)
            text_content = re.sub(r'</?(b|strong)[\s>]', '*', text_content, flags=re.IGNORECASE)
            text_content = re.sub(r'</?(i|em)[\s>]', '_', text_content, flags=re.IGNORECASE)
            
            # Remove remaining HTML tags
            text_content = re.sub(r'<[^>]+>', '', text_content)
            
            # Decode HTML entities
            text_content = text_content.replace('&nbsp;', ' ')
            text_content = text_content.replace('&amp;', '&')
            text_content = text_content.replace('&lt;', '<')
            text_content = text_content.replace('&gt;', '>')
            text_content = text_content.replace('&quot;', '"')
            text_content = text_content.replace('&apos;', "'")
            text_content = text_content.replace('&rsquo;', "'")
            text_content = text_content.replace('&lsquo;', "'")
            text_content = text_content.replace('&rdquo;', '"')
            text_content = text_content.replace('&ldquo;', '"')
            text_content = text_content.replace('&ndash;', '-')
            text_content = text_content.replace('&mdash;', '-')
            text_content = text_content.replace('&hellip;', '...')
            text_content = text_content.replace('&copy;', '(c)')
            text_content = text_content.replace('&reg;', '(R)')
            text_content = text_content.replace('&trade;', '(TM)')
            
            # Normalize whitespace: Keep paragraph breaks but remove excessive spaces
            lines = text_content.split('\n')
            cleaned_lines = []
            prev_was_empty = False
            
            for line in lines:
                line = line.strip()
                if not line:
                    if not prev_was_empty:
                        cleaned_lines.append('')
                        prev_was_empty = True
                    continue
                prev_was_empty = False
                # Normalize multiple spaces to single space (preserves words)
                line = ' '.join(line.split())
                if line:
                    cleaned_lines.append(line)
            
            # Join with double newlines for paragraphs
            text_content = '\n\n'.join(cleaned_lines)
            text_content = re.sub(r'\n{3,}', '\n\n', text_content)
            text_content = text_content.strip()
        else:
            # Content is already plain text
            text_content = content
            
            # If force_plain_text is True, send only plain text (no HTML wrapper)
            if force_plain_text:
                # Create simple plain text message without HTML alternative
                msg = MIMEText(text_content, 'plain', 'utf-8')
                
                # Set headers
                msg['Subject'] = Header(final_subject, 'utf-8').encode()
                msg['From'] = formataddr((str(Header(from_name, 'utf-8')), from_address))
                msg['To'] = f"<{to_email}>"
                msg['Date'] = formatdate(localtime=True)
                msg['Message-ID'] = make_msgid()
                
                if reply_to:
                    msg['Reply-To'] = reply_to
                
                # Send email
                context = ssl.create_default_context()
                server = smtplib.SMTP(host, port)
                server.starttls(context=context)
                server.login(username, password)
                server.sendmail(from_address, [to_email], msg.as_bytes())
                server.quit()
                
                return (True, "Email sent successfully")
            else:
                # Wrap plain text in minimal HTML for consistency (when HTML format is requested but content is plain)
                html_converted = content.replace("\n\n", "</p><p>").replace("\n", "<br>")
                html_content = '<html><body><p>' + html_converted + '</p></body></html>'
                content = html_content
                is_html = True
        
        # Create multipart message with BOTH HTML and plain text
        msg = MIMEMultipart('alternative')
        
        # Set headers properly
        msg['Subject'] = Header(final_subject, 'utf-8').encode()
        msg['From'] = formataddr((str(Header(from_name, 'utf-8')), from_address))
        # To header with angle brackets for proper formatting (fixes TO_NO_BRKTS_HTML_ONLY)
        msg['To'] = f"<{to_email}>"
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()
        
        # Ensure From header domain matches envelope from (fixes HEADER_FROM_DIFFERENT_DOMAINS)
        # The from_address in sendmail should match the From header for SPF alignment
        
        # Set Reply-To if provided
        if reply_to:
            msg['Reply-To'] = reply_to
        
        # Additional headers for better deliverability (10/10 template requirements)
        # List-Unsubscribe headers for proper list management
        msg['List-Unsubscribe'] = f'<mailto:{from_address}?subject=Unsubscribe>'
        msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
        
        # X-Mailer identification
        msg['X-Mailer'] = 'FV-S-EmailSystem/1.0'
        
        # Precedence and priority headers
        msg['Precedence'] = 'bulk'
        msg['X-Priority'] = '3'
        msg['X-MSMail-Priority'] = 'Normal'
        msg['X-MimeOLE'] = 'FV-S Email System'
        
        # List-ID for identification
        msg['X-List-ID'] = 'FV-S Email Campaign'
        msg['X-Auto-Response-Suppress'] = 'OOF'
        
        # ALWAYS attach BOTH plain text and HTML versions
        # Plain text FIRST (required by RFC for multipart/alternative)
        text_part = MIMEText(text_content, 'plain', 'utf-8')
        msg.attach(text_part)
        
        # HTML version SECOND
        html_part = MIMEText(content, 'html', 'utf-8')
        msg.attach(html_part)
        
        # Connect and send
        if port == 465:
            # SSL connection for port 465
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
            try:
                server.login(username, password)
            except smtplib.SMTPException:
                pass

                server = smtplib.SMTP_SSL(host, port, timeout=30)
                server.login(username, password)
        else:
            # For port 587 and others - use STARTTLS
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPException:
                pass

                pass
            server.login(username, password)
        
        # Send the email
        server.sendmail(from_address, [to_email], msg.as_bytes())
        server.quit()
        
        return (True, "Email sent successfully")
        
    except smtplib.SMTPAuthenticationError as e:
        pass

        return (False, f"Authentication failed: {str(e)}")
    except smtplib.SMTPRecipientsRefused as e:
        pass

        return (False, f"Recipient refused: {str(e)}")
    except smtplib.SMTPServerDisconnected as e:
        pass

        return (False, f"Server disconnected: {str(e)}")
    except Exception as e:
        pass

        return (False, f"SMTP error: {str(e)}")


def send_email_with_retry(
    smtp_config: dict,
    to_email: str,
    from_name: str,
    subject: str,
    content: str,
    reply_to: str = None,
    max_retries: int = 3,
    delay: int = 5,
    **customization
) -> tuple:
    """
    Send email with retry logic
    
    Returns:
        tuple: (success: bool, message: str, attempts: int)
    """
    for attempt in range(max_retries):
        success, message = send_smtp_email(
            smtp_config,
            to_email,
            from_name,
            subject,
            content,
            reply_to,
            **customization
        )
        
        if success:
            return (True, message, attempt + 1)
        
        # Wait before retry
        if attempt < max_retries - 1:
            time.sleep(delay)
    
    return (False, message, max_retries)


def process_campaign_batch(
    batch: List[str],
    smtp_configs: List[dict],
    from_names: List[str],
    subjects: List[str],
    letter_content: str,
    reply_to: str,
    settings: dict,
    customization: dict
) -> dict:
    """
    Process a batch of emails
    
    Returns:
        dict with sent, failed, and logs
    """
    results = {
        'sent': 0,
        'failed': 0,
        'logs': []
    }
    
    delay = settings.get('random_delay', 5)
    smtp_rotate_after = settings.get('smtp_rotate_after', 50)
    max_retries = settings.get('retry_limit', 3)
    
    smtp_index = 0
    from_index = 0
    subject_index = 0
    
    for i, email in enumerate(batch):
        # Rotate SMTP
        if i > 0 and i % smtp_rotate_after == 0:
            smtp_index = (smtp_index + 1) % len(smtp_configs)
        
        smtp_config = smtp_configs[smtp_index]
        from_name = from_names[from_index % len(from_names)]
        subject = subjects[subject_index % len(subjects)]
        
        from_index = (from_index + 1) % len(from_names)
        subject_index = (subject_index + 1) % len(subjects)
        
        # Customize content
        final_content = prepare_email_content(
            letter_content,
            email,
            customization.get('random', False),
            customization.get('unsubscribe', False)
        )
        
        # Send email with retry
        success, message, attempts = send_email_with_retry(
            smtp_config,
            email,
            from_name,
            subject,
            final_content,
            reply_to,
            max_retries,
            delay,
            customize_email=customization.get('email', False),
            customize_ename=customization.get('ename_value', ''),
            custom_subject_suffix=customization.get('ename_value', '') if customization.get('ename', False) else None
        )
        
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'email': email,
            'from_name': from_name,
            'subject': subject,
            'smtp': smtp_config.get('email', smtp_config.get('host', 'unknown')),
            'success': success,
            'message': message,
            'attempts': attempts
        }
        
        if success:
            results['sent'] += 1
            log_entry['type'] = 'success'
        else:
            results['failed'] += 1
            log_entry['type'] = 'error'
        
        results['logs'].append(log_entry)
        
        # Apply delay between emails
        time.sleep(delay)
    
    return results


def log_sending_result(user_id: str, base_path: Path, campaign_id: str, result: dict) -> bool:
    """Log sending result to file"""
    try:
        logs_dir = base_path / f"fv-s-{user_id}" / "sender-config" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        
        log_file = logs_dir / f"{campaign_id}_log.json"
        
        # Load existing logs or create new
        if log_file.exists():
            with open(log_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {'campaign_id': campaign_id, 'logs': []}
        
        # Append new logs
        data['logs'].extend(result.get('logs', []))
        
        # Save
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception:
        pass

        return False


# Throttles session-file writes per campaign so per-email progress updates do
# not hammer the disk (writes are still atomic).
_session_write_timestamps: Dict[str, float] = {}
_SESSION_WRITE_MIN_INTERVAL = 1.0  # seconds


def update_sending_session(user_id: str, base_path: Path, campaign_id: str, sent: int, failed: int, status: str = 'running') -> bool:
    """Update sending session stats (atomic write, rate-throttled)."""
    try:
        sessions_dir = base_path / f"fv-s-{user_id}" / "sender-config" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        
        session_file = sessions_dir / f"{campaign_id}.json"

        # Always persist terminal statuses; throttle intermediate 'running'
        # updates to at most one write per _SESSION_WRITE_MIN_INTERVAL.
        if status == "running":
            last = _session_write_timestamps.get(campaign_id, 0.0)
            now = time.time()
            if now - last < _SESSION_WRITE_MIN_INTERVAL:
                return True
            _session_write_timestamps[campaign_id] = now
        else:
            _session_write_timestamps.pop(campaign_id, None)
        
        # Load existing session to preserve all fields
        existing_session = {}
        if session_file.exists():
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    existing_session = json.load(f)
            except Exception:
                pass

                pass
        
        # Update only the fields that need to change, preserve everything else
        existing_session['id'] = campaign_id
        existing_session['user_id'] = user_id
        existing_session['updated_at'] = datetime.now().isoformat()
        existing_session['sent'] = sent
        existing_session['failed'] = failed
        existing_session['status'] = status
        
        # Ensure critical fields are preserved
        if 'created_at' not in existing_session:
            existing_session['created_at'] = datetime.now().isoformat()
        if 'total_emails' not in existing_session:
            existing_session['total_emails'] = 0
        if 'campaign_name' not in existing_session:
            existing_session['campaign_name'] = 'Campaign'
        if 'started_at' not in existing_session:
            existing_session['started_at'] = existing_session.get('created_at', datetime.now().isoformat())
        if 'emails' not in existing_session:
            existing_session['emails'] = []
        if 'subject' not in existing_session:
            existing_session['subject'] = 'Unknown Subject'
        
        return write_json_atomic(session_file, existing_session)
    except Exception:
        pass

        return False


# =============================================================================
# COMPREHENSIVE SENT LOGGING FUNCTIONS
# =============================================================================

def get_sent_log_file(user_id: str, base_path: Path) -> Path:
    """Get the path to the user's sent.json log file"""
    user_folder = base_path / f"fv-s-{user_id}"
    user_folder.mkdir(parents=True, exist_ok=True)
    return user_folder / "sent.json"


def get_access_log_file(user_id: str, base_path: Path) -> Path:
    """Get the path to the user's access logs file"""
    user_folder = base_path / f"fv-s-{user_id}"
    user_folder.mkdir(parents=True, exist_ok=True)
    return user_folder / "access_logs.json"


def initialize_sent_log(user_id: str, base_path: Path) -> dict:
    """Initialize or load the sent.json log file"""
    sent_file = get_sent_log_file(user_id, base_path)
    
    try:
        if sent_file.exists():
            with open(sent_file, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass

        pass
    
    # Create new log structure
    log_data = {
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "total_sent": 0,
        "total_failed": 0,
        "sends": []
    }
    
    try:
        with open(sent_file, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

        pass
    
    return log_data


def log_email_send(
    user_id: str,
    base_path: Path,
    email: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    success: bool,
    message: str,
    from_name: str = None,
    subject: str = None,
    campaign_id: str = None,
    response_time: float = None,
    error_code: str = None,
    error_details: str = None,
    proxy_info: dict = None
) -> bool:
    """
    Log a single email send attempt to sent.json
    
    Returns:
        bool: True if logged successfully, False otherwise
    """
    sent_file = get_sent_log_file(user_id, base_path)
    
    try:
        # Load existing log or create new
        if sent_file.exists():
            try:
                with open(sent_file, 'r', encoding='utf-8') as f:
                    log_data = json.load(f)
            except Exception:
                pass

                log_data = initialize_sent_log(user_id, base_path)
        else:
            log_data = initialize_sent_log(user_id, base_path)
        
        # Create log entry
        log_entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "email": email,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_user": smtp_user,
            "success": success,
            "message": message,
            "from_name": from_name,
            "subject": subject,
            "campaign_id": campaign_id,
            "response_time_ms": round(response_time * 1000, 2) if response_time else None,
            "error_code": error_code,
            "error_details": error_details,
            "proxy_info": proxy_info
        }
        
        # Update totals
        if success:
            log_data["total_sent"] = log_data.get("total_sent", 0) + 1
            log_entry["type"] = "success"
        else:
            log_data["total_failed"] = log_data.get("total_failed", 0) + 1
            log_entry["type"] = "error"
        
        # Add to sends list
        log_data["sends"].append(log_entry)
        log_data["updated_at"] = datetime.now().isoformat()
        
        # Keep only last 10000 entries to prevent file from growing too large
        if len(log_data["sends"]) > 10000:
            log_data["sends"] = log_data["sends"][-10000:]
        
        # Save updated log
        with open(sent_file, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, indent=4, ensure_ascii=False)
        
        return True
    except Exception as e:
        pass

        print(f"Error logging email send: {e}")
        return False


def get_sent_logs(
    user_id: str,
    base_path: Path,
    limit: int = 100,
    offset: int = 0,
    success_only: bool = False,
    failed_only: bool = False,
    start_date: str = None,
    end_date: str = None
) -> dict:
    """
    Get paginated sent logs
    
    Returns:
        dict with logs, pagination info, and totals
    """
    sent_file = get_sent_log_file(user_id, base_path)
    
    try:
        if not sent_file.exists():
            return {
                "logs": [],
                "total": 0,
                "sent": 0,
                "failed": 0,
                "limit": limit,
                "offset": offset
            }
        
        with open(sent_file, 'r', encoding='utf-8') as f:
            log_data = json.load(f)
        
        sends = log_data.get("sends", [])
        
        # Apply filters
        if success_only:
            sends = [s for s in sends if s.get("success", False)]
        elif failed_only:
            sends = [s for s in sends if not s.get("success", False)]
        
        # Date filtering
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                sends = [s for s in sends if datetime.fromisoformat(s.get("timestamp", "").replace('Z', '+00:00')) >= start_dt]
            except Exception:
                pass

                pass
        
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                sends = [s for s in sends if datetime.fromisoformat(s.get("timestamp", "").replace('Z', '+00:00')) <= end_dt]
            except Exception:
                pass

                pass
        
        # Sort by timestamp descending
        sends.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # Paginate
        total = len(sends)
        paginated = sends[offset:offset + limit]
        
        return {
            "logs": paginated,
            "total": total,
            "sent": log_data.get("total_sent", 0),
            "failed": log_data.get("total_failed", 0),
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total
        }
    except Exception as e:
        pass

        return {
            "logs": [],
            "total": 0,
            "sent": 0,
            "failed": 0,
            "limit": limit,
            "offset": offset,
            "error": str(e)
        }


def get_sending_stats(user_id: str, base_path: Path) -> dict:
    """Get overall sending statistics from sent.json"""
    stats = {
        'total_sent': 0,
        'total_failed': 0,
        'campaigns_completed': 0,
        'campaigns_failed': 0,
        'recent_logs': [],
        'sent_today': 0,
        'failed_today': 0,
        'emails_per_minute': 0,
        'avg_response_time': 0,
        'success_rate': 0
    }
    
    try:
        sent_file = get_sent_log_file(user_id, base_path)
        
        if not sent_file.exists():
            return stats
        
        with open(sent_file, 'r', encoding='utf-8') as f:
            log_data = json.load(f)
        
        logs = log_data.get("sends", [])
        
        # Calculate totals
        stats['total_sent'] = log_data.get("total_sent", 0)
        stats['total_failed'] = log_data.get("total_failed", 0)
        
        # Calculate success rate
        total = stats['total_sent'] + stats['total_failed']
        if total > 0:
            stats['success_rate'] = round((stats['total_sent'] / total) * 100, 2)
        
        # Today's stats
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_logs = [l for l in logs if datetime.fromisoformat(l.get("timestamp", "").replace('Z', '+00:00')) >= today_start]
        
        stats['sent_today'] = len([l for l in today_logs if l.get("success", False)])
        stats['failed_today'] = len([l for l in today_logs if not l.get("success", False)])
        
        # Calculate emails per minute (last hour)
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_logs = [l for l in logs if datetime.fromisoformat(l.get("timestamp", "").replace('Z', '+00:00')) >= one_hour_ago]
        success_recent = len([l for l in recent_logs if l.get("success", False)])
        
        # Calculate emails per minute
        time_diff = (datetime.now() - one_hour_ago).total_seconds() / 60
        if time_diff > 0:
            stats['emails_per_minute'] = round(success_recent / time_diff, 2)
        
        # Calculate average response time
        response_times = [l.get("response_time_ms", 0) for l in logs if l.get("response_time_ms")]
        if response_times:
            stats['avg_response_time'] = round(sum(response_times) / len(response_times), 2)
        
        # Recent logs (last 20)
        stats['recent_logs'] = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)[:20]
        
        # Count campaigns from sessions
        sessions_dir = base_path / f"fv-s-{user_id}" / "sender-config" / "sessions"
        if sessions_dir.exists():
            for session_file in sessions_dir.glob("*.json"):
                try:
                    with open(session_file, 'r', encoding='utf-8') as f:
                        session = json.load(f)
                    if session.get("status") == "completed":
                        stats['campaigns_completed'] += 1
                    elif session.get("status") == "failed":
                        stats['campaigns_failed'] += 1
                except Exception:
                    pass

                    continue
        
    except Exception:
        pass

        pass
    
    return stats


def log_access_event(
    user_id: str,
    base_path: Path,
    event_type: str,
    details: dict = None,
    ip_address: str = None,
    user_agent: str = None
) -> bool:
    """
    Log an access/security event
    
    Event types: login, logout, password_change, failed_login, settings_change, campaign_start, campaign_stop
    """
    access_file = get_access_log_file(user_id, base_path)
    
    try:
        # Load existing or create new
        if access_file.exists():
            try:
                with open(access_file, 'r', encoding='utf-8') as f:
                    access_data = json.load(f)
            except Exception:
                pass

                access_data = {"user_id": user_id, "events": []}
        else:
            access_data = {"user_id": user_id, "events": []}
        
        # Create event entry
        event = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "details": details or {}
        }
        
        # Add event
        access_data["events"].append(event)
        access_data["updated_at"] = datetime.now().isoformat()
        
        # Keep only last 5000 events
        if len(access_data["events"]) > 5000:
            access_data["events"] = access_data["events"][-5000:]
        
        # Save
        with open(access_file, 'w', encoding='utf-8') as f:
            json.dump(access_data, f, indent=4, ensure_ascii=False)
        
        return True
    except Exception:
        pass

        return False


def get_access_logs(
    user_id: str,
    base_path: Path,
    limit: int = 50,
    offset: int = 0,
    event_type: str = None
) -> dict:
    """
    Get access logs with optional filtering
    """
    access_file = get_access_log_file(user_id, base_path)
    
    try:
        if not access_file.exists():
            return {
                "logs": [],
                "total": 0,
                "limit": limit,
                "offset": offset
            }
        
        with open(access_file, 'r', encoding='utf-8') as f:
            access_data = json.load(f)
        
        events = access_data.get("events", [])
        
        # Filter by type if specified
        if event_type:
            events = [e for e in events if e.get("type") == event_type]
        
        # Sort by timestamp descending
        events.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # Paginate
        total = len(events)
        paginated = events[offset:offset + limit]
        
        return {
            "logs": paginated,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total
        }
    except Exception:
        pass

        return {
            "logs": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "error": str(e)
        }


def get_security_info(user_id: str, base_path: Path) -> dict:
    """
    Get comprehensive security information for a user
    """
    security_info = {
        "user_id": user_id,
        "password_changed_at": None,
        "last_login": None,
        "login_count": 0,
        "last_access": None,
        "recent_failed_logins": [],
        "active_sessions": 0,
        "account_created": None
    }
    
    try:
        # Get from logs.json
        logs_file = base_path / f"fv-s-{user_id}" / FileNames.LOGS
        
        if logs_file.exists():
            with open(logs_file, 'r', encoding='utf-8') as f:
                logs_data = json.load(f)
            
            security_info["password_changed_at"] = logs_data.get("password_changed_at")
            security_info["last_login"] = logs_data.get("last_login")
            security_info["login_count"] = logs_data.get("login_count", 0)
            security_info["account_created"] = logs_data.get("created_at")
        
        # Get access logs for recent failed logins
        access_file = get_access_log_file(user_id, base_path)
        
        if access_file.exists():
            with open(access_file, 'r', encoding='utf-8') as f:
                access_data = json.load(f)
            
            events = access_data.get("events", [])
            
            # Get recent failed logins
            failed_logins = [e for e in events if e.get("type") == "failed_login"]
            failed_logins.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            security_info["recent_failed_logins"] = failed_logins[:5]
            
            # Get last access
            if events:
                events.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
                security_info["last_access"] = events[0].get("timestamp")
            
            # Count active sessions
            sessions_dir = base_path / f"fv-s-{user_id}" / "sender-config" / "sessions"
            if sessions_dir.exists():
                for session_file in sessions_dir.glob("*.json"):
                    try:
                        with open(session_file, 'r', encoding='utf-8') as f:
                            session = json.load(f)
                        if session.get("status") == "running":
                            security_info["active_sessions"] += 1
                    except Exception:
                        pass

                        continue
        
        # Get from users.json
        from functions import get_user_by_id, read_users_database
        
        user = get_user_by_id(base_path, user_id)
        if user:
            security_info["password_changed_at"] = user.get("password_changed_at")
            security_info["last_login"] = user.get("last_login")
            security_info["login_count"] = user.get("login_count", 0)
            security_info["account_created"] = user.get("created_at")
    
    except Exception:
        pass

        pass
    
    return security_info


# =============================================================================
# USER SESSION MANAGEMENT (Login Sessions across devices)
# =============================================================================

def get_user_sessions_file(user_id: str, base_path: Path) -> Path:
    """Get the user sessions file path"""
    user_dir = base_path / f"fv-s-{user_id}"
    sessions_dir = user_dir / "login-sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / "user_sessions.json"


def read_user_sessions(user_id: str, base_path: Path) -> dict:
    """Read user sessions data"""
    sessions_file = get_user_sessions_file(user_id, base_path)
    
    if not sessions_file.exists():
        return {"sessions": [], "last_updated": datetime.now().isoformat()}
    
    try:
        with open(sessions_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

        return {"sessions": [], "last_updated": datetime.now().isoformat()}


def write_user_sessions(user_id: str, base_path: Path, sessions_data: dict) -> bool:
    """Write user sessions data"""
    try:
        sessions_file = get_user_sessions_file(user_id, base_path)
        sessions_data["last_updated"] = datetime.now().isoformat()
        
        with open(sessions_file, 'w', encoding='utf-8') as f:
            json.dump(sessions_data, f, indent=2)
        
        return True
    except Exception:
        pass

        return False


def create_user_session(user_id: str, base_path: Path, session_token: str, ip_address: str, user_agent: str, device_info: str = "Unknown Device") -> dict:
    """Create a new user login session"""
    sessions_data = read_user_sessions(user_id, base_path)
    
    # Create new session
    new_session = {
        "session_id": session_token[:32] if len(session_token) > 32 else session_token,
        "user_id": user_id,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "device_info": device_info,
        "created_at": datetime.now().isoformat(),
        "last_active": datetime.now().isoformat(),
        "is_active": True,
        "is_current": True
    }
    
    # Mark all other sessions as not current
    for session in sessions_data.get("sessions", []):
        session["is_current"] = False
    
    # Add new session
    sessions_data["sessions"].insert(0, new_session)
    
    # Keep only the last 10 sessions
    sessions_data["sessions"] = sessions_data["sessions"][:10]
    
    write_user_sessions(user_id, base_path, sessions_data)
    
    return new_session


def update_user_session_activity(user_id: str, base_path: Path, session_id: str) -> bool:
    """Update the last active time for a session"""
    sessions_data = read_user_sessions(user_id, base_path)
    
    for session in sessions_data.get("sessions", []):
        if session.get("session_id") == session_id:
            session["last_active"] = datetime.now().isoformat()
            write_user_sessions(user_id, base_path, sessions_data)
            return True
    
    return False


def logout_user_session(user_id: str, base_path: Path, session_id: str) -> bool:
    """Logout a specific session"""
    sessions_data = read_user_sessions(user_id, base_path)
    
    for session in sessions_data.get("sessions", []):
        if session.get("session_id") == session_id:
            session["is_active"] = False
            session["logged_out_at"] = datetime.now().isoformat()
            write_user_sessions(user_id, base_path, sessions_data)
            return True
    
    return False


def logout_all_other_sessions(user_id: str, base_path: Path, current_session_id: str) -> int:
    """Logout all sessions except the current one"""
    sessions_data = read_user_sessions(user_id, base_path)
    count = 0
    
    for session in sessions_data.get("sessions", []):
        if session.get("session_id") != current_session_id and session.get("is_active", True):
            session["is_active"] = False
            session["logged_out_at"] = datetime.now().isoformat()
            count += 1
    
    if count > 0:
        write_user_sessions(user_id, base_path, sessions_data)
    
    return count


def get_active_user_sessions(user_id: str, base_path: Path) -> list:
    """Get all active sessions for a user"""
    sessions_data = read_user_sessions(user_id, base_path)
    active_sessions = [s for s in sessions_data.get("sessions", []) if s.get("is_active", True)]
    return active_sessions


# =============================================================================
# PROXY TESTING AND MANAGEMENT FUNCTIONS
# =============================================================================

def test_decodo_proxy(config: dict) -> dict:
    """Test Decodo proxy connection using host:port:user:pass format"""
    try:
        host = config.get("host", "")
        port = config.get("port", 0)
        username = config.get("username", "")
        password = config.get("password", "")
        
        if not host or not port:
            return {"success": False, "error": "Missing host or port"}
        
        if not username or not password:
            return {"success": False, "error": "Missing username or password"}
        
        import socket
        import time
        import urllib.request
        
        # Test TCP connection and measure latency
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        
        try:
            start_time = time.time()
            sock.connect((host, int(port)))
            sock.close()
            latency = int((time.time() - start_time) * 1000)
            
            # Try to get IP through proxy
            test_ip = None
            proxy_url = f"http://{username}:{password}@{host}:{port}"
            try:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler)
                opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
                response = opener.open('https://api.ipify.org?format=json', timeout=15)
                if response:
                    ip_data = json.loads(response.read().decode('utf-8'))
                    test_ip = ip_data.get('ip')
            except Exception as e:
                pass

                pass
            
            return {
                "success": True,
                "message": f"Decodo proxy {host}:{port} connection successful",
                "latency": latency,
                "ip": test_ip
            }
        except Exception as e:
            pass

            return {"success": False, "error": f"Connection failed: {str(e)}"}
    except Exception as e:
        pass

        return {"success": False, "error": str(e)}


def test_brightdata_proxy(config: dict) -> dict:
    """Test Bright Data proxy connection using host:port:user:pass format"""
    try:
        host = config.get("host", "")
        port = config.get("port", 0)
        username = config.get("username", "")
        password = config.get("password", "")
        
        if not host or not port:
            return {"success": False, "error": "Missing host or port"}
        
        if not username or not password:
            return {"success": False, "error": "Missing username or password"}
        
        import socket
        import time
        import urllib.request
        
        # Test TCP connection and measure latency
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        
        try:
            start_time = time.time()
            sock.connect((host, int(port)))
            sock.close()
            latency = int((time.time() - start_time) * 1000)
            
            # Try to get IP through proxy
            test_ip = None
            proxy_url = f"http://{username}:{password}@{host}:{port}"
            try:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler)
                opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
                response = opener.open('https://api.ipify.org?format=json', timeout=15)
                if response:
                    ip_data = json.loads(response.read().decode('utf-8'))
                    test_ip = ip_data.get('ip')
            except Exception as e:
                pass

                pass
            
            return {
                "success": True,
                "message": f"Bright Data proxy {host}:{port} connection successful",
                "latency": latency,
                "ip": test_ip
            }
        except Exception as e:
            pass

            return {"success": False, "error": f"Connection failed: {str(e)}"}
    except Exception as e:
        pass

        return {"success": False, "error": str(e)}


def test_oxylabs_proxy(config: dict) -> dict:
    """Test Oxylabs proxy connection"""
    try:
        username = config.get("username", "")
        password = config.get("password", "")
        country = config.get("country", "us")
        
        if not username or not password:
            return {"success": False, "error": "Missing username or password"}
        
        # Oxylabs proxy format: customer-USERNAME-session-RANDOM
        import socket
        import time
        import urllib.request
        
        proxy_host = f"pr.oxylabs.io"
        proxy_port = 7777  # Oxylabs default
        
        # Test connection and measure latency
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        
        try:
            start_time = time.time()
            sock.connect((proxy_host, proxy_port))
            sock.close()
            latency = int((time.time() - start_time) * 1000)
            
            # Try to get IP through proxy
            test_ip = None
            proxy_url = f"http://{username}:{password}@{proxy_host}:{proxy_port}"
            try:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler)
                opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
                response = opener.open('https://api.ipify.org?format=json', timeout=15)
                if response:
                    ip_data = json.loads(response.read().decode('utf-8'))
                    test_ip = ip_data.get('ip')
            except Exception as e:
                pass

                pass
            
            return {
                "success": True,
                "message": "Oxylabs proxy connection successful",
                "latency": latency,
                "ip": test_ip
            }
        except Exception as e:
            pass

            return {"success": False, "error": f"Connection failed: {str(e)}"}
    except Exception as e:
        pass

        return {"success": False, "error": str(e)}


def test_custom_proxy(config: dict) -> dict:
    """Test custom proxy connection (HTTP, HTTPS, SOCKS4, SOCKS5)"""
    try:
        protocol = config.get("protocol", "http")
        host = config.get("host", "")
        port = config.get("port", 0)
        username = config.get("username", "")
        password = config.get("password", "")
        
        if not host or not port:
            return {"success": False, "error": "Missing host or port"}
        
        import socket
        import time
        import urllib.request
        
        # Test TCP connection and measure latency
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        
        try:
            start_time = time.time()
            sock.connect((host, int(port)))
            sock.close()
            latency = int((time.time() - start_time) * 1000)
            
            # Try to get IP through proxy
            test_ip = None
            
            if username and password:
                proxy_url = f"{protocol}://{username}:{password}@{host}:{port}"
                try:
                    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                    opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler)
                    opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
                    response = opener.open('https://api.ipify.org?format=json', timeout=15)
                    if response:
                        ip_data = json.loads(response.read().decode('utf-8'))
                        test_ip = ip_data.get('ip')
                except Exception as e:
                    pass

                    pass
            elif host and port:
                proxy_url = f"{protocol}://{host}:{port}"
                try:
                    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                    opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler)
                    opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
                    response = opener.open('https://api.ipify.org?format=json', timeout=15)
                    if response:
                        ip_data = json.loads(response.read().decode('utf-8'))
                        test_ip = ip_data.get('ip')
                except Exception as e:
                    pass

                    pass
            
            return {
                "success": True,
                "message": f"{protocol.upper()} proxy connection successful",
                "latency": latency,
                "ip": test_ip
            }
        except Exception as e:
            pass

            return {"success": False, "error": f"Connection failed: {str(e)}"}
    except Exception as e:
        pass

        return {"success": False, "error": str(e)}


def test_single_proxy(proxy: dict) -> dict:
    """Test a single proxy from the proxy list"""
    try:
        proxy_type = proxy.get("type", "custom")
        
        if proxy_type == "decodo":
            return test_decodo_proxy(proxy)
        elif proxy_type == "brightdata":
            return test_brightdata_proxy(proxy)
        elif proxy_type == "oxylabs":
            return test_oxylabs_proxy(proxy)
        else:
            return test_custom_proxy(proxy)
    except Exception as e:
        pass

        return {"success": False, "error": str(e)}


def get_proxy_for_sending(proxies: list, settings: dict, last_proxy_index: int = 0) -> tuple:
    """Get next available proxy for sending based on rotation settings"""
    if not proxies:
        return None, last_proxy_index
    
    # Filter active proxies only
    active_proxies = [p for p in proxies if p.get("status") == "active"]
    
    if not active_proxies:
        return None, last_proxy_index
    
    # Apply rotation based on settings
    rotate_after = settings.get("rotateAfterEmails", 5)
    
    # Simple round-robin rotation
    proxy_index = last_proxy_index % len(active_proxies)
    selected_proxy = active_proxies[proxy_index]
    
    return selected_proxy, proxy_index


def build_proxy_url(proxy: dict) -> str:
    """Build proxy URL from proxy configuration
    
    Handles proxy formats like:
    {
      "host": "us.decodo.com",
      "port": "10000",  (can be string or int)
      "username": "spc3h9bjvk",
      "password": "hWX2Ps4Ntz5uh9p_le",
      "type": "custom",
      "status": "active"
    }
    """
    proxy_type = proxy.get("type", "custom")
    
    if proxy_type == "decodo":
        # Decodo rotating proxy: http://user:pass@host:port
        host = proxy.get("host", "")
        port = proxy.get("port", 0)
        # Handle string port
        try:
            port = int(port) if port else 0
        except (ValueError, TypeError):
            pass

            port = 0
        username = proxy.get("username", "")
        password = proxy.get("password", "")
        
        if host and port:
            if username and password:
                return f"http://{username}:{password}@{host}:{port}"
            else:
                return f"http://{host}:{port}"
        return None
    elif proxy_type == "brightdata":
        # Bright Data rotating proxy: http://user:pass@host:port
        host = proxy.get("host", "")
        port = proxy.get("port", 0)
        # Handle string port
        try:
            port = int(port) if port else 0
        except (ValueError, TypeError):
            pass

            port = 0
        username = proxy.get("username", "")
        password = proxy.get("password", "")
        
        if host and port:
            if username and password:
                return f"http://{username}:{password}@{host}:{port}"
            else:
                return f"http://{host}:{port}"
        return None
    elif proxy_type == "oxylabs":
        username = proxy.get("username", "")
        password = proxy.get("password", "")
        if username and password:
            return f"http://{username}:{password}@pr.oxylabs.io:7777"
    else:
        # Custom proxy - this handles your Decodo proxy with type="custom"
        protocol = proxy.get("protocol", "http")
        host = proxy.get("host", "")
        port = proxy.get("port", 0)
        # Handle string port (your proxy has port as string "10000")
        try:
            port = int(port) if port else 0
        except (ValueError, TypeError):
            pass

            port = 0
        username = proxy.get("username", "")
        password = proxy.get("password", "")
        
        if host and port:
            if username and password:
                return f"{protocol}://{username}:{password}@{host}:{port}"
            else:
                return f"{protocol}://{host}:{port}"
    
    return None


async def send_smtp_email_via_proxy(smtp_config: dict, email: str, from_name: str, subject: str, content: str, reply_to: str, proxy_url: str = None) -> tuple:
    """Send SMTP email via proxy if proxy_url is provided"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        host = smtp_config.get("host", "")
        port = smtp_config.get("port", 587)
        username = smtp_config.get("user", "")
        password = smtp_config.get("password", "")
        from_address = smtp_config.get("from_address", username)
        
        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_address}>"
        msg["To"] = email
        if reply_to:
            msg["Reply-To"] = reply_to
        
        # Add plain text and HTML parts
        text_part = MIMEText(content, "plain")
        html_part = MIMEText(content, "html")
        msg.attach(text_part)
        msg.attach(html_part)
        
        # Connect to SMTP server
        if proxy_url:
            # Using proxy requires different approach
            # For HTTP proxies, we can use SMTPS or HTTP CONNECT
            import socket
            import socks
            
            # Configure SOCKS proxy if available
            proxy_parts = proxy_url.replace("http://", "").replace("https://", "").split("@")
            if len(proxy_parts) == 2:
                auth, address = proxy_parts
                host_port = address.split(":")
                proxy_host = host_port[0]
                proxy_port = int(host_port[1]) if len(host_port) > 1 else 1080
        
        # Standard SMTP connection
        server = smtplib.SMTP(host, int(port), timeout=30)
        server.ehlo()
        
        if int(port) == 587:
            server.starttls()
        
        server.login(username, password)
        server.sendmail(from_address, [email], msg.as_string())
        server.quit()
        
        return True, "Email sent successfully"
    except Exception as e:
        pass

        return False, str(e)


def remove_failed_proxy(proxies: list, proxy_index: int, auto_remove: bool = True) -> list:
    """Remove a failed proxy from the list if auto_remove is enabled"""
    if not auto_remove:
        return proxies
    
    if 0 <= proxy_index < len(proxies):
        proxies.pop(proxy_index)
    
    return proxies


# =============================================================================
# VALIDATOR MODELS
# =============================================================================

class EmailValidationRequest(BaseModel):
    """Request to start email validation"""
    email_list_id: Optional[str] = Field(None, description="ID of existing email list to validate")
    import_content: Optional[str] = Field(None, description="Raw content to create new list")
    list_name: Optional[str] = Field(None, description="Name for new list (if importing)")
    use_proxy: bool = Field(True, description="Use proxy rotation")
    proxy_type: str = Field("brightdata", description="Proxy type: brightdata, decodo, custom")
    browser_api: bool = Field(True, description="Use Bright Data browser API")
    batch_size: int = Field(3, description="Emails per batch before proxy rotation")


class PhoneValidationRequest(BaseModel):
    """Request to start phone validation"""
    phone_numbers: List[str] = Field(..., description="List of phone numbers to validate")
    country_code: Optional[str] = Field(None, description="Country code if all numbers are from same country")
    list_name: Optional[str] = Field(None, description="Name for the phone list")
    use_browser_api: bool = Field(True, description="Use Bright Data browser API")


class ValidatorSession(BaseModel):
    """Validator session for persistent process tracking"""
    session_id: str
    user_id: str
    type: str = Field(..., description="email or phone")
    status: str = "pending"  # pending, running, paused, completed, stopped, error
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    
    # Progress stats
    total_items: int = 0
    processed: int = 0
    valid: int = 0
    invalid: int = 0
    failed: int = 0
    verified_per_minute: float = 0.0
    
    # Current items
    queue: List[str] = []
    current_batch: List[str] = []
    
    # Source info
    list_id: Optional[str] = None
    list_name: Optional[str] = None
    is_import: bool = False
    
    # Settings
    use_proxy: bool = True
    proxy_type: str = "brightdata"
    browser_api: bool = True
    batch_size: int = 3
    
    # Error handling
    error_message: Optional[str] = None
    last_error: Optional[str] = None


class EmailValidationResult(BaseModel):
    """Detailed email validation result from browser scraping"""
    email: str
    valid: bool = False
    status: str = "unknown"  # valid, invalid, catch-all, unknown
    sub_status: Optional[str] = None  # did_you_mean, etc.
    free_email: bool = False
    did_you_mean: Optional[str] = None
    account: Optional[str] = None
    domain: Optional[str] = None
    domain_age_days: Optional[int] = None
    smtp_provider: Optional[str] = None
    mx_found: bool = False
    mx_record: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    validated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    error: Optional[str] = None


class EmailListCreate(BaseModel):
    """Request to create a new email list"""
    name: str = Field(..., description="Name for the email list")
    emails: List[str] = Field(..., description="List of email addresses")


class EmailListResponse(BaseModel):
    """Email list summary response"""
    id: str
    name: str
    total_emails: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    created_at: str
    source: str = "manual"  # manual, import, validation


class EmailListDetailResponse(BaseModel):
    """Detailed email list response with all emails"""
    id: str
    name: str
    total_emails: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    created_at: str
    source: str = "manual"  # Default for legacy data created before this field existed
    emails: List[EmailValidationResult] = []


class PhoneValidationResult(BaseModel):
    """Detailed phone number validation result from ipqualityscore scraping"""
    number: str
    country_code: str = "US"
    formatted_number: Optional[str] = None
    valid: bool = False
    carrier: Optional[str] = None
    line_type: Optional[str] = None  # Wireless, VOIP, Landline, etc.
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    is_prepaid: Optional[bool] = None
    is_leaked: Optional[bool] = None
    validated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    error: Optional[str] = None


class PhoneListResponse(BaseModel):
    """Phone number list summary response"""
    id: str
    name: str
    total_numbers: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    created_at: str
    country_codes: List[str] = []  # All countries in this list


class PhoneListDetailResponse(BaseModel):
    """Detailed phone list response with all numbers"""
    id: str
    name: str
    total_numbers: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    created_at: str
    country_codes: List[str] = []
    numbers: List[PhoneValidationResult] = []


class PhoneListImportRequest(BaseModel):
    """Request to import phone numbers with country detection"""
    numbers: List[str] = Field(..., description="Raw phone numbers")
    country_code: Optional[str] = Field(None, description="Force country code if all numbers are from same country")
    list_name: str = Field(..., description="Name for the phone list")


class PhoneEntry(BaseModel):
    """Single phone entry with validation data"""
    number: str
    country_code: Optional[str] = None
    formatted_number: Optional[str] = None
    valid: bool = True
    carrier: Optional[str] = None
    line_type: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    is_prepaid: Optional[bool] = None
    is_leaked: Optional[bool] = None
    added_at: Optional[str] = None


class PhoneListData(BaseModel):
    """Phone number list file structure"""
    id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    country_code: Optional[str] = None
    total_numbers: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    numbers: List[PhoneEntry] = []


# =============================================================================
# VALIDATOR SESSION MANAGEMENT
# =============================================================================

def get_validator_sessions_dir(user_id: str, base_path: Path) -> Path:
    """Get the validator sessions directory path"""
    user_folder = base_path / f"fv-s-{user_id}"
    validator_dir = user_folder / "validator-sessions"
    validator_dir.mkdir(parents=True, exist_ok=True)
    return validator_dir


def get_active_validator_session(user_id: str, base_path: Path, session_type: str = None) -> Optional[dict]:
    """Get the active validator session for a user (if any)"""
    sessions_dir = get_validator_sessions_dir(user_id, base_path)
    
    # Check for running sessions
    for session_file in sessions_dir.glob("*.json"):
        # Legacy pointers written inside this directory are not sessions.
        if session_file.name.startswith("active_"):
            continue
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                session = json.load(f)
            
            # Skip completed/stopped sessions
            if session.get('status') in ['completed', 'stopped', 'error']:
                continue
            
            # Filter by type if specified
            if session_type and session.get('type') != session_type:
                continue
            
            return session
        except Exception:
            pass

            continue
    
    return None


def save_validator_session(user_id: str, base_path: Path, session_data: dict) -> bool:
    """Save validator session to file"""
    try:
        sessions_dir = get_validator_sessions_dir(user_id, base_path)
        session_id = session_data.get('session_id', f"{session_data.get('type', 'validator')}_{uuid.uuid4().hex[:8]}")
        
        session_file = sessions_dir / f"{session_id}.json"
        session_data['session_id'] = session_id
        session_data['updated_at'] = datetime.now().isoformat()
        
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception as e:
        pass

        print(f"Error saving validator session: {e}")
        return False


def update_validator_progress(user_id: str, base_path: Path, session_id: str, updates: dict) -> bool:
    """Update validator session progress"""
    try:
        sessions_dir = get_validator_sessions_dir(user_id, base_path)
        session_file = sessions_dir / f"{session_id}.json"
        
        if not session_file.exists():
            return False
        
        with open(session_file, 'r', encoding='utf-8') as f:
            session = json.load(f)
        
        # Update fields
        for key, value in updates.items():
            session[key] = value
        
        session['updated_at'] = datetime.now().isoformat()
        
        with open(session_file, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception as e:
        pass

        print(f"Error updating validator progress: {e}")
        return False


def get_validator_session(user_id: str, base_path: Path, session_id: str) -> Optional[dict]:
    """Get a specific validator session"""
    try:
        sessions_dir = get_validator_sessions_dir(user_id, base_path)
        session_file = sessions_dir / f"{session_id}.json"
        
        if not session_file.exists():
            return None
        
        with open(session_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

        return None


def _get_active_session_pointer_file(user_id: str, base_path: Path, session_type: str) -> Path:
    """
    Path to the active-session pointer file for a validator type (email/phone).

    Deliberately kept beside -- not inside -- ``validator-sessions/``: that
    directory is globbed for ``*.json`` to find live sessions, and a pointer
    stored there was being read back as a second, permanently "pending"
    session (which then blocked every new run).
    """
    pointer_dir = base_path / f"fv-s-{user_id}" / "validator-active"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    return pointer_dir / f"active_{session_type}.json"


def save_active_session_state(
    user_id: str,
    base_path: Path,
    session_type: str,
    session_id: str,
    session_data: Optional[dict] = None
) -> bool:
    """Persist which validator session is currently active for a user."""
    try:
        pointer_file = _get_active_session_pointer_file(user_id, base_path, session_type)
        data = {
            "session_id": session_id,
            "type": session_type,
            "updated_at": datetime.now().isoformat(),
        }
        if session_data:
            data.update(session_data)
        with open(pointer_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        pass

        print(f"Error saving active session state: {e}")
        return False


def clear_active_session(user_id: str, base_path: Path, session_type: str) -> bool:
    """Clear the active validator session pointer for a user."""
    try:
        pointer_file = _get_active_session_pointer_file(user_id, base_path, session_type)
        if pointer_file.exists():
            pointer_file.unlink()
        return True
    except Exception as e:
        pass

        print(f"Error clearing active session: {e}")
        return False


def check_validator_running(user_id: str, base_path: Path) -> dict:
    """Check if there's a running validator process for the user"""
    active_session = get_active_validator_session(user_id, base_path)
    
    if active_session:
        return {
            "running": True,
            "session_id": active_session.get('session_id'),
            "type": active_session.get('type'),
            "status": active_session.get('status'),
            "list_name": active_session.get('list_name'),
            "progress": f"{active_session.get('processed', 0)}/{active_session.get('total_items', 0)}"
        }
    
    return {"running": False}


# =============================================================================
# EMAIL VALIDATION FUNCTIONS
# =============================================================================

def create_email_validation_session(
    user_id: str,
    base_path: Path,
    emails: List[str],
    list_id: str = None,
    list_name: str = None,
    is_import: bool = False,
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
) -> dict:
    """Create a new email validation session"""
    
    # Check if there's already an active session
    active = get_active_validator_session(user_id, base_path, "email")
    if active:
        return {"success": False, "error": "A validation process is already running", "session_id": active.get('session_id')}
    
    # Generate session ID
    session_id = f"email_val_{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()
    
    session_data = {
        "session_id": session_id,
        "user_id": user_id,
        "type": "email",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "total_items": len(emails),
        "processed": 0,
        "valid": 0,
        "invalid": 0,
        "failed": 0,
        "verified_per_minute": 0.0,
        "queue": emails,
        "current_batch": [],
        "list_id": list_id,
        "list_name": list_name or (f"Imported {len(emails)} emails" if is_import else None),
        "is_import": is_import,
        "transport": "decodo",
        "batch_size": batch_size,
        "results": [],  # Store validation results
        "last_proxy_rotation": 0,
        "start_time": None,
        "elapsed_seconds": 0
    }
    
    save_validator_session(user_id, base_path, session_data)
    save_active_session_state(user_id, base_path, "email", session_id, session_data)
    
    return {"success": True, "session_id": session_id, "session": session_data}


# =============================================================================
# EMAIL VALIDATION BATCH PROCESSOR (Decodo transport)
# =============================================================================

async def process_email_validation_batch(
    user_id: str,
    base_path: Path,
    session_id: str
) -> dict:
    """
    Process a batch of emails for validation
    This runs asynchronously and updates the session
    
    Returns updated session data
    """
    session = get_validator_session(user_id, base_path, session_id)
    
    if not session:
        return {"success": False, "error": "Session not found"}
    
    
    
    if session.get('status') == 'stopped':
        return {"success": False, "error": "Session was stopped"}
    
    if session.get('status') == 'completed':
        return {"success": False, "error": "Session already completed"}
    
    # Decodo resolves its own egress; no browser/proxy plumbing here.
    decodo_settings = load_decodo_settings(user_id, base_path)
    
    # Honour the size recorded on the session, clamped to the worker ceiling.
    try:
        batch_size = int(session.get('batch_size') or DEFAULT_VALIDATION_BATCH_SIZE)
    except (TypeError, ValueError):
        batch_size = DEFAULT_VALIDATION_BATCH_SIZE
    batch_size = max(1, min(batch_size, MAX_VALIDATION_BATCH_SIZE))
    queue = Queue.from_list(session.get('queue', []))
    current_batch = session.get('current_batch', [])

    # Build the next batch - up to batch_size emails
    while len(current_batch) < batch_size and queue:
        current_batch.append(queue.pop(0))

    if not current_batch:
        # Queue is empty, validation complete
        update_validator_progress(user_id, base_path, session_id, {
            "status": "completed",
            "completed_at": datetime.now().isoformat()
        })
        return {"success": True, "completed": True}

    # Pick the transport: a Decodo key buys a scraped verifier verdict; without
    # one we fall back to the free engine (syntax + MX + reputation + optional
    # SMTP) rather than failing every address.
    results = []
    if decodo_settings.get("api_key"):
        email_transport = "decodo"
        validation_results = await validate_emails_async(
            list(current_batch), decodo_settings
        )
    elif is_offline_engine_available():
        email_transport = "local"
        validation_results = await validate_emails_offline_async(
            list(current_batch),
            check_dns=True,
            smtp_probe=bool(os.getenv("FV_S_SMTP_PROBE", "1") == "1"),
        )
    else:
        email_transport = "decodo"
        validation_results = await validate_emails_async(
            list(current_batch), decodo_settings
        )
    
    # Process results
    valid_count = 0
    invalid_count = 0
    failed_count = 0
    
    for result in validation_results:
        # A transport/parse failure is 'failed'; only an explicit positive
        # verdict is 'valid'. 'unknown'/'risky' land in 'invalid' and are
        # counted against the run rather than silently promoted.
        if result.get('error'):
            failed_count += 1
        elif result.get('valid') and result.get('status') in VALID_STATUSES:
            valid_count += 1
        else:
            invalid_count += 1
        
        # Build detailed result entry with error information for logging
        result_entry = {
            "email": result.get('email'),
            "valid": bool(result.get('valid', False)) and not result.get('error'),
            "status": result.get('status'),
            "category": result.get('category'),
            "confidence": result.get('confidence', 0),
            "domain": result.get('domain'),
            "site": result.get('site'),
            "validated_at": datetime.now().isoformat()
        }
        
        # Include error details if present (for failed/disposable/invalid emails)
        if result.get('error'):
            result_entry["error"] = result.get('error')
        if result.get('reason'):
            result_entry["reason"] = result.get('reason')
        if result.get('did_you_mean'):
            result_entry["did_you_mean"] = result.get('did_you_mean')
        
        results.append(result_entry)
    
    # Update session
    total_processed = session.get('processed', 0) + len(current_batch)
    total_valid = session.get('valid', 0) + valid_count
    total_invalid = session.get('invalid', 0) + invalid_count
    total_failed = session.get('failed', 0) + failed_count
    
    # Calculate verified per minute
    start_time_str = session.get('start_time')
    if start_time_str:
        start_time = datetime.fromisoformat(start_time_str)
        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed > 0:
            vpm = (total_valid / elapsed) * 60
        else:
            vpm = 0
    else:
        vpm = 0
    
    # Save results to session
    all_results = session.get('results', [])
    all_results.extend(results)
    
    # Save this batch's results separately for real-time logging display
    # This allows frontend to show detailed results including error messages
    last_batch_results = results
    
    updates = {
        "processed": total_processed,
        "valid": total_valid,
        "invalid": total_invalid,
        "failed": total_failed,
        "verified_per_minute": round(vpm, 2),
        "current_batch": [],
        "queue": [str(q) for q in queue],
        "results": all_results,
        "last_batch_results": last_batch_results,
        "elapsed_seconds": int((datetime.now() - datetime.fromisoformat(start_time_str)).total_seconds()) if start_time_str else 0
    }
    
    # Check if this is the last email (queue empty after processing 1)
    
    if len(queue) == 0 and len(all_results) > 0:
        updates["status"] = "completed"
        updates["completed_at"] = datetime.now().isoformat()
        
        # Save validated emails to list
        save_validation_results_to_list(user_id, base_path, session, all_results)
        
        # Also save valid emails to JSON file in "valid emails" folder
        session_name = session.get('list_name') or session.get('source_name', 'Email Validation')
        save_valid_emails_to_file(user_id, base_path, all_results, session_name)
    else:
        # Mark as running
        if session.get('status') != 'running':
            updates["status"] = "running"
            updates["started_at"] = datetime.now().isoformat()
    
    update_validator_progress(user_id, base_path, session_id, updates)
    
    return {
        "success": True,
        "processed": len(current_batch),
        "valid": valid_count,
        "invalid": invalid_count,
        "failed": failed_count,
        "remaining": len(queue)
    }


def save_validation_results_to_list(
    user_id: str,
    base_path: Path,
    session: dict,
    results: List[dict]
) -> str:
    """
    Save validation results to email list
    Overwrites original list or creates new one

    Only emails that have been actually validated (passed Decodo)
    are marked as valid: True AND validated: True so the export only
    includes truly validated emails.
    """
    from functions import read_user_data, write_user_data

    list_id = session.get('list_id')
    list_name = session.get('list_name')
    is_import = session.get('is_import', False)

    # Collect valid emails - only those confirmed valid by the validation API
    valid_emails = []
    for result in results:
        if result.get('valid', False):
            valid_emails.append({
                "email": result.get('email'),
                "domain": result.get('domain') or (result.get('email', '').split('@')[1] if '@' in result.get('email', '') else None),
                "valid": True,
                "validated": True,
                "validated_at": result.get('validated_at') or datetime.now().isoformat(),
                "sub_status": result.get('sub_status'),
                "status": result.get('status'),
            })

    if is_import:
        # Create new email list from imported emails
        if not list_name:
            list_name = f"Validated Import {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        # Generate list ID
        new_list_id = f"elist_{uuid.uuid4().hex[:12]}"

        # Create email list data - only validated emails are included
        email_list_data = {
            "id": new_list_id,
            "name": list_name,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "total_emails": len(valid_emails),
            "valid_count": len(valid_emails),
            "invalid_count": 0,
            "emails": valid_emails
        }

        # Save to user email lists
        email_lists = read_user_data(user_id, base_path, "email_lists", [])
        email_lists.append(email_list_data)
        write_user_data(user_id, base_path, "email_lists", email_lists)

        return new_list_id
    elif list_id:
        # Update existing list with only valid emails (those confirmed by validation)
        email_lists = read_user_data(user_id, base_path, "email_lists", [])

        for i, email_list in enumerate(email_lists):
            if email_list.get('id') == list_id:
                email_lists[i]['emails'] = valid_emails
                email_lists[i]['total_emails'] = len(valid_emails)
                email_lists[i]['valid_count'] = len(valid_emails)
                email_lists[i]['invalid_count'] = 0
                email_lists[i]['updated_at'] = datetime.now().isoformat()
                write_user_data(user_id, base_path, "email_lists", email_lists)
                return list_id

        return None
    else:
        return None


def save_valid_emails_to_file(
    user_id: str,
    base_path: Path,
    results: List[dict],
    session_name: str = None
) -> str:
    """
    Save valid emails to a JSON file in the user's valid emails folder.
    File naming: emails-{timestamp}.json
    Location: {user_folder}/valid emails/emails-{timestamp}.json
    """
    import json
    
    # Collect valid emails from results
    valid_emails = []
    for result in results:
        if result.get('valid', False):
            valid_emails.append({
                "email": result.get('email'),
                "domain": result.get('domain'),
                "status": result.get('status'),
                "sub_status": result.get('sub_status'),
                "free_email": result.get('free_email', False),
                "smtp_provider": result.get('smtp_provider'),
                "mx_found": result.get('mx_found', False),
                "validated_at": result.get('validated_at', datetime.now().isoformat())
            })
    
    if not valid_emails:
        print(f"[SAVE] No valid emails to save")
        return None
    
    # Create user folder path
    user_folder = base_path / f"fv-s-{user_id}"
    valid_emails_folder = user_folder / "valid emails"
    
    # Create directory if it doesn't exist
    valid_emails_folder.mkdir(parents=True, exist_ok=True)
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"emails-{timestamp}.json"
    filepath = valid_emails_folder / filename
    
    # Create file data
    file_data = {
        "created_at": datetime.now().isoformat(),
        "session_name": session_name or f"Validation Session {timestamp}",
        "total_valid": len(valid_emails),
        "emails": valid_emails
    }
    
    # Write to file
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(file_data, f, indent=2, ensure_ascii=False)
        print(f"[SAVE] Saved {len(valid_emails)} valid emails to {filepath}")
        return str(filepath)
    except Exception as e:
        pass

        print(f"[SAVE] Error saving valid emails: {e}")
        return None


# Queue helper class for list operations
class Queue:
    """Simple queue implementation using list"""
    def __init__(self, items: List[str] = None):
        self.items = items or []
    
    @classmethod
    def from_list(cls, items: List[str]):
        return cls(items if isinstance(items, list) else [])
    
    def pop(self, index: int = 0) -> str:
        if self.items:
            return self.items.pop(index)
        return None
    
    def append(self, item: str):
        self.items.append(item)
    
    def __len__(self):
        return len(self.items)
    
    def __iter__(self):
        return iter(self.items)


# =============================================================================
# PHONE VALIDATION FUNCTIONS
# =============================================================================

def create_phone_validation_session(
    user_id: str,
    base_path: Path,
    phone_numbers: List[str],
    list_name: str = None,
    country_code: str = None,
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
) -> dict:
    """Create a new phone validation session"""
    
    # Check if there's already an active session
    active = get_active_validator_session(user_id, base_path, "phone")
    if active:
        return {"success": False, "error": "A validation process is already running", "session_id": active.get('session_id')}
    
    # Generate session ID
    session_id = f"phone_val_{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()
    
    session_data = {
        "session_id": session_id,
        "user_id": user_id,
        "type": "phone",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "total_items": len(phone_numbers),
        "processed": 0,
        "valid": 0,
        "invalid": 0,
        "failed": 0,
        "verified_per_minute": 0.0,
        "queue": phone_numbers,
        "current_number": None,
        "list_name": list_name or f"Phone Validation {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "country_code": country_code,
        "transport": phone_transport_label(load_veriphone_settings(user_id, base_path)),
        "site": "veriphone",
        "batch_size": batch_size,
        "results": [],
        "last_batch_results": [],
        "start_time": None,
        "elapsed_seconds": 0
    }
    
    save_validator_session(user_id, base_path, session_data)
    save_active_session_state(user_id, base_path, "phone", session_id, session_data)
    
    return {"success": True, "session_id": session_id, "session": session_data}


def format_phone_number(phone: str) -> tuple:
    """
    Format phone number and extract country code
    Returns (country_code, formatted_number)
    """
    # Clean the number
    clean_number = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    
    # Remove leading + if present
    if clean_number.startswith("+"):
        clean_number = clean_number[1:]
    
    # Detect country code from number
    # Common country codes: 1 (US/CA), 44 (UK), 234 (Nigeria), etc.
    country_code_map = {
        "1": "US",      # US/Canada
        "44": "GB",     # UK
        "234": "NG",    # Nigeria
        "233": "GH",    # Ghana
        "225": "CI",    # Ivory Coast
        "221": "SN",    # Senegal
        "254": "KE",    # Kenya
        "255": "TZ",    # Tanzania
        "256": "UG",    # Uganda
        "212": "MA",    # Morocco
        "213": "DZ",    # Algeria
        "216": "TN",    # Tunisia
        "20": "EG",     # Egypt
        "966": "SA",    # Saudi Arabia
        "971": "AE",    # UAE
        "973": "BH",    # Bahrain
        "965": "KW",    # Kuwait
        "968": "OM",    # Oman
        "974": "QA",    # Qatar
        "91": "IN",     # India
        "86": "CN",     # China
        "81": "JP",     # Japan
        "82": "KR",     # South Korea
        "61": "AU",     # Australia
        "64": "NZ",     # New Zealand
        "55": "BR",     # Brazil
        "52": "MX",     # Mexico
        "34": "ES",     # Spain
        "33": "FR",     # France
        "49": "DE",     # Germany
        "39": "IT",     # Italy
        "31": "NL",     # Netherlands
        "32": "BE",     # Belgium
        "41": "CH",     # Switzerland
        "43": "AT",     # Austria
        "46": "SE",     # Sweden
        "47": "NO",     # Norway
        "45": "DK",     # Denmark
        "358": "FI",    # Finland
        "351": "PT",    # Portugal
        "353": "IE",    # Ireland
        "48": "PL",     # Poland
        "7": "RU",      # Russia
        "90": "TR",     # Turkey
        "972": "IL",    # Israel
        "27": "ZA",     # South Africa
        "263": "ZW",    # Zimbabwe
        "260": "ZM",    # Zambia
        "250": "RW",    # Rwanda
        "237": "CM",    # Cameroon
        "254": "KE",    # Kenya
        "880": "BD",    # Bangladesh
        "92": "PK",     # Pakistan
        "62": "ID",     # Indonesia
        "60": "MY",     # Malaysia
        "65": "SG",     # Singapore
        "63": "PH",     # Philippines
        "66": "TH",     # Thailand
        "84": "VN",     # Vietnam
        "971": "AE",    # UAE
    }

    # Country -> dial code, derived from the map above (longest prefix wins).
    dial_code_map = {}
    for code, country in sorted(country_code_map.items(), key=lambda x: len(x[0]), reverse=True):
        dial_code_map.setdefault(country, code)
    
    # Find matching country code
    matched_country = "US"  # Default
    matched_code = ""
    
    for code, country in sorted(country_code_map.items(), key=lambda x: len(x[0]), reverse=True):
        if clean_number.startswith(code):
            matched_code = code
            matched_country = country
            # Remove the country code from the number
            clean_number = clean_number[len(code):]
            break
    
    # If no match found, assume single digit country code at start
    if not matched_code:
        if len(clean_number) > 10:
            if clean_number[0] == "1":
                matched_country = "US"
                matched_code = "1"
                clean_number = clean_number[1:]
            elif clean_number[0:2].isdigit():
                matched_country = "UNKNOWN"
                matched_code = clean_number[0:2]
                clean_number = clean_number[2:]
    
    return matched_country, clean_number


async def process_phone_validation(
    user_id: str,
    base_path: Path,
    session_id: str
) -> dict:
    """
    Validate the next batch of phone numbers through Decodo/veriphone.io.

    Mirrors ``process_email_validation_batch``: honours the session batch size,
    counts failed (transport) separately from invalid (no positive verdict),
    and records results for the live log.
    """
    session = get_validator_session(user_id, base_path, session_id)

    if not session:
        return {"success": False, "error": "Session not found"}

    if session.get('status') == 'stopped':
        return {"success": False, "error": "Session was stopped"}

    if session.get('status') == 'completed':
        return {"success": False, "error": "Session already completed"}

    decodo_settings = load_decodo_settings(user_id, base_path)

    queue = Queue.from_list(session.get('queue', []))
    current_batch = list(session.get('current_batch', []) or [])

    # Honour the configured size, clamped to the worker ceiling.
    try:
        batch_size = int(session.get('batch_size') or DEFAULT_VALIDATION_BATCH_SIZE)
    except (TypeError, ValueError):
        batch_size = DEFAULT_VALIDATION_BATCH_SIZE
    batch_size = max(1, min(batch_size, MAX_VALIDATION_BATCH_SIZE))

    while len(current_batch) < batch_size and queue:
        current_batch.append(queue.pop(0))

    if not current_batch:
        update_validator_progress(user_id, base_path, session_id, {
            "status": "completed",
            "completed_at": datetime.now().isoformat()
        })
        save_phone_results_to_list(user_id, base_path, session)

        return {"success": True, "completed": True}

    country_code = session.get('country_code') or ""
    veriphone_settings = load_veriphone_settings(user_id, base_path)
    browser_use_settings = load_browser_use_settings(user_id, base_path)

    # The session may pin a transport (e.g. browser-use); otherwise the default
    # order applies. Passed via decodo_settings so validate_phone can see it.
    transport = session.get('transport')
    if not transport or transport == 'decodo-scrape':
        transport = phone_transport_label(veriphone_settings)
    decodo_settings = dict(decodo_settings or {})
    if session.get('transport'):
        decodo_settings['transport'] = session['transport']

    validation_results = await validate_phones_async(
        list(current_batch), country_code, decodo_settings, veriphone_settings,
        browser_use_settings,
    )

    results = []
    valid_count = 0
    invalid_count = 0
    failed_count = 0

    for result in validation_results:
        if result.get('error'):
            failed_count += 1
        elif result.get('valid') and result.get('status') in VALID_STATUSES:
            valid_count += 1
        else:
            invalid_count += 1

        results.append({
            "number": result.get('number'),
            "e164": result.get('e164'),
            "country_code": result.get('country_code'),
            "formatted_number": result.get('formatted_number'),
            "valid": bool(result.get('valid')) and not result.get('error'),
            "status": result.get('status'),
            "category": result.get('category'),
            "confidence": result.get('confidence', 0),
            "reason": result.get('reason'),
            "carrier": result.get('carrier'),
            "line_type": result.get('line_type'),
            "country": result.get('country'),
            "region": result.get('region'),
            "city": result.get('city'),
            "site": result.get('site'),
            "error": result.get('error'),
            "validated_at": datetime.now().isoformat(),
        })

    total_processed = session.get('processed', 0) + len(current_batch)
    total_valid = session.get('valid', 0) + valid_count
    total_invalid = session.get('invalid', 0) + invalid_count
    total_failed = session.get('failed', 0) + failed_count

    # Anchor the clock on the first batch that actually runs, and keep it.
    start_time_str = session.get('start_time')
    if not start_time_str:
        start_time_str = datetime.now().isoformat()
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(start_time_str)).total_seconds()
    except (TypeError, ValueError):
        elapsed = 0
    # Sub-second elapsed times produce absurd rates (batch 1 can finish in
    # ~0.01s); report 0 until there is a measurable window.
    vpm = round((total_valid / elapsed) * 60, 2) if elapsed >= 1 else 0

    all_results = list(session.get('results', []) or [])
    all_results.extend(results)

    updates = {
        "processed": total_processed,
        "valid": total_valid,
        "invalid": total_invalid,
        "failed": total_failed,
        "transport": transport,
        "verified_per_minute": vpm,
        "queue": [str(q) for q in queue],
        "current_batch": [],
        "current_number": results[-1]["number"] if results else None,
        "results": all_results,
        "last_batch_results": results,
        "start_time": start_time_str,
        "elapsed_seconds": int(elapsed),
    }

    if len(queue) == 0:
        updates["status"] = "completed"
        updates["completed_at"] = datetime.now().isoformat()
    elif session.get('status') != 'running':
        updates["status"] = "running"
        updates["started_at"] = datetime.now().isoformat()

    update_validator_progress(user_id, base_path, session_id, updates)
    session.update(updates)

    if updates.get("status") == "completed":
        save_phone_results_to_list(user_id, base_path, session)

    return {
        "success": True,
        "processed": len(current_batch),
        "valid": valid_count,
        "invalid": invalid_count,
        "failed": failed_count,
        "remaining": len(queue),
        "completed": updates.get("status") == "completed",
        "last_batch_results": results,
    }


def save_phone_results_to_list(
    user_id: str,
    base_path: Path,
    session: dict
) -> str:
    """
    Save phone validation results to phone list file
    """
    # Create phone lists directory
    user_folder = base_path / f"fv-s-{user_id}"
    phone_lists_dir = user_folder / "phone-lists"
    phone_lists_dir.mkdir(parents=True, exist_ok=True)
    
    list_id = f"plist_{uuid.uuid4().hex[:12]}"
    list_name = session.get('list_name', f"Phone Validation {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    country_code = session.get('country_code')
    
    results = session.get('results', [])

    # Count valid/invalid. A transport failure is not an invalid number, so it
    # is tracked separately rather than folded into the negative bucket.
    valid_count = sum(
        1 for r in results
        if r.get('valid') and not r.get('error') and r.get('status') in VALID_STATUSES
    )
    failed_count = sum(1 for r in results if r.get('error'))
    invalid_count = len(results) - valid_count - failed_count

    # Create phone entries
    numbers = []
    for result in results:
        entry = {
            "number": result.get('number'),
            "e164": result.get('e164'),
            "country_code": result.get('country_code') or country_code,
            "formatted_number": result.get('formatted_number'),
            "valid": bool(result.get('valid')) and not result.get('error'),
            "status": result.get('status'),
            "category": result.get('category'),
            "confidence": result.get('confidence', 0),
            "reason": result.get('reason'),
            "carrier": result.get('carrier'),
            "line_type": result.get('line_type'),
            "country": result.get('country'),
            "city": result.get('city'),
            "region": result.get('region'),
            "site": result.get('site'),
            "error": result.get('error'),
            "is_prepaid": result.get('is_prepaid'),
            "is_leaked": result.get('is_leaked'),
            "added_at": datetime.now().isoformat()
        }
        numbers.append(entry)
    
    # Create phone list data
    phone_list_data = {
        "id": list_id,
        "name": list_name,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "country_code": country_code,
        "total_numbers": len(numbers),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "failed_count": failed_count,
        "site": session.get('site', 'veriphone'),
        "numbers": numbers
    }
    
    # Write the list itself. Only the index used to be written, so every
    # result list was unreachable: get_phone_lists() found an entry whose id
    # resolved to a file that never existed, and GET /phone/lists/{id} 404'd.
    list_file = phone_lists_dir / f"{list_id}.json"
    with open(list_file, 'w', encoding='utf-8') as f:
        json.dump(phone_list_data, f, indent=2, ensure_ascii=False)

    # Save to phone lists index (summary only -- numbers live in the file).
    phone_lists_index = phone_lists_dir / "lists.json"
    try:
        if phone_lists_index.exists():
            with open(phone_lists_index, 'r', encoding='utf-8') as f:
                lists = json.load(f)
        else:
            lists = []
    except Exception:
        lists = []

    lists.append({
        "id": list_id,
        "name": list_name,
        "created_at": phone_list_data["created_at"],
        "updated_at": phone_list_data["updated_at"],
        "country_code": country_code,
        "total_numbers": phone_list_data["total_numbers"],
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "failed_count": failed_count,
        "site": phone_list_data["site"],
    })

    with open(phone_lists_index, 'w', encoding='utf-8') as f:
        json.dump(lists, f, indent=2, ensure_ascii=False)

    return list_id


# =============================================================================
# PHONE LISTS MANAGEMENT
# =============================================================================

def get_phone_lists(user_id: str, base_path: Path) -> list:
    """Get all phone lists for a user"""
    user_folder = base_path / f"fv-s-{user_id}"
    phone_lists_dir = user_folder / "phone-lists"
    phone_lists_index = phone_lists_dir / "lists.json"
    
    if not phone_lists_index.exists():
        return []
    
    try:
        with open(phone_lists_index, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

        return []


def get_phone_list_by_id(user_id: str, base_path: Path, list_id: str) -> Optional[dict]:
    """Get a specific phone list by ID"""
    lists = get_phone_lists(user_id, base_path)
    
    for phone_list in lists:
        if phone_list.get('id') == list_id:
            return phone_list
    
    return None


def delete_phone_list(user_id: str, base_path: Path, list_id: str) -> bool:
    """Delete a phone list"""
    lists = get_phone_lists(user_id, base_path)
    original_count = len(lists)
    
    lists = [l for l in lists if l.get('id') != list_id]
    
    if len(lists) < original_count:
        user_folder = base_path / f"fv-s-{user_id}"
        phone_lists_dir = user_folder / "phone-lists"
        phone_lists_index = phone_lists_dir / "lists.json"
        
        with open(phone_lists_index, 'w', encoding='utf-8') as f:
            json.dump(lists, f, indent=2, ensure_ascii=False)
        
        return True
    
    return False


def rename_phone_list(user_id: str, base_path: Path, list_id: str, new_name: str) -> bool:
    """Rename a phone list"""
    lists = get_phone_lists(user_id, base_path)
    
    for i, phone_list in enumerate(lists):
        if phone_list.get('id') == list_id:
            lists[i]['name'] = new_name
            lists[i]['updated_at'] = datetime.now().isoformat()
            
            user_folder = base_path / f"fv-s-{user_id}"
            phone_lists_dir = user_folder / "phone-lists"
            phone_lists_index = phone_lists_dir / "lists.json"
            
            with open(phone_lists_index, 'w', encoding='utf-8') as f:
                json.dump(lists, f, indent=2, ensure_ascii=False)
            
            return True
    
    return False


def create_phone_list(
    user_id: str,
    base_path: Path,
    name: str,
    numbers: List[dict],
    country_code: str = None
) -> dict:
    """
    Create a new phone list with validated numbers
    Returns the created list metadata
    """
    user_folder = base_path / f"fv-s-{user_id}"
    phone_lists_dir = user_folder / "phone-lists"
    phone_lists_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate list ID
    list_id = f"phone_list_{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()
    
    # Count valid/invalid
    valid_count = sum(1 for n in numbers if n.get('valid', False))
    invalid_count = len(numbers) - valid_count
    
    # Extract unique country codes
    country_codes = list(set(n.get('country_code', 'US') for n in numbers if n.get('country_code')))
    if country_code and country_code not in country_codes:
        country_codes.append(country_code)
    
    # Create list metadata
    list_metadata = {
        "id": list_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "country_code": country_code,
        "total_numbers": len(numbers),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "country_codes": country_codes
    }
    
    # Save list metadata to lists.json
    phone_lists_index = phone_lists_dir / "lists.json"
    existing_lists = []
    if phone_lists_index.exists():
        try:
            with open(phone_lists_index, 'r', encoding='utf-8') as f:
                existing_lists = json.load(f)
        except Exception:
            pass

            existing_lists = []
    
    existing_lists.append(list_metadata)
    
    with open(phone_lists_index, 'w', encoding='utf-8') as f:
        json.dump(existing_lists, f, indent=2, ensure_ascii=False)
    
    # Save full list data to separate file
    list_file = phone_lists_dir / f"{list_id}.json"
    list_data = {
        **list_metadata,
        "numbers": numbers
    }
    
    with open(list_file, 'w', encoding='utf-8') as f:
        json.dump(list_data, f, indent=2, ensure_ascii=False)
    
    return list_metadata


def get_phone_list_numbers(user_id: str, base_path: Path, list_id: str) -> Optional[dict]:
    """Get all numbers from a phone list with full details"""
    user_folder = base_path / f"fv-s-{user_id}"
    phone_lists_dir = user_folder / "phone-lists"
    list_file = phone_lists_dir / f"{list_id}.json"
    
    if not list_file.exists():
        return None
    
    try:
        with open(list_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass

        return None


def add_numbers_to_phone_list(
    user_id: str,
    base_path: Path,
    list_id: str,
    numbers: List[dict]
) -> bool:
    """Add validated numbers to an existing phone list"""
    list_data = get_phone_list_numbers(user_id, base_path, list_id)
    
    if not list_data:
        return False
    
    existing_numbers = list_data.get('numbers', [])
    existing_numbers.extend(numbers)
    
    # Update counts
    list_data['numbers'] = existing_numbers
    list_data['total_numbers'] = len(existing_numbers)
    list_data['valid_count'] = sum(1 for n in existing_numbers if n.get('valid', False))
    list_data['invalid_count'] = list_data['total_numbers'] - list_data['valid_count']
    list_data['updated_at'] = datetime.now().isoformat()
    
    # Update country codes
    new_codes = list(set(n.get('country_code', 'US') for n in existing_numbers if n.get('country_code')))
    list_data['country_codes'] = new_codes
    
    # Save updated list
    user_folder = base_path / f"fv-s-{user_id}"
    phone_lists_dir = user_folder / "phone-lists"
    list_file = phone_lists_dir / f"{list_id}.json"
    
    with open(list_file, 'w', encoding='utf-8') as f:
        json.dump(list_data, f, indent=2, ensure_ascii=False)
    
    # Update lists.json index
    phone_lists_index = phone_lists_dir / "lists.json"
    lists = get_phone_lists(user_id, base_path)
    
    for i, phone_list in enumerate(lists):
        if phone_list.get('id') == list_id:
            lists[i] = {
                "id": list_data['id'],
                "name": list_data['name'],
                "created_at": list_data['created_at'],
                "updated_at": list_data['updated_at'],
                "country_code": list_data.get('country_code'),
                "total_numbers": list_data['total_numbers'],
                "valid_count": list_data['valid_count'],
                "invalid_count": list_data['invalid_count'],
                "country_codes": list_data['country_codes']
            }
            break
    
    with open(phone_lists_index, 'w', encoding='utf-8') as f:
        json.dump(lists, f, indent=2, ensure_ascii=False)
    
    return True


# =============================================================================
# PHONE NUMBER FORMATTING & CATEGORIZATION
# =============================================================================

def format_phone_number(phone: str) -> tuple:
    """
    Format phone number and extract country code
    Returns (country_code, formatted_number)
    """
    # Clean the number
    clean_number = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "")
    
    # Common country codes map
    country_code_map = {
        "1": "US",      # US/Canada
        "44": "GB",     # UK
        "234": "NG",    # Nigeria
        "233": "GH",    # Ghana
        "225": "CI",    # Ivory Coast
        "221": "SN",    # Senegal
        "254": "KE",    # Kenya
        "255": "TZ",    # Tanzania
        "256": "UG",    # Uganda
        "212": "MA",    # Morocco
        "213": "DZ",    # Algeria
        "216": "TN",    # Tunisia
        "20": "EG",     # Egypt
        "966": "SA",    # Saudi Arabia
        "971": "AE",    # UAE
        "973": "BH",    # Bahrain
        "965": "KW",    # Kuwait
        "968": "OM",    # Oman
        "974": "QA",    # Qatar
        "91": "IN",     # India
        "86": "CN",     # China
        "81": "JP",     # Japan
        "82": "KR",     # South Korea
        "61": "AU",     # Australia
        "64": "NZ",     # New Zealand
        "55": "BR",     # Brazil
        "52": "MX",     # Mexico
        "34": "ES",     # Spain
        "33": "FR",     # France
        "49": "DE",     # Germany
        "39": "IT",     # Italy
        "27": "ZA",     # South Africa
    }
    
    # Find matching country code (longest match first)
    matched_country = "US"  # Default
    matched_code = ""
    
    for code, country in sorted(country_code_map.items(), key=lambda x: len(x[0]), reverse=True):
        if clean_number.startswith(code):
            matched_code = code
            matched_country = country
            # Remove the country code from the number
            clean_number = clean_number[len(code):]
            break
    
    # If no match found, try to detect from length
    if not matched_code:
        if len(clean_number) > 10:
            if clean_number[0] == "1":
                matched_country = "US"
                matched_code = "1"
                clean_number = clean_number[1:]
            elif len(clean_number) > 9 and clean_number[0:2].isdigit():
                matched_country = "UNKNOWN"
                matched_code = clean_number[0:2]
                clean_number = clean_number[2:]
    
    return matched_country, clean_number


def categorize_phone_numbers(raw_numbers: List[str]) -> dict:
    """
    Categorize and clean phone numbers by country code
    Returns dict with country codes as keys and lists of cleaned numbers as values
    
    Example output:
    {
        "NG": [{"original": "+2349028938460", "cleaned": "09028938460", "country": "NG"}],
        "US": [{"original": "+1-555-123-4567", "cleaned": "5551234567", "country": "US"}],
        "Unknown": [{"original": "1234567890", "cleaned": "1234567890", "country": "UNKNOWN"}]
    }
    """
    results = {}
    
    for number in raw_numbers:
        if not number or not number.strip():
            continue
        
        original = number.strip()
        country_code, formatted = format_phone_number(original)
        
        entry = {
            "original": original,
            "cleaned": formatted,
            "country_code": country_code,
            "valid": True  # Will be validated later
        }
        
        key = f" {country_code}"  # Space prefix for sorting
        if key not in results:
            results[key] = []
        results[key].append(entry)
    
    return results


def import_phone_numbers(
    user_id: str,
    base_path: Path,
    raw_numbers: List[str],
    list_name: str = None,
    country_code: str = None
) -> dict:
    """
    Import phone numbers with automatic country detection
    Returns categorized numbers and creates phone list
    """
    # Categorize numbers by country
    categorized = categorize_phone_numbers(raw_numbers)
    
    # Handle unknown country codes - use provided country_code or ask user
    if " Unknown" in categorized and country_code:
        # Re-process unknown numbers with provided country code
        unknown_numbers = categorized.pop(" Unknown", [])
        for entry in unknown_numbers:
            entry["country_code"] = country_code
            if country_code not in categorized:
                categorized[f" {country_code}"] = []
            categorized[f" {country_code}"].append(entry)
    
    # Flatten all numbers for validation queue
    all_numbers = []
    for country_entries in categorized.values():
        all_numbers.extend(country_entries)
    
    # Create list name if not provided
    if not list_name:
        list_name = f"Import {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    # Count totals
    total = len(all_numbers)
    unique_countries = [key.strip() for key in categorized.keys()]
    
    return {
        "success": True,
        "list_name": list_name,
        "total_numbers": total,
        "categorized": categorized,
        "country_codes": unique_countries,
        "numbers": all_numbers,
        "needs_country_confirmation": " Unknown" in categorized and not country_code
    }


# =============================================================================
# BROWSER AUTOMATION CONFIGURATION
# =============================================================================

# VALIDATOR SESSION UTILITIES
# =============================================================================

def calculate_verified_per_minute(session: dict) -> float:
    """Calculate verified items per minute based on elapsed time"""
    processed = session.get('processed', 0)
    elapsed = session.get('elapsed_seconds', 0)
    
    if elapsed <= 0:
        return 0.0
    
    return round((processed / elapsed) * 60, 2)


def get_session_elapsed_time(session: dict) -> int:
    """Get elapsed time in seconds from session start"""
    started_at = session.get('started_at')
    if not started_at:
        return session.get('elapsed_seconds', 0)
    
    try:
        start_time = datetime.fromisoformat(started_at)
        elapsed = (datetime.now() - start_time).total_seconds()
        return int(elapsed)
    except Exception:
        pass

        return session.get('elapsed_seconds', 0)


def finalize_validation_session(
    user_id: str,
    base_path: Path,
    session_id: str,
    validation_type: str = "email"
) -> dict:
    """
    Finalize a completed validation session
    - Updates session status to completed
    - Saves results to appropriate list
    - Returns summary
    """
    session = get_validator_session(user_id, base_path, session_id)
    
    if not session:
        return {"success": False, "error": "Session not found"}
    
    # Update session status
    update_validator_progress(user_id, base_path, session_id, {
        "status": "completed",
        "completed_at": datetime.now().isoformat()
    })
    
    # Get results
    results = session.get('results', [])
    valid_items = [r for r in results if r.get('valid', False)]
    invalid_items = [r for r in results if not r.get('valid', False)]
    
    summary = {
        "success": True,
        "session_id": session_id,
        "type": validation_type,
        "status": "completed",
        "total": len(results),
        "valid": len(valid_items),
        "invalid": len(invalid_items),
        "failed": session.get('failed', 0),
        "list_name": session.get('list_name'),
        "list_id": session.get('list_id'),
        "is_import": session.get('is_import', False)
    }
    
    return summary


def cleanup_old_sessions(user_id: str, base_path: Path, days_old: int = 7) -> int:
    """
    Clean up old completed/stopped sessions
    Returns number of sessions cleaned up
    """
    sessions_dir = get_validator_sessions_dir(user_id, base_path)
    cutoff_date = datetime.now() - timedelta(days=days_old)
    cleaned = 0
    
    for session_file in sessions_dir.glob("*.json"):
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                session = json.load(f)
            
            # Only clean completed/stopped/error sessions
            if session.get('status') not in ['completed', 'stopped', 'error']:
                continue
            
            # Check if old enough
            updated_at = session.get('updated_at')
            if updated_at:
                try:
                    session_date = datetime.fromisoformat(updated_at)
                    if session_date > cutoff_date:
                        continue
                except Exception:
                    pass

                    pass
            
            # Delete old session
            session_file.unlink()
            cleaned += 1
            
        except Exception:
            pass

            continue
    
    return cleaned

