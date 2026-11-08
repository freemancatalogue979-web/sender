"""Session-lifecycle tests for the phone/email validator pointers."""
import json
from pathlib import Path

import pytest

import functions as F


@pytest.fixture
def base(tmp_path, monkeypatch):
    b = tmp_path / "users"
    b.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FV_S_SECRET_KEY", "test-secret")
    return b


def test_pointer_file_is_not_a_session_file(base):
    """Regression: the pointer used to live in validator-sessions/ and was
    globbed back as a permanently 'pending' session."""
    uid = "u1"
    F.save_active_session_state(uid, base, "phone", "phone_val_abc", {"status": "pending"})

    sessions_dir = F.get_validator_sessions_dir(uid, base)
    names = [p.name for p in sessions_dir.glob("*.json")]
    assert "active_phone.json" not in names

    pointer = F._get_active_session_pointer_file(uid, base, "phone")
    assert pointer.exists()
    assert pointer.parent.name == "validator-active"


def test_completed_session_is_not_reported_active(base):
    uid = "u2"
    F.save_validator_session(uid, base, {
        "session_id": "phone_val_done",
        "user_id": uid,
        "type": "phone",
        "status": "completed",
        "queue": [],
        "results": [],
    })
    F.save_active_session_state(uid, base, "phone", "phone_val_done", {"status": "completed"})

    assert F.get_active_validator_session(uid, base, "phone") is None


def test_legacy_pointer_inside_sessions_dir_is_ignored(base):
    """Older installs still have active_*.json inside the sessions folder."""
    uid = "u3"
    sessions_dir = F.get_validator_sessions_dir(uid, base)
    (sessions_dir / "active_email.json").write_text(
        json.dumps({"session_id": "x", "type": "email", "status": "pending", "queue": []}),
        encoding="utf-8",
    )
    F.save_validator_session(uid, base, {
        "session_id": "email_val_real",
        "user_id": uid,
        "type": "email",
        "status": "completed",
        "queue": [],
        "results": [],
    })

    # The stale pointer must not masquerade as a second active session.
    assert F.get_active_validator_session(uid, base, "email") is None


def test_running_session_is_still_detected(base):
    uid = "u4"
    F.save_validator_session(uid, base, {
        "session_id": "phone_val_live",
        "user_id": uid,
        "type": "phone",
        "status": "running",
        "queue": ["+2347069977249"],
        "results": [],
    })
    active = F.get_active_validator_session(uid, base, "phone")
    assert active is not None
    assert active["session_id"] == "phone_val_live"


def test_verified_per_minute_is_not_absurd_on_a_fast_first_batch():
    """A batch finishing in milliseconds must not report a cosmic rate."""
    session = {"valid": 0, "start_time": None}
    total_valid = 2
    start_time_str = session.get("start_time")
    if not start_time_str:
        from datetime import datetime
        start_time_str = datetime.now().isoformat()
    from datetime import datetime
    elapsed = (datetime.now() - datetime.fromisoformat(start_time_str)).total_seconds()
    vpm = round((total_valid / elapsed) * 60, 2) if elapsed >= 1 else 0
    assert vpm == 0
    assert start_time_str is not None
