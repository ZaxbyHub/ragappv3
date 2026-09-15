"""Issue #558 acceptance check AC7 (finding C16) — genuine outage still warns.

``CSRFManager`` with a NON-EMPTY but unreachable Redis URL (127.0.0.1 port 1
— nothing listens there) must still log the "Redis unavailable for CSRF"
WARNING (a real outage is exactly what WARNING is for) and still fall back
to a working store: token generate/validate round-trips True.

PRESERVING check: GREEN at base 2a7732a1 and must stay green after any
correct fix (the quiet-mode change must be scoped to the empty URL only).
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.security import CSRFManager


def test_unreachable_redis_url_still_warns_and_falls_back(caplog):
    """AC7: a real Redis outage still logs the WARNING and the in-memory
    fallback store still works (GREEN at base and after any correct fix)."""
    caplog.set_level(logging.WARNING, logger="security")

    manager = CSRFManager(
        redis_url="redis://127.0.0.1:1/0", ttl=900, db_path=""
    )

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "Redis unavailable for CSRF" in m
        for m in messages
    ), messages

    # Functional: the fallback store still issues and validates tokens.
    token = manager.generate_token()
    assert manager.validate_token(token) is True
