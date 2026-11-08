"""
FV-S Admin Panel Configuration
Central configuration settings for the server
"""

import os
import base64
import hashlib
import secrets as _secrets
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET_AVAILABLE = True
except ImportError:
    Fernet = None
    InvalidToken = Exception
    _FERNET_AVAILABLE = False


# =============================================================================
# PATH CONFIGURATION
# =============================================================================

# Base directory for the server
BASE_DIR = Path(__file__).parent.resolve()

# Users data directory
USERS_BASE_PATH = BASE_DIR / "users"


# =============================================================================
# SERVER CONFIGURATION
# =============================================================================

class ServerConfig(BaseModel):
    """Server configuration settings"""
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True
    reload: bool = True

    # Public base URL used to build absolute links (e.g. unsubscribe).
    public_url: str = "http://localhost:8000"

    # Security
    secret_key: str = ""  # Empty => auto-generated and persisted (see get_secret_key)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # CORS
    cors_origins: list = ["*"]
    cors_credentials: bool = False
    cors_methods: list = ["*"]
    cors_headers: list = ["*"]

    # SMTP Testing
    smtp_timeout: int = 20
    smtp_default_port: int = 587
    smtp_ssl_port: int = 465
    smtp_starttls_port: int = 587

    # File Upload
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    allowed_extensions: list = [".txt", ".csv", ".json"]

    # Rate Limiting
    rate_limit_per_minute: int = 60


# =============================================================================
# CONFIGURATION INSTANCE
# =============================================================================

config = ServerConfig()

# The legacy, hardcoded secret used before secret persistence was introduced.
# Kept so that password hashes created with the old default still verify.
LEGACY_SECRET_KEY = "fv-s-secret-key-change-in-production"


