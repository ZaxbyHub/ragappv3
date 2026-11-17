"""Acceptance check C6 (issue #982 Family D): limiter construction must
never surface the credential-bearing REDIS_URL.

``backend/app/limiter.py`` hands ``settings.redis_url`` verbatim to slowpa
c``WhitelistLimiter``; for an unregistered storage scheme (or an unparsable
sentinel/cluster URI) ``limits.storage.storage_from_string`` raises
``ConfigurationError: unknown storage scheme : <full credential-bearing
URI>`` at import time — the repo's own INFO line is already redacted
(``redact_url``), the leak channel is the third-party exception message
(live-probed: 02-reproduction.md Attempt 1).

The test drives ``build_limiter`` DIRECTLY with an injected URL (the
limiter module builds its module-level instance at import from
``settings.redis_url``; re-importing ``app.main``/``app.limiter`` here
would not exercise the failure path and could not be un-imported).

- Bogus scheme with a marker password: the boot failure (raised or logged)
  must never carry the marker. RED at base, GREEN after the fix.
- Valid ``redis://`` and the empty-config ``memory://`` fallback keep
  constructing a limiter (positive pairing, GREEN before and after).
"""

import logging
import os
import sys
from contextlib import contextmanager

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.limiter import WhitelistLimiter, build_limiter

LIMITER_LOGGER = "app.limiter"
MARKER = "s3cret982c6marker"


class _ListHandler(logging.Handler):
    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


def test_bogus_scheme_failure_never_surfaces_credential_uri():
    """AC6 (C6): an unregistered-scheme REDIS_URL fails boot without ever
    surfacing the credential-bearing URI."""
    bogus_uri = f"bogus://:{MARKER}@redis-host.invalid:6379/0"
    with capture_records(LIMITER_LOGGER, logging.INFO) as captured:
        with pytest.raises(Exception) as excinfo:  # noqa: BLE001
            build_limiter(bogus_uri)

    message = str(excinfo.value)
    assert MARKER not in message, (
        "982C6: the limiter boot failure surfaces the credential-bearing "
        f"REDIS_URL verbatim (third-party ConfigurationError message): "
        f"{message!r}"
    )
    leaks = [r.getMessage() for r in captured.records if MARKER in r.getMessage()]
    assert leaks == [], (
        f"982C6: {len(leaks)} INFO+ record(s) of {LIMITER_LOGGER} carry the "
        f"credential-bearing URI: {leaks}"
    )


def test_valid_and_memory_uris_still_construct():
    """Positive pairing: a registered scheme and the no-Redis fallback keep
    constructing the limiter (the redaction must not break boot wiring)."""
    assert isinstance(build_limiter("redis://localhost:6379/0"), WhitelistLimiter)
    assert isinstance(build_limiter(""), WhitelistLimiter)
