"""Issue #698 feedback round (PRR-004) — the prune index actually exists.

`idx_embedding_cache_created_at` is what keeps `store()`'s entry-cap prune
off the full-scan path; a refactor of the connection-init block could
silently drop it. Pin its existence after first connect on a fresh cache DB.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

_B09_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b09_hermetic_env():
    """Hermetic env before any app import; restored on teardown."""
    saved = {key: os.environ.get(key) for key in _B09_ENV}
    os.environ.update(_B09_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_created_at_index_exists_after_first_connect(tmp_path, monkeypatch):
    from app.config import settings
    from app.services import embedding_cache as ec

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(ec, "_conn", None)

    conn = ec._get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name="
            "'embedding_cache'"
        ).fetchall()
        names = {row[0] for row in rows}
        assert "idx_embedding_cache_created_at" in names
        # The prune's ORDER BY leading column is covered by the index.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT cache_key FROM embedding_cache "
            "ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "idx_embedding_cache_created_at" in detail
    finally:
        # Drop the tmp connection so the monkeypatch restore below does not
        # leave the module global pointing at the tmp-path DB.
        ec._conn = None
