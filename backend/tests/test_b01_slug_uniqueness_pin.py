"""Regression pin (issue #690, T1-05-K-06): slug uniqueness parity.

Non-frozen companion to frozen check C7: C7 asserts fresh and migrated
schemas produce the SAME outcome for duplicate slugs, but a fix that simply
dropped uniqueness everywhere would also satisfy it. This pin asserts both
vintages actively REJECT duplicate non-NULL slugs, matching the fresh
schema's ``slug TEXT UNIQUE`` contract.
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.database import (  # noqa: E402
    init_db,
    migrate_add_org_slug_column,
)

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key-0123456789abcdef")
os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")


def _duplicate_slug_outcome(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO organizations (name, description, slug)"
            " VALUES ('One', '', 'dup-pin')"
        )
        conn.commit()
        conn.execute(
            "INSERT INTO organizations (name, description, slug)"
            " VALUES ('Two', '', 'dup-pin')"
        )
        conn.commit()
        return "accepted"
    except sqlite3.IntegrityError:
        return "IntegrityError"
    finally:
        conn.close()


def test_fresh_schema_rejects_duplicate_non_null_slug(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db = str(tmp_path / "fresh.db")
    init_db(db)
    assert _duplicate_slug_outcome(db) == "IntegrityError"


def test_migrated_schema_rejects_duplicate_non_null_slug(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT DEFAULT '',
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    conn.close()
    migrate_add_org_slug_column(db)
    assert _duplicate_slug_outcome(db) == "IntegrityError"
