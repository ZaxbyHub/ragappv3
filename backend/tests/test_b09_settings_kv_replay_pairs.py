"""Issue #698 feedback round — settings_kv replay of jointly-valid pairs.

Per-key replay validated each persisted key against the NOT-yet-updated
singleton, so a jointly-valid pair persisted by one PUT (e.g.
chunk_size_chars=150 + chunk_overlap_chars=100 over boot defaults
{2000, 200}) was silently split: the size key failed the new cross-field
overlap<size validator against the stale overlap and was warn-skipped,
reverting the operator's saved size on every restart. The replay now
batch-validates the merged candidate state first and only falls back to
per-key replay when the set as a whole is rejected. The same fix covers
the pre-existing jobs-lease instance (heartbeat/reclaim coherence,
issue #559) that had the same per-key order dependence at base.
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


@pytest.fixture
def replay_db(tmp_path, monkeypatch):
    """A tmp settings_kv DB plus a deterministic singleton pair baseline."""
    from app.config import settings

    db_path = tmp_path / "settings_kv.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    # Deterministic boot state for the pair under test (defaults).
    monkeypatch.setattr(settings, "chunk_size_chars", 2000)
    monkeypatch.setattr(settings, "chunk_overlap_chars", 200)
    return db_path


def _seed(db_path, rows: dict):
    conn = sqlite3.connect(str(db_path))
    for key, value in rows.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    conn.commit()
    conn.close()


def test_size_lowering_pair_restores_both(replay_db, monkeypatch):
    """A PUT-legal size-lowering pair replays as a unit, not half-reverted."""
    from app.config import settings
    from app.lifespan import _load_persisted_settings

    _seed(
        replay_db,
        {"chunk_size_chars": 150, "chunk_overlap_chars": 100},
    )
    _load_persisted_settings(str(replay_db))
    assert settings.chunk_size_chars == 150
    assert settings.chunk_overlap_chars == 100


def test_jobs_lease_pair_restores_both(replay_db, monkeypatch):
    """The pre-existing jobs-lease instance of the class (issue #559 pair).

    Per-key replay of {heartbeat:100, reclaim:600} over boot defaults
    {30, 300} rejected the heartbeat first (300 <= 4x100 is false, but the
    validator ran against the stale reclaim 300: 300 <= 400) and silently
    kept heartbeat=30. The merged pair is jointly valid (600 > 400) and now
    replays as a unit.
    """
    from app.config import settings
    from app.lifespan import _load_persisted_settings

    monkeypatch.setattr(settings, "jobs_heartbeat_interval_seconds", 30.0)
    monkeypatch.setattr(settings, "jobs_lease_reclaim_timeout_seconds", 300.0)
    _seed(
        replay_db,
        {
            "jobs_heartbeat_interval_seconds": 100,
            "jobs_lease_reclaim_timeout_seconds": 600,
        },
    )
    _load_persisted_settings(str(replay_db))
    assert settings.jobs_heartbeat_interval_seconds == 100.0
    assert settings.jobs_lease_reclaim_timeout_seconds == 600.0


def test_individually_invalid_value_still_skipped_with_warning(
    replay_db, monkeypatch, caplog
):
    """Batch rejection falls back to per-key warn-and-skip (no behavior loss).

    chunk_size_chars=9999999 is individually invalid (above the embedder
    cap); paired with a valid retrieval_top_k the batch fails, the fallback
    skips the bad key, and the good key still applies.
    """
    import logging

    from app.config import settings
    from app.lifespan import _load_persisted_settings

    monkeypatch.setattr(settings, "retrieval_top_k", 5)
    _seed(
        replay_db,
        {"chunk_size_chars": 9_999_999, "retrieval_top_k": 7},
    )
    with caplog.at_level(logging.WARNING, logger="app.lifespan"):
        _load_persisted_settings(str(replay_db))
    assert settings.chunk_size_chars == 2000  # bad value skipped
    assert settings.retrieval_top_k == 7  # good value still applied
    assert any("falling back to per-key replay" in r.message for r in caplog.records)


def test_single_valid_pair_batch_path_has_no_fallback_warning(replay_db, caplog):
    """The happy pair path applies via the batch without the fallback log."""
    import logging

    from app.lifespan import _load_persisted_settings

    _seed(
        replay_db,
        {"chunk_size_chars": 150, "chunk_overlap_chars": 100},
    )
    with caplog.at_level(logging.WARNING, logger="app.lifespan"):
        _load_persisted_settings(str(replay_db))
    assert not any(
        "falling back to per-key replay" in r.message for r in caplog.records
    )