def get_secret_key() -> str:
    """
    Resolve the server secret used for JWT signing and password hashing.

    Resolution order:
      1. ``FV_S_SECRET_KEY`` environment variable (recommended in production)
      2. a persisted key at ``data/secret.key``
      3. auto-generate a new key and persist it (first run)

    Returns a stable string; subsequent calls return the same value.
    """
    env_key = os.getenv("FV_S_SECRET_KEY")
    if env_key:
        return env_key

    key_file = BASE_DIR / "data" / "secret.key"
    try:
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
    except Exception:
        pass

    key = _secrets.token_hex(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
    except Exception:
        pass
    return key


# =============================================================================
# CREDENTIAL ENCRYPTION (Phase 10)
# =============================================================================

def get_encryption_key() -> bytes:
    """Derive a Fernet key from env or server secret."""
    env_key = os.getenv("FV_S_ENCRYPTION_KEY") or os.getenv("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key
    digest = hashlib.sha256(get_secret_key().encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a mailbox/API password for database storage."""
    if not plaintext:
        return ""
    if not _FERNET_AVAILABLE:
        return plaintext
    return Fernet(get_encryption_key()).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored mailbox/API password."""
    if not ciphertext:
        return ""
    if not _FERNET_AVAILABLE:
        return ciphertext
    try:
        return Fernet(get_encryption_key()).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ciphertext


# =============================================================================
# DIRECTORY SETUP
# =============================================================================

def setup_directories():
    """Create necessary directories if they don't exist"""
    # Users directory
    USERS_BASE_PATH.mkdir(parents=True, exist_ok=True)
    
    print(f"Users directory: {USERS_BASE_PATH}")


# =============================================================================
# ENVIRONMENT VARIABLES (Optional Override)
# =============================================================================

def load_env_config():
    """Load configuration from environment variables (optional)"""
    global config, proxy_config, decodo_config, browser_config

    # Override with environment variables if set
    if os.getenv("FV_S_HOST"):
        config.host = os.getenv("FV_S_HOST")

    if os.getenv("FV_S_PORT"):
        config.port = int(os.getenv("FV_S_PORT"))

    if os.getenv("FV_S_SECRET_KEY"):
        config.secret_key = os.getenv("FV_S_SECRET_KEY")

    if os.getenv("FV_S_PUBLIC_URL"):
        config.public_url = os.getenv("FV_S_PUBLIC_URL").rstrip("/")

    if os.getenv("FV_S_DEBUG"):
        config.debug = os.getenv("FV_S_DEBUG").lower() == "true"

    if os.getenv("FV_S_RELOAD"):
        config.reload = os.getenv("FV_S_RELOAD").lower() == "true"

    # Proxy credentials (were previously hardcoded in this file)
    if os.getenv("FV_S_PROXY_HOST"):
        proxy_config.hardcoded_proxy_host = os.getenv("FV_S_PROXY_HOST")
    if os.getenv("FV_S_PROXY_PORT"):
        proxy_config.hardcoded_proxy_port = int(os.getenv("FV_S_PROXY_PORT"))
    if os.getenv("FV_S_PROXY_USERNAME"):
        proxy_config.hardcoded_proxy_username = os.getenv("FV_S_PROXY_USERNAME")
    if os.getenv("FV_S_PROXY_PASSWORD"):
        proxy_config.hardcoded_proxy_password = os.getenv("FV_S_PROXY_PASSWORD")

    # Decodo scraping API key
    if os.getenv("FV_S_DECODO_API_KEY"):
        decodo_config.api_key = os.getenv("FV_S_DECODO_API_KEY")

    # Bright Data browser URL (contains embedded credentials)
    if os.getenv("FV_S_BROWSER_URL"):
        browser_config.browser_url = os.getenv("FV_S_BROWSER_URL")


# =============================================================================
# FILE CONSTANTS
# =============================================================================

class FileNames:
    """Standard file names for user data storage"""
    LOGS = "logs.json"           # User credentials (username, password hash)
    SMTP = "smtp.json"           # SMTP credentials
    API_CREDENTIALS = "api.json" # API credentials (Mailgun, SendGrid, etc.)
    ABOUT_TXT = "about.txt"      # User system information (text format)
    ABOUT_JSON = "about.json"    # User system information (JSON format)
    MAILBOX = "mailbox.json"     # Microsoft Graph/OAuth2 configuration
    MAILBOX_SENDERS = "mailbox_senders.json"  # Sender user IDs
    CAMPAIGNS = "campaigns.json" # Campaign configurations
    SETTINGS = "settings.json"   # User settings/preferences
    EMAIL_LISTS = "email_lists"  # Directory for email lists
    SUBJECTS = "subjects.json"   # Subject templates
    FROM_NAMES = "from_names.json"  # From name templates
    LETTERS = "letters"          # Directory for letter templates
    EMAILS_DIR = "emails"        # Directory for email list files


class UserFolderPrefix:
    """Prefix for user folders"""
    PREFIX = "fv-s-"
    
    @classmethod
    def generate(cls, user_id: str) -> str:
        """Generate folder name from user ID"""
        return f"{cls.PREFIX}{user_id}"
    
    @classmethod
    def extract_user_id(cls, folder_name: str) -> Optional[str]:
        """Extract user ID from folder name"""
        if folder_name.startswith(cls.PREFIX):
            return folder_name[len(cls.PREFIX):]
        return None


# =============================================================================
# DEFAULT VALUES
# =============================================================================

class Defaults:
    """Default values for new users"""
    USERNAME = "admin"
    PASSWORD = "admin"
    
    SMTP_DEFAULTS = {
        "port": 587,
        "status": "active",
        "success_rate": 0
    }
    
    API_DEFAULTS = {
        "region": "us-east-1",
        "status": "active",
        "success_rate": 0
    }


# =============================================================================
# PROVIDER MAPPINGS
# =============================================================================

class Providers:
    """SMTP and API provider information"""
    
    SMTP = {
        "gmail": {
            "name": "Gmail / Google Workspace",
            "host": "smtp.gmail.com",
            "port": 587,
            "ssl_port": 465
        },
        "yahoo": {
            "name": "Yahoo Mail",
            "host": "smtp.mail.yahoo.com",
            "port": 587,
            "ssl_port": 465
        },
        "outlook": {
            "name": "Outlook / Microsoft 365",
            "host": "smtp.office365.com",
            "port": 587,
            "ssl_port": 465
        },
        "aws": {
            "name": "Amazon SES",
            "host": "email-smtp.us-east-1.amazonaws.com",
            "port": 587,
            "ssl_port": 465
        },
        "mailgun": {
            "name": "Mailgun",
            "host": "smtp.mailgun.org",
            "port": 587,
            "ssl_port": 465
        },
        "sendgrid": {
            "name": "SendGrid",
            "host": "smtp.sendgrid.net",
            "port": 587,
            "ssl_port": 465
        },
        "smtp2go": {
            "name": "SMTP2GO",
            "host": "mail.smtp2go.com",
            "port": 587,
            "ssl_port": 465
        },
        "mailjet": {
            "name": "Mailjet",
            "host": "in-v3.mailjet.com",
            "port": 587,
            "ssl_port": 465
        },
        "postmark": {
            "name": "Postmark",
            "host": "smtp.postmarkapp.com",
            "port": 587,
            "ssl_port": 465
        },
        "custom": {
            "name": "Custom SMTP Server",
            "host": "",
            "port": 587,
            "ssl_port": 465
        }
    }
    
    API = {
        "mailgun": {
            "name": "Mailgun API",
            "base_url": "https://api.mailgun.net/v3"
        },
        "sendgrid": {
            "name": "SendGrid API",
            "base_url": "https://api.sendgrid.com/v3"
        },
        "mailjet": {
            "name": "Mailjet API",
            "base_url": "https://api.mailjet.com/v3.1"
        },
        "postmark": {
            "name": "Postmark API",
            "base_url": "https://api.postmarkapp.com/v1"
        },
        "aws_ses": {
            "name": "Amazon SES API",
            "base_url": "https://email.{region}.amazonaws.com"
        }
    }


# =============================================================================
# PROXY CONFIGURATION
# =============================================================================

class ProxyConfig(BaseModel):
    """Proxy settings for email/phone validation.

    Credentials are supplied via environment variables (FV_S_PROXY_HOST, etc.)
    so no secrets are committed to the repository.
    """
    use_hardcoded_proxy: bool = True

    hardcoded_proxy_host: str = ""
    hardcoded_proxy_port: int = 10001
    hardcoded_proxy_username: str = ""
    hardcoded_proxy_password: str = ""

    @property
    def hardcoded_proxy_url(self) -> str:
        """Build the full proxy URL from the configured credentials."""
        return f"http://{self.hardcoded_proxy_username}:{self.hardcoded_proxy_password}@{self.hardcoded_proxy_host}:{self.hardcoded_proxy_port}"


proxy_config = ProxyConfig()


# =============================================================================
# DECODO WEB SCRAPING API CONFIGURATION
# =============================================================================

class DecodoApiConfig(BaseModel):
    """Decodo API settings for web scraping.

    The API key is supplied via the FV_S_DECODO_API_KEY environment variable
    so no credentials are committed to the repository.
    """
    use_decodo_api: bool = True

    api_key: str = ""

    # Decodo API endpoint
    api_url: str = "https://scraper-api.decodo.com/v2/scrape"

    # Target URL to scrape (skrapp.io email verifier)
    target_url: str = "https://skrapp.io/email-verifier"

    # Use JavaScript rendering to get dynamic content
    headless_mode: str = "html"  # "html" for rendered HTML, "png" for screenshot

    # Use target "universal" for browser actions
    target_type: str = "universal"


decodo_config = DecodoApiConfig()


# =============================================================================
# BROWSER AUTOMATION CONFIGURATION
# =============================================================================

class BrowserConfig(BaseModel):
    """Browser automation settings for email/phone validation.

    The Bright Data browser URL (which embeds credentials) is supplied via the
    FV_S_BROWSER_URL environment variable so nothing secret is committed.
    """
    use_browser_api: bool = False  # True = Bright Data, False = Local Playwright

    browser_url: str = ""

    # Local Playwright settings
    local_browser_headless: bool = True  # Default, auto-overridden if display available
    local_browser_timeout: int = 30


browser_config = BrowserConfig()


# =============================================================================
# INITIALIZATION
# =============================================================================

def init_config():
    """Initialize configuration and create directories"""
    setup_directories()
    load_env_config()


# Run initialization
init_config()