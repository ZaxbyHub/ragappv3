"""Issue #513 acceptance check C14 (AC14 / INGEST-016).

Maintenance mode enabled at enqueue time during an admitted upload must leave
NO pending files row pointing at a missing file (either row+file are both
retained for retry, or both are removed) and must return an operational
status; after maintenance ends, re-upload of the same content must not be
duplicate-conflict blocked by an orphaned row.

Discriminating: pre-fix, ``_do_upload`` commits the 'pending' row, then
``background_processor.enqueue`` raises DocumentProcessingError (maintenance),
the generic handler unlinks the uploaded file, and the orphaned pending row
remains - ``_check_duplicate_in_flight`` then 409s every re-upload of the
same content -> FAIL. Post-fix the row/file pair is coherent -> PASS.
"""

import asyncio
import os
import shutil
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

    tmp_dir = tempfile.mkdtemp(prefix="c14_513_")
    try:
        os.environ["DATA_DIR"] = tmp_dir

        from app.config import settings
        from app.models.database import SQLiteConnectionPool, init_db, run_migrations

        settings.data_dir = Path(tmp_dir)
        db_path = str(Path(tmp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        pool = SQLiteConnectionPool(db_path, max_size=5)

        conn = pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at, "
                "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
            )
            vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='V'"
            ).fetchone()[0]
            conn.commit()
        finally:
            pool.release_connection(conn)

        from app.api.routes.documents import _do_upload
        from app.services.background_tasks import BackgroundProcessor
        from app.services.maintenance import MaintenanceService

        maintenance = MaintenanceService(pool)
        bp = BackgroundProcessor(pool=pool, maintenance_service=maintenance)

        content = b"c14 maintenance-mode upload probe contents"

        class _FakeUploadFile:
            def __init__(self, filename, data):
                self.filename = filename
                self._data = data
                self._pos = 0

            async def read(self, n=-1):
                if n is None or n < 0:
                    chunk = self._data[self._pos:]
                else:
                    chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

        class _FakeVectorStore:
            db = None

        class _FakeEmbedding:
            async def embed_batch(self, texts, fail_fast=True):
                return []

        async def do_upload():
            request = SimpleNamespace(headers={})
            return await _do_upload(
                request,
                _FakeUploadFile("c14_probe.txt", content),
                settings,
                _FakeVectorStore(),
                _FakeEmbedding(),
                pool,
                bp,
                vault_id,
            )

        # -- Phase 1: upload while maintenance mode is enabled. --------------
        maintenance.set_flag(True, "c14 probe")
        try:
            from fastapi import HTTPException

            phase1_error = None
            try:
                asyncio.run(do_upload())
            except HTTPException as exc:
                phase1_error = exc
            except Exception as exc:  # noqa: BLE001
                print(
                    "C14 CHECK: FAIL: upload raised a non-operational error: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 1

            if phase1_error is None:
                print(
                    "C14 CHECK: FAIL: upload under maintenance mode unexpectedly "
                    "succeeded at enqueue time (enqueue failure not simulated)"
                )
                return 1
            if not (400 <= phase1_error.status_code < 600):
                print(
                    "C14 CHECK: FAIL: upload returned a non-operational status "
                    f"{phase1_error.status_code}"
                )
                return 1
        finally:
            maintenance.set_flag(False, "")

        # -- Coherence: no pending row may point at a missing file. ----------
        import hashlib

        file_hash = hashlib.sha256(content).hexdigest()

        conn = pool.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, file_path, status FROM files WHERE file_hash = ? "
                "AND vault_id = ? AND status IN ('pending', 'processing')",
                (file_hash, vault_id),
            ).fetchall()
        finally:
            pool.release_connection(conn)

        orphans = [r["file_path"] for r in rows if not Path(r["file_path"]).exists()]
        if orphans:
            print(
                "C14 CHECK: FAIL: pending rows point at missing uploaded file(s) "
                f"after maintenance-mode enqueue failure: {[str(o) for o in orphans]}; "
                "re-upload of the same content will be 409-blocked by the "
                "orphaned row"
            )
            return 1

        coherent_pair_exists = len(rows) > 0

        # -- Phase 2: maintenance over; re-upload must not 409 on an orphan. -
        from fastapi import HTTPException

        reupload_error = None
        try:
            asyncio.run(do_upload())
        except HTTPException as exc:
            reupload_error = exc
        except Exception as exc:  # noqa: BLE001
            print(
                "C14 CHECK: FAIL: re-upload after maintenance raised a "
                f"non-operational error: {type(exc).__name__}: {exc}"
            )
            return 1

        if reupload_error is not None and reupload_error.status_code == 409:
            if not coherent_pair_exists:
                print(
                    "C14 CHECK: FAIL: re-upload after maintenance was "
                    "duplicate-conflict blocked (409) with no coherent "
                    "retained row/file pair"
                )
                return 1
            # A retained coherent pair legitimately blocks a duplicate upload;
            # the orphan check above already proved coherence.
        elif reupload_error is not None:
            print(
                "C14 CHECK: FAIL: re-upload after maintenance failed with "
                f"status {reupload_error.status_code}: {reupload_error.detail}"
            )
            return 1

        print("C14 CHECK: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C14 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c14_maintenance_upload_orphan():
    assert main() == 0
