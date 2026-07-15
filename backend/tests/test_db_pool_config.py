"""Tests for the db_pool_max_size config and get_pool singleton sizing (issue #302).

Covers:
- ``Settings.db_pool_max_size`` default, env override, and validator (>=1).
- ``get_pool`` honors the requested ``max_size`` on the FIRST call for a path.
- ``get_pool`` returns the cached pool and WARNS (not raises, not resizes) when
  a later caller requests a LARGER size — making the silent-seeding class of
  bug observable instead of invisible.
"""
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pydantic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import (
    SQLiteConnectionPool,
    _pool_cache,
    _pool_cache_lock,
    get_pool,
)

# ---------------------------------------------------------------------------
# db_pool_max_size config
# ---------------------------------------------------------------------------


def test_db_pool_max_size_default_is_ten():
    """Default preserves the historical hardcoded 10 (lifespan.py used max_size=10)."""
    from app.config import Settings

    with mock.patch.dict(os.environ, {}, clear=False):
        s = Settings()
    assert s.db_pool_max_size == 10


def test_db_pool_max_size_env_override():
    """DB_POOL_MAX_SIZE env var overrides the default."""
    from app.config import Settings

    with mock.patch.dict(os.environ, {"DB_POOL_MAX_SIZE": "7"}):
        s = Settings()
    assert s.db_pool_max_size == 7


@pytest.mark.parametrize("bad", ["0", "-1", "-5"])
def test_db_pool_max_size_rejects_below_one(bad):
    """Validator rejects 0 and negative values (would exhaust on every checkout)."""
    from app.config import Settings

    with mock.patch.dict(os.environ, {"DB_POOL_MAX_SIZE": bad}):
        with pytest.raises(pydantic.ValidationError):
            Settings()


def test_db_pool_max_size_accepts_one():
    """1 is the smallest valid value."""
    from app.config import Settings

    with mock.patch.dict(os.environ, {"DB_POOL_MAX_SIZE": "1"}):
        s = Settings()
    assert s.db_pool_max_size == 1


# ---------------------------------------------------------------------------
# get_pool singleton sizing + upward-drift warning
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_pool_cache():
    """Clear the module-level _pool_cache (and close pools) around each test.

    The autouse _reset_db_pool conftest fixture already does this per-test, but
    these tests assert on cached sizes directly and must be robust to ordering,
    so they own their own teardown as well (critic item 7).
    """
    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            try:
                p.close_all()
            except Exception:
                pass
        _pool_cache.clear()
    yield
    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            try:
                p.close_all()
            except Exception:
                pass
        _pool_cache.clear()


def _tmp_db_path():
    tmp = tempfile.mkdtemp(prefix="dbpoolcfg-")
    return str(Path(tmp) / "t.db"), tmp


def test_get_pool_honors_max_size_on_first_call(fresh_pool_cache):
    """First caller for a path seeds the pool at its requested max_size."""
    path, _tmp = _tmp_db_path()
    try:
        pool = get_pool(path, max_size=8)
        assert isinstance(pool, SQLiteConnectionPool)
        assert pool.max_size == 8
    finally:
        with _pool_cache_lock:
            for p in list(_pool_cache.values()):
                try:
                    p.close_all()
                except Exception:
                    pass
            _pool_cache.clear()


def test_get_pool_ignores_max_size_on_cache_hit(fresh_pool_cache):
    """Singleton returns the cached pool regardless of a later requested size."""
    path, _tmp = _tmp_db_path()
    try:
        first = get_pool(path, max_size=8)
        second = get_pool(path, max_size=3)
        assert second is first
        assert second.max_size == 8  # unchanged — cache hit ignores max_size
    finally:
        with _pool_cache_lock:
            for p in list(_pool_cache.values()):
                try:
                    p.close_all()
                except Exception:
                    pass
            _pool_cache.clear()


def test_get_pool_warns_on_upward_size_drift(fresh_pool_cache, caplog):
    """A LARGER requested size on a cached path logs a warning and keeps the pool.

    This is the fix for the silent-seeding bug (issue #302): previously the
    mismatch was completely invisible. Now it is observable so operators can
    diagnose a pool seeded smaller than intended.
    """
    path, _tmp = _tmp_db_path()
    try:
        seeded = get_pool(path, max_size=5)
        with caplog.at_level(logging.WARNING, logger="app.models.database"):
            returned = get_pool(path, max_size=20)
        assert returned is seeded
        assert returned.max_size == 5  # NOT resized
        # Exactly one warning was emitted, naming the path and both sizes.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert "5" in msg and "20" in msg and path in msg
    finally:
        with _pool_cache_lock:
            for p in list(_pool_cache.values()):
                try:
                    p.close_all()
                except Exception:
                    pass
            _pool_cache.clear()


def test_get_pool_no_warning_on_smaller_request(fresh_pool_cache, caplog):
    """A SMALLER requested size does NOT warn (document_processor/file_watcher
    legitimately request max_size=1/2 after lifespan seeds at 10)."""
    path, _tmp = _tmp_db_path()
    try:
        get_pool(path, max_size=10)
        with caplog.at_level(logging.WARNING, logger="app.models.database"):
            get_pool(path, max_size=2)
        drift_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "singleton cannot resize" in r.getMessage()
        ]
        assert drift_warnings == []
    finally:
        with _pool_cache_lock:
            for p in list(_pool_cache.values()):
                try:
                    p.close_all()
                except Exception:
                    pass
            _pool_cache.clear()
