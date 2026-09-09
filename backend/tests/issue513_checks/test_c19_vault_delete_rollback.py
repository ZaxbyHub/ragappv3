"""Issue #513 acceptance check C19 (AC19 / INGEST-022).

An injected pre-commit SQL failure during vault deletion must leave both the
relational rows and the vector store usable (vectors must not be destroyed
before the SQL commit, or a durable retry tombstone must exist); a subsequent
successful deletion must eventually remove both.

Discriminating: pre-fix, ``vaults.delete_vault`` runs
``vector_store.delete_by_vault`` BEFORE/amid the SQL transaction with no
tombstone, so a SQL failure before commit rolls back the rows while the
vectors are already gone -> FAIL. Post-fix the vector deletion is reconciled
after the commit (or tombstoned for retry) -> PASS.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "backend"))


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    tmp_dir = tempfile.mkdtemp(prefix="c19_513_")
    try:
        os.environ["DATA_DIR"] = tmp_dir

        from app.config import settings
        from app.models.database import init_db, run_migrations

        settings.data_dir = Path(tmp_dir)
        db_path = str(Path(tmp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO vaults (name, description, visibility, created_at, "
            "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
        )
        vault_id = conn.execute(
            "SELECT id FROM vaults WHERE name='V'"
        ).fetchone()[0]
        for i in range(2):
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (?, ?, ?, ?, 1, 'indexed')",
                (vault_id, f"/tmp/f{i}.txt", f"f{i}.txt", f"hash{i}",),
            )
        conn.commit()
        conn.close()

        class _FailingVaultDeleteConn(sqlite3.Connection):
            """Real connection that raises once when the vault DELETE runs
            (pre-commit SQL failure), delegating everything else."""

            marker = "DELETE FROM vaults"

            def execute(self, sql, params=()):
                cls = type(self)
                if cls.marker is not None and cls.marker in sql:
                    cls.marker = None
                    raise sqlite3.OperationalError(
                        "injected pre-commit SQL failure"
                    )
                return super().execute(sql, params)

        failing_conn = sqlite3.connect(
            db_path, factory=_FailingVaultDeleteConn, check_same_thread=False
        )
        failing_conn.row_factory = sqlite3.Row
        failing_conn.execute("PRAGMA foreign_keys = ON")

        class _FakeVectorStore:
            def __init__(self):
                self.deleted_vaults = []
                self.deleted_files = []
                self.db = None

            async def delete_by_vault(self, vault_id_str):
                self.deleted_vaults.append(vault_id_str)
                return 2

            async def delete_by_file(self, file_id_str):
                self.deleted_files.append(file_id_str)
                return 1

        from fastapi import HTTPException

        from app.api.routes.vaults import delete_vault

        vs = _FakeVectorStore()

        # -- Phase 1: SQL failure before commit. -----------------------------
        phase1_error = None
        try:
            asyncio.run(
                delete_vault(
                    vault_id=vault_id,
                    conn=failing_conn,
                    vector_store=vs,
                    user={},
                    _csrf_token=None,
                )
            )
        except HTTPException as exc:
            phase1_error = exc
        except Exception as exc:  # noqa: BLE001
            print(
                "C19 CHECK: FAIL: vault deletion raised a non-operational error: "
                f"{type(exc).__name__}: {exc}"
            )
            return 1
        finally:
            try:
                failing_conn.rollback()
            except sqlite3.Error:
                pass
            failing_conn.close()

        if phase1_error is None:
            print(
                "C19 CHECK: FAIL: injected pre-commit SQL failure did not fail "
                "the vault deletion"
            )
            return 1

        # AMEND (CHECK_WRONG): this phase-2 verification connection is used
        # from this thread while the route's asyncio.to_thread workers may
        # still hold the writer; without check_same_thread=False the raw
        # connect raises ProgrammingError on any compliant implementation,
        # which no implementation can satisfy. Assertions unchanged.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            vault_rows = conn.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (vault_id,)
            ).fetchone()[0]
            file_rows = conn.execute(
                "SELECT COUNT(*) FROM files WHERE vault_id = ?", (vault_id,)
            ).fetchone()[0]
            pending_vector_deletes = conn.execute(
                "SELECT COUNT(*) FROM vector_delete_pending"
            ).fetchone()[0]

            if vault_rows != 1 or file_rows != 2:
                print(
                    "C19 CHECK: FAIL: relational evidence not usable after "
                    f"rollback (vault_rows={vault_rows}, file_rows={file_rows})"
                )
                return 1

            vectors_gone = str(vault_id) in vs.deleted_vaults
            if vectors_gone and pending_vector_deletes == 0:
                print(
                    "C19 CHECK: FAIL: vector chunks for the vault were deleted "
                    "before the SQL commit and the rollback could not restore "
                    "them (no vector_delete_pending tombstone either); both "
                    "stores are not usable after the failed deletion"
                )
                return 1

            # -- Phase 2: successful commit -> vectors eventually removed. ---
            phase2_error = None
            try:
                asyncio.run(
                    delete_vault(
                        vault_id=vault_id,
                        conn=conn,
                        vector_store=vs,
                        user={},
                        _csrf_token=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                phase2_error = exc

            if phase2_error is not None:
                print(
                    "C19 CHECK: FAIL: vault deletion failed after the injected "
                    f"failure was removed: {type(phase2_error).__name__}: "
                    f"{phase2_error}"
                )
                return 1

            vault_gone = conn.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (vault_id,)
            ).fetchone()[0] == 0
            files_gone = conn.execute(
                "SELECT COUNT(*) FROM files WHERE vault_id = ?", (vault_id,)
            ).fetchone()[0] == 0
            pending_after = conn.execute(
                "SELECT COUNT(*) FROM vector_delete_pending"
            ).fetchone()[0]

            if not vault_gone or not files_gone:
                print(
                    "C19 CHECK: FAIL: successful deletion did not remove the "
                    f"relational rows (vault_gone={vault_gone}, "
                    f"files_gone={files_gone})"
                )
                return 1
            vectors_removed_or_tombstoned = (
                str(vault_id) in vs.deleted_vaults or pending_after > 0
            )
            if not vectors_removed_or_tombstoned:
                print(
                    "C19 CHECK: FAIL: successful commit did not remove or "
                    "tombstone the vault's vectors for retry"
                )
                return 1
        finally:
            conn.close()

        print("C19 CHECK: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C19 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c19_vault_delete_rollback():
    assert main() == 0
