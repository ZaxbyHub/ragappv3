"""Issue #513 acceptance check C23 (AC23 / INGEST-017).

A pending file inserted (as a concurrent upload would) between the
collection phase and the deletion phase of delete-all-vault must end
coherently: its files row AND its on-disk upload file both survive, or both
disappear.

Discriminating: pre-fix, the atomic delete runs ``DELETE FROM files WHERE
vault_id = ?`` — a set wider than the collected/purged file ids — so the new
row vanishes while its upload file (never in stored_paths) survives as an
orphan -> FAIL. Post-fix the deletion set is coherent with the collection ->
PASS.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "backend"))


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    tmp_dir = tempfile.mkdtemp(prefix="c23_513_")
    try:
        os.environ["DATA_DIR"] = tmp_dir

        from app.api.routes import documents as documents_routes
        from app.config import settings
        from app.models.database import init_db, run_migrations

        settings.data_dir = Path(tmp_dir)
        db_path = str(Path(tmp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vaults (name, description, visibility, created_at, "
            "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
        )
        vault_id = conn.execute(
            "SELECT id FROM vaults WHERE name='V'"
        ).fetchone()[0]

        uploads_dir = settings.vault_uploads_dir(vault_id)
        seeded_ids = []
        for i in range(2):
            disk_path = uploads_dir / f"seeded_{i}.txt"
            disk_path.write_bytes(f"seeded content {i}".encode())
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, file_type, status, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, '.txt', 'indexed', '2026-01-01', "
                "'2026-01-01')",
                (vault_id, str(disk_path), f"seeded_{i}.txt", f"hash-seed-{i}", 16),
            )
            seeded_ids.append(cur.lastrowid)
        conn.commit()

        # The "concurrent upload": a NEW pending row + its on-disk file,
        # committed between the route's collection phase and its deletion
        # phase (injected via the per-file purge hook that runs in between).
        new_disk_path = uploads_dir / "raced_upload.txt"
        new_file_id = None

        real_purge = documents_routes._purge_file_derived_data  # noqa: SLF001

        async def _purge_with_injected_upload(conn_arg, vector_store, file_id, vid):
            result = await real_purge(conn_arg, vector_store, file_id, vid)
            nonlocal new_file_id
            if file_id == seeded_ids[-1] and new_file_id is None:
                other = sqlite3.connect(db_path)
                try:
                    other.execute("PRAGMA foreign_keys = ON")
                    new_disk_path.write_bytes(b"raced upload content")
                    cur = other.execute(
                        "INSERT INTO files (vault_id, file_path, file_name, "
                        "file_hash, file_size, file_type, status, created_at, "
                        "modified_at) VALUES (?, ?, ?, 'hash-raced', 20, "
                        "'.txt', 'pending', '2026-01-01', '2026-01-01')",
                        (vid, str(new_disk_path), "raced_upload.txt"),
                    )
                    other.commit()
                    new_file_id = cur.lastrowid
                finally:
                    other.close()
            return result

        class _FakeVSDB:
            async def table_names(self):
                return ["chunks"]

            async def open_table(self, _name):
                return object()

        class _FakeVectorStore:
            db = _FakeVSDB()

            def __init__(self):
                self.deleted_files = []

            async def delete_by_file(self, file_id_str):
                self.deleted_files.append(file_id_str)
                return 1

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(secret_manager=None))
        )

        documents_routes._purge_file_derived_data = _purge_with_injected_upload  # noqa: SLF001
        try:
            response = asyncio.run(
                documents_routes.delete_all_vault_documents(
                    vault_id=vault_id,
                    request=request,
                    conn=conn,
                    user={},
                    vector_store=_FakeVectorStore(),
                    _csrf_token=None,
                )
            )
        finally:
            documents_routes._purge_file_derived_data = real_purge  # noqa: SLF001

        if response.deleted_count < len(seeded_ids):
            print(
                "C23 CHECK: FAIL: delete-all-vault did not delete the "
                f"originally collected files (deleted_count={response.deleted_count})"
            )
            return 1
        if new_file_id is None:
            print(
                "C23 CHECK: FAIL: setup invalid - the raced pending upload was "
                "never inserted between collection and deletion"
            )
            return 1

        row = conn.execute(
            "SELECT id FROM files WHERE id = ?", (new_file_id,)
        ).fetchone()
        file_exists = new_disk_path.exists()
        row_exists = row is not None

        if row_exists != file_exists:
            detail = (
                "row deleted while its upload file survived as an orphan"
                if file_exists
                else "row survived while its upload file was deleted"
            )
            print(
                f"C23 CHECK: FAIL: raced pending upload is incoherent after "
                f"delete-all-vault: {detail} (row_exists={row_exists}, "
                f"file_exists={file_exists})"
            )
            return 1

        print("C23 CHECK: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C23 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c23_bulk_delete_race():
    assert main() == 0
