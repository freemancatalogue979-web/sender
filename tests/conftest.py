"""Shared pytest fixtures for FV-S tests."""
import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so `import functions`, etc. resolve.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_user_base(tmp_path):
    """A temporary users/ base path with no user folders."""
    base = tmp_path / "users"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def fixed_secret(monkeypatch):
    """Pin the server secret so tests are deterministic and don't touch disk."""
    monkeypatch.setenv("FV_S_SECRET_KEY", "test-secret-1234567890")
    yield "test-secret-1234567890"
