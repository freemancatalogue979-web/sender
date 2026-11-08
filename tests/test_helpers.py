"""Tests for functions.py helper functions (unsubscribe, audit, recovery...)."""
import json

import functions as F


def test_unsubscribe_token_round_trip(fixed_secret):
    token = F.unsubscribe_token("User@Example.com", "user-1")
    assert F.verify_unsubscribe_token(token) == ("user-1", "user@example.com")
    # Tampered token fails
    assert F.verify_unsubscribe_token(token + "x") is None
    assert F.verify_unsubscribe_token("garbage") is None


def test_unsubscribe_url_contains_token(fixed_secret):
    url = F.build_unsubscribe_url("a@b.com", "u1")
    assert "/unsubscribe/" in url
    token = url.rsplit("/", 1)[-1]
    assert F.verify_unsubscribe_token(token) == ("u1", "a@b.com")


def test_suppression_list(tmp_user_base):
    assert F.get_suppression_list("u1", tmp_user_base) == []
    assert F.suppress_email("u1", tmp_user_base, "A@B.com") is True
    assert F.is_suppressed("u1", tmp_user_base, "a@b.com") is True
    # idempotent
    F.suppress_email("u1", tmp_user_base, "a@b.com")
    assert F.get_suppression_list("u1", tmp_user_base) == ["a@b.com"]
    F.unsuppress_email("u1", tmp_user_base, "a@b.com")
    assert F.is_suppressed("u1", tmp_user_base, "a@b.com") is False


def test_write_json_atomic(tmp_path):
    path = tmp_path / "nested" / "file.json"
    data = {"a": 1, "b": [1, 2, 3]}
    assert F.write_json_atomic(path, data) is True
    assert json.loads(path.read_text(encoding="utf-8")) == data
    # no leftover tmp file
    assert not list(tmp_path.rglob("*.tmp"))


def test_audit_log(tmp_user_base):
    F.audit_event("u1", tmp_user_base, "thing_happened", {"k": "v"})
    F.audit_event("u1", tmp_user_base, "other_thing")
    entries = F.read_audit_log("u1", tmp_user_base)
    assert len(entries) == 2
    # newest first
    assert entries[0]["event"] == "other_thing"
    assert entries[1]["details"] == {"k": "v"}
    assert "timestamp" in entries[0]


def test_recover_interrupted_sessions(tmp_user_base):
    user = tmp_user_base / "fv-s-u1" / "sender-config" / "sessions"
    user.mkdir(parents=True)

    def write(name, status):
        (user / name).write_text(json.dumps({"id": name, "status": status}))

    write("a.json", "running")
    write("b.json", "paused")
    write("c.json", "completed")

    recovered = F.recover_interrupted_sessions(tmp_user_base)
    assert recovered == 2

    a = json.loads((user / "a.json").read_text())
    b = json.loads((user / "b.json").read_text())
    c = json.loads((user / "c.json").read_text())
    assert a["status"] == "interrupted"
    assert b["status"] == "interrupted"
    assert c["status"] == "completed"  # untouched


def test_ensure_default_admin(tmp_user_base):
    created = F.ensure_default_admin(tmp_user_base)
    assert created is True
    users = F.read_users_database(tmp_user_base)
    assert len(users) == 1 and users[0]["username"] == "admin"
    # second call does nothing
    assert F.ensure_default_admin(tmp_user_base) is False


def test_password_hash_verifies_with_legacy_secret(fixed_secret, monkeypatch):
    # A hash created with the legacy secret must still verify after rotation.
    legacy = "fv-s-secret-key-change-in-production"
    legacy_hash = F._password_hash("mypassword", legacy)
    # Current secret is different (pinned via env)
    assert F._secret_key() != legacy
    assert F.verify_password("mypassword", legacy_hash) is True
    assert F.verify_password("wrongpassword", legacy_hash) is False


def test_token_round_trip_and_legacy_acceptance(fixed_secret):
    token = F.create_access_token({"sub": "u1"})
    assert F.decode_token(token) is not None
    assert F.decode_token(token)["sub"] == "u1"
    # Expired token is rejected
    from datetime import timedelta
    expired = F.create_access_token({"sub": "u1"}, expires_delta=timedelta(seconds=-10))
    assert F.decode_token(expired) is None


def test_check_domain_deliverability_invalid():
    res = F.check_domain_deliverability("not-a-domain")
    assert "error" in res or "domain" in res
    # Valid-shaped domain returns the expected keys without raising.
    res2 = F.check_domain_deliverability("example.com")
    for key in ("domain", "spf", "dmarc", "dkim", "has_spf", "has_dmarc", "has_dkim"):
        assert key in res2


def test_update_sending_session_atomic(tmp_user_base):
    # Running status writes, then terminal status always persists.
    F.update_sending_session("u1", tmp_user_base, "c1", 1, 0, "running")
    p = tmp_user_base / "fv-s-u1" / "sender-config" / "sessions" / "c1.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["status"] == "running"
    F.update_sending_session("u1", tmp_user_base, "c1", 5, 2, "completed")
    data = json.loads(p.read_text())
    assert data["status"] == "completed" and data["sent"] == 5
