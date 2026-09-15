"""Issue #558 acceptance check AC6 (finding C16) — empty REDIS_URL is quiet.

``CSRFManager(redis_url="", ...)`` — the documented no-Redis configuration
(REDIS_URL="" is what CI and backend/tests/conftest.py set) — must boot
quietly: no WARNING containing "Redis unavailable for CSRF" (a falsy URL is
"disabled", not an outage), the INFO line announcing the SQLite-backed
store is still logged, and the manager remains functionally correct.

DISCRIMINATING check: RED at base 2a7732a1. The base __init__
unconditionally calls ``redis.from_url("")``, whose scheme error is caught
by the broad ``except`` and logged as "Redis unavailable for CSRF: ..." at
WARNING on every boot before the SQLite fallback at INFO.
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.security import CSRFManager


def test_empty_redis_url_boots_quiet_with_working_sqlite_store(
    tmp_path, caplog
):
    """AC6: redis_url="" logs no Redis-outage WARNING, still logs the
    SQLite-backed-store INFO, and tokens round-trip (RED at base)."""
    caplog.set_level(logging.INFO, logger="security")

    manager = CSRFManager(
        redis_url="", ttl=900, db_path=str(tmp_path / "csrf-test.db")
    )

    messages = [r.getMessage() for r in caplog.records]
    outage_warnings = [m for m in messages if "Redis unavailable for CSRF" in m]
    assert not outage_warnings, outage_warnings
    assert any("using SQLite-backed store" in m for m in messages), messages

    # Functional: the SQLite-backed store still issues and validates tokens.
    token = manager.generate_token()
    assert manager.validate_token(token) is True
    assert manager.validate_token("nope") is False
