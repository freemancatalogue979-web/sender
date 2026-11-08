"""Tests for configuration and secret management."""
import base64

import pytest


def test_secret_key_from_env_is_stable(monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", "env-secret-abc")
    from config import get_secret_key
    assert get_secret_key() == "env-secret-abc"
    assert get_secret_key() == "env-secret-abc"


def test_encryption_round_trip(fixed_secret, monkeypatch):
    # Force the encryption key to derive from our pinned secret.
    monkeypatch.setenv("FV_S_SECRET_KEY", fixed_secret)
    monkeypatch.delenv("FV_S_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

    from config import encrypt_secret, decrypt_secret

    plain = "s3cr3t-password"
    cipher = encrypt_secret(plain)
    assert cipher != plain
    assert decrypt_secret(cipher) == plain


def test_decrypt_plaintext_is_passthrough(fixed_secret, monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", fixed_secret)
    monkeypatch.delenv("FV_S_ENCRYPTION_KEY", raising=False)
    from config import decrypt_secret

    # Legacy plaintext values decrypt to themselves (graceful migration).
    assert decrypt_secret("legacy-plaintext") == "legacy-plaintext"
    assert decrypt_secret("") == ""


def test_get_encryption_key_is_urlsafe_b64(fixed_secret, monkeypatch):
    monkeypatch.setenv("FV_S_SECRET_KEY", fixed_secret)
    monkeypatch.delenv("FV_S_ENCRYPTION_KEY", raising=False)
    from config import get_encryption_key
    key = get_encryption_key()
    # Fernet keys are 44-char url-safe base64.
    assert len(key) == 44
    base64.urlsafe_b64decode(key)  # must not raise


def test_hardcoded_secrets_removed():
    """No live credentials should be committed in config defaults."""
    from config import proxy_config, decodo_config
    assert proxy_config.hardcoded_proxy_username == ""
    assert proxy_config.hardcoded_proxy_password == ""
    assert decodo_config.api_key == ""
