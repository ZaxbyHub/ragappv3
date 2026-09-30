"""Issue #692 acceptance checks AC4-AC5 (B03): upload-migration row hygiene.

Discriminating expectations at the unfixed HEAD (each check FAILS for the
defect reason, not for a harness error):

  AC4 - ``upload_path.migrate_uploads(False)`` moves the bytes on disk but
        never updates the ``files.file_path`` rows: the row still names the
        legacy ``data/uploads/<name>`` path, which the migration itself has
        renamed away (``<name>.migrated``) -> ``assert 0 == 1`` FAILS.
        Post-fix the row resolves to an existing path (either the row is
        rewritten to the per-vault path or the read path resolves the new
        location).
  AC5 - the same migrated legacy document must stay readable through
        ``documents.get_document_raw``: at HEAD the row's stale path no
        longer exists so the route 404s -> ``assert 404 == 200`` FAILS.

Harness mirrors ``tests/issue513_checks/test_c19_vault_delete_rollback.py``
(tmp DATA_DIR before app imports, init_db + run_migrations, real sqlite3
connection handed straight into the route function; the request stub exposes
``app.state`` because the success path touches it for ``_safe_record_action``).
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CSRF_TEST_POLICY = "naive"

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))


def _env_defaults() -> None:
    """Test env BEFORE any app import (mirrors test_c19 / conftest.py)."""
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")


def _seed_legacy_row(tmp_dir: str):
    """Init a tmp DATA_DIR DB with one vault + one legacy-layout file row.

    The bytes sit in the legacy flat ``data/uploads/legacy.txt`` location and
    the row's file_path names that legacy path (the pre-migration state).
    Returns (conn, vault_id, file_id, legacy_path).
    """
    os.environ["DATA_DIR"] = tmp_dir

    from app.config import settings
    from app.models.database import init_db, run_migrations

    settings.data_dir = Path(tmp_dir)
    db_path = str(settings.data_dir / "app.db")
    init_db(db_path)
    run_migrations(db_path)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO vaults (name, description, visibility, created_at, "
        "updated_at) VALUES ('B03V4', 'b03', 'private', '2026-01-01', "
        "'2026-01-01')"
    )
    vault_id = conn.execute(
        "SELECT id FROM vaults WHERE name = 'B03V4'"
    ).fetchone()[0]
    legacy_path = settings.uploads_dir / "legacy.txt"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("b03 legacy upload bytes", encoding="utf-8")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
        "file_size, status) VALUES (?, ?, ?, ?, ?, 'indexed')",
        (vault_id, str(legacy_path), "legacy.txt", "hashb03ac4", 22),
    )
    conn.commit()
    file_id = conn.execute(
        "SELECT id FROM files WHERE vault_id = ?", (vault_id,)
    ).fetchone()[0]
    return conn, vault_id, file_id, legacy_path


def _ac4_migrated_row_file_path_exists() -> int:
    """AC4: after migrate_uploads(False) the row's file_path must exist."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_ac4_")
    try:
        conn, _vault_id, file_id, _legacy_path = _seed_legacy_row(tmp_dir)

        from app.services.upload_path import migrate_uploads

        migrate_uploads(False)

        row_file_path = conn.execute(
            "SELECT file_path FROM files WHERE id = ?", (file_id,)
        ).fetchone()[0]

        present = int(Path(row_file_path).exists())
        assert present == 1
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _ac5_migrated_legacy_document_is_readable() -> int:
    """AC5: after migrate_uploads(False) the row must still be servable."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_ac5_")
    try:
        from fastapi import HTTPException

        from app.config import settings

        conn, _vault_id, file_id, _legacy_path = _seed_legacy_row(tmp_dir)

        from app.services.upload_path import migrate_uploads

        migrate_uploads(False)

        from app.api.routes.documents import get_document_raw

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

        async def _evaluate(user, resource_type, resource_id, action):  # noqa: ANN001
            return True

        status = None
        try:
            asyncio.run(
                get_document_raw(
                    file_id=file_id,
                    request=request,
                    conn=conn,
                    user={"id": 1, "role": "superadmin"},
                    evaluate=_evaluate,
                    settings_dep=settings,
                )
            )
            status = 200
        except HTTPException as exc:
            status = exc.status_code

        assert status == 200
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_migrated_row_file_path_exists():
    assert _ac4_migrated_row_file_path_exists() == 0


def test_migrated_legacy_document_is_readable():
    assert _ac5_migrated_legacy_document_is_readable() == 0


if __name__ == "__main__":
    raise SystemExit(
        _ac4_migrated_row_file_path_exists()
        or _ac5_migrated_legacy_document_is_readable()
    )
