"""Issue #697 acceptance checks (Workstream B PR 8) — near-dup identity.

AC3 (test_legacy_256_dim_row_joins_group): a legacy 256-dim
``document_near_dups`` row (group_id NULL, centroid blob holding a REAL
``_text_fingerprint(text).tobytes()``) must still join the advisory group
of a newly recorded 1024-dim centroid when both files share identical
parsed_text. A fingerprint-comparable path must reach the legacy row;
``grouped = int(ga is not None and ga == gb)`` must be 1.

AC4 (test_cross_model_centroids_not_grouped): centroids recorded under
DIFFERENT embedding models (settings.embedding_model patched around each
``record_file_centroid`` call) must NOT be grouped even when their
vectors are identical — model identity must scope the comparison.
``grouped = int(gp is not None and gp == gq)`` must be 0.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

_B08_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b08_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards.

    Mirrors backend/tests/conftest.py and the C9 harness's ``_hermetic_env``
    pattern: every app import in this module is function-local, so this
    module-scoped fixture has run (and the env keys are set) before the
    first ``app.*`` module is imported. All previous values are restored on
    teardown so later modules see the conftest-provided environment.
    """
    saved = {key: os.environ.get(key) for key in _B08_ENV}
    os.environ.update(_B08_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _seed_vault(db_path: str) -> int:
    """Insert one vault row on a run_migrations DB; return its id."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        return int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    finally:
        conn.commit()
        conn.close()


def test_legacy_256_dim_row_joins_group() -> None:
    """AC3 (issue #697): the legacy 256-dim row joins B's group."""
    from app.models.database import run_migrations
    from app.services import near_duplicates
    from app.services.near_duplicates import _text_fingerprint

    tmp = Path(tempfile.mkdtemp(prefix="b08_ac3_"))
    db_path = str(tmp / "app.db")
    run_migrations(db_path)
    vid = _seed_vault(db_path)
    conn = sqlite3.connect(db_path)
    try:
        text = "identical document body text for the legacy probe"
        conn.execute(
            "INSERT INTO files "
            "(vault_id, file_path, file_name, file_hash, file_size, status, parsed_text) "
            "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
            (vid, "/b08ac3/a.txt", "a.txt", "hash-a", 10, text),
        )
        conn.execute(
            "INSERT INTO files "
            "(vault_id, file_path, file_name, file_hash, file_size, status, parsed_text) "
            "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
            (vid, "/b08ac3/b.txt", "b.txt", "hash-b", 10, text),
        )
        conn.commit()
        ids = {r[1]: r[0] for r in conn.execute("SELECT id, file_name FROM files")}
        a = ids["a.txt"]
        b = ids["b.txt"]
        fp = _text_fingerprint(text)
        conn.execute(
            "INSERT INTO document_near_dups "
            "(vault_id, file_id, centroid, dim, group_id, similarity) "
            "VALUES (?, ?, ?, 256, NULL, NULL)",
            (vid, a, fp.tobytes()),
        )
        conn.commit()

        near_duplicates.record_file_centroid(
            conn, vid, b, [np.ones(1024).tolist()], document_text=text
        )
        ga = near_duplicates.get_near_duplicate_group(conn, a)
        gb = near_duplicates.get_near_duplicate_group(conn, b)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    # Integer capture keeps the assert clear of ruff E712.
    grouped = int(ga is not None and ga == gb)
    assert grouped == 1


def test_cross_model_centroids_not_grouped() -> None:
    """AC4 (issue #697): identical vectors under different models stay apart."""
    from app.config import settings
    from app.models.database import run_migrations
    from app.services import near_duplicates

    tmp = Path(tempfile.mkdtemp(prefix="b08_ac4_"))
    db_path = str(tmp / "app.db")
    run_migrations(db_path)
    vid = _seed_vault(db_path)
    conn = sqlite3.connect(db_path)
    try:
        for name in ("p.txt", "q.txt"):
            conn.execute(
                "INSERT INTO files "
                "(vault_id, file_path, file_name, file_hash, file_size, status) "
                "VALUES (?, ?, ?, ?, ?, 'indexed')",
                (vid, f"/b08ac4/{name}", name, f"hash-{name}", 10),
            )
        conn.commit()
        ids = {r[1]: r[0] for r in conn.execute("SELECT id, file_name FROM files")}
        p = ids["p.txt"]
        q = ids["q.txt"]
        vec = [[0.25 * (i + 1) for i in range(8)]]

        with patch.object(settings, "embedding_model", "model-one"):
            near_duplicates.record_file_centroid(conn, vid, p, vec)
        with patch.object(settings, "embedding_model", "model-two"):
            near_duplicates.record_file_centroid(conn, vid, q, vec)
        gp = near_duplicates.get_near_duplicate_group(conn, p)
        gq = near_duplicates.get_near_duplicate_group(conn, q)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    # Integer capture keeps the assert clear of ruff E712.
    grouped = int(gp is not None and gp == gq)
    assert grouped == 0
