"""Issue #513 acceptance check C18 (AC18 / INGEST-021) — PRESERVING.

A committed generation's rows and asset bytes must survive a failed republish
of the same content AND the artifact sweeper; the first-publication-failure
control must still clean newly-created bytes.

Modeled on backend/tests/test_artifact_compensation.py (the merged fix for
INGEST-021), self-contained: (1) a successful asset publication through the
real ``_publish_artifacts`` path commits rows+bytes; (2) a failed republish
carrying identical asset bytes crashes mid-transaction after retiring the
committed generation — compensation must roll that back so the committed
rows+bytes survive the sweeper; (3) control: a first-publication failure on a
fresh file must have its newly-created bytes swept away.

This behavior is already fixed on master; this check guards it, so it must
PASS at the current HEAD.
"""

import hashlib
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

    tmp_dir = tempfile.mkdtemp(prefix="c18_513_")
    try:
        os.environ["DATA_DIR"] = tmp_dir

        from app.config import settings
        from app.models.database import init_db, run_migrations
        from app.services import artifact_store
        from app.services.document_artifacts import (
            AtomKind,
            DocumentAsset,
            DocumentAtom,
            ParsedDocument,
        )
        from app.services.document_processor import DocumentProcessor

        settings.data_dir = Path(tmp_dir)
        db_path = str(Path(tmp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (1, 1, '/tmp/x.png', 'x.png', 'h', 1, "
            "'indexed')"
        )
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (2, 1, '/tmp/y.png', 'y.png', 'h2', 1, "
            "'indexed')"
        )
        conn.commit()

        # Redirect the artifact root into the temp dir (save/restore).
        artifact_root = Path(tmp_dir) / "vault-artifacts"
        artifact_root.mkdir(parents=True, exist_ok=True)
        real_artifact_root = artifact_store.artifact_root
        artifact_store.artifact_root = lambda vault_id, settings_obj=None: artifact_root

        class _FakePool:
            def __init__(self, conn):
                self.conn = conn

            def get_connection(self):
                return self.conn

            def release_connection(self, _conn):
                pass

        def _asset(data, file_id, gen):
            asset_id = hashlib.sha256(data).hexdigest()
            return DocumentAsset(
                asset_id=asset_id,
                file_id=file_id,
                generation_hash=gen,
                sha256=asset_id,
                rel_path=artifact_store.compute_asset_rel_path(file_id, gen, asset_id),
                mime_type="image/png",
                byte_size=len(data),
            )

        def _parsed(asset, payloads, file_id, gen):
            return ParsedDocument(
                atoms=[
                    DocumentAtom(
                        atom_id=f"a-{file_id}-0",
                        schema_version=1,
                        file_id=file_id,
                        generation_hash=gen,
                        ordinal=0,
                        kind=AtomKind.IMAGE,
                        raw_text="img",
                        asset_id=asset.asset_id,
                    )
                ],
                assets=(asset,),
                parser_fingerprint="probe:1",
                asset_payloads=payloads,
            )

        def _publish(file_id, gen, parsed):
            proc_like = object.__new__(DocumentProcessor)
            proc_like.pool = _FakePool(conn)
            DocumentProcessor._publish_artifacts(
                proc_like,
                file_id=file_id,
                vault_id=1,
                generation_hash=gen,
                parsed=parsed,
            )

        try:
            # -- Step 1: successful asset publication (real code path). ------
            committed_bytes = b"committed-generation-bytes"
            committed_asset = _asset(committed_bytes, 1, "genCOMMITTED")
            _publish(
                1,
                "genCOMMITTED",
                _parsed(
                    committed_asset,
                    {committed_asset.asset_id: committed_bytes},
                    1,
                    "genCOMMITTED",
                ),
            )
            committed_file = artifact_root / committed_asset.rel_path
            if not committed_file.exists():
                print(
                    "C18 CHECK: FAIL: setup invalid - successful publication did "
                    "not write asset bytes"
                )
                return 1
            if conn.execute(
                "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
            ).fetchone()[0] != 1:
                print(
                    "C18 CHECK: FAIL: setup invalid - successful publication did "
                    "not commit asset rows"
                )
                return 1

            # -- Step 2: FAILED republish of identical content. --------------
            # Same asset bytes (same content-addressed asset id) under the
            # republish attempt; publish_generation retires the committed
            # generation mid-transaction (its normal `generation_hash <> ?`
            # retirement + tombstone) and then crashes before committing.
            republish_asset = _asset(committed_bytes, 1, "genRETRY")
            real_publish = artifact_store.publish_generation

            def _fail_mid_publish(conn_arg, **kwargs):
                real_publish(conn_arg, **kwargs)
                raise RuntimeError("mid-transaction republish failure")

            artifact_store.publish_generation = _fail_mid_publish
            try:
                _publish(
                    1,
                    "genRETRY",
                    _parsed(
                        republish_asset,
                        {republish_asset.asset_id: committed_bytes},
                        1,
                        "genRETRY",
                    ),
                )
            finally:
                artifact_store.publish_generation = real_publish

            # Committed rows survive the compensation rollback.
            committed_rows = conn.execute(
                "SELECT COUNT(*) FROM document_assets WHERE file_id=1 "
                "AND generation_hash='genCOMMITTED'"
            ).fetchone()[0]
            if committed_rows != 1:
                print(
                    "C18 CHECK: FAIL: committed generation rows were discarded "
                    f"by the failed republish (rows={committed_rows})"
                )
                return 1
            committed_tombstones = conn.execute(
                "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
                (committed_asset.rel_path,),
            ).fetchone()[0]
            if committed_tombstones != 0:
                print(
                    "C18 CHECK: FAIL: committed asset bytes were tombstoned by "
                    "the failed republish compensation "
                    f"(tombstones={committed_tombstones})"
                )
                return 1

            # Run the sweeper: committed rows + their bytes must survive.
            artifact_store.sweep_pending_asset_deletes(conn)
            if not committed_file.exists():
                print(
                    "C18 CHECK: FAIL: committed asset bytes were deleted by the "
                    "sweeper after the failed republish"
                )
                return 1
            if committed_file.read_bytes() != committed_bytes:
                print("C18 CHECK: FAIL: committed asset bytes were mutated")
                return 1
            if conn.execute(
                "SELECT COUNT(*) FROM document_assets WHERE file_id=1 "
                "AND generation_hash='genCOMMITTED'"
            ).fetchone()[0] != 1:
                print(
                    "C18 CHECK: FAIL: committed asset rows lost after the sweep"
                )
                return 1

            # -- Control: first-publication failure cleans new bytes. --------
            first_pub_bytes = b"first-publication-bytes"
            new_asset = _asset(first_pub_bytes, 2, "genFIRST")
            new_file = artifact_root / new_asset.rel_path

            def _fail_publish(_conn_arg, **_kw):
                raise RuntimeError("first publication failure")

            artifact_store.publish_generation = _fail_publish
            try:
                _publish(
                    2,
                    "genFIRST",
                    _parsed(
                        new_asset,
                        {new_asset.asset_id: first_pub_bytes},
                        2,
                        "genFIRST",
                    ),
                )
            finally:
                artifact_store.publish_generation = real_publish

            if not new_file.exists():
                print(
                    "C18 CHECK: FAIL: control setup invalid - first-publication "
                    "bytes were never materialized"
                )
                return 1
            tombstoned = conn.execute(
                "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
                (new_asset.rel_path,),
            ).fetchone()[0]
            artifact_store.sweep_pending_asset_deletes(conn)
            if tombstoned != 1 or new_file.exists():
                print(
                    "C18 CHECK: FAIL: first-publication failure did not clean "
                    f"newly-created bytes (tombstones={tombstoned}, "
                    f"bytes_exist={new_file.exists()})"
                )
                return 1
            remaining = conn.execute(
                "SELECT COUNT(*) FROM artifact_delete_pending"
            ).fetchone()[0]
            if remaining != 0:
                print(
                    "C18 CHECK: FAIL: sweeper left pending tombstones: "
                    f"{remaining}"
                )
                return 1

            print("C18 CHECK: PASS")
            return 0
        finally:
            artifact_store.artifact_root = real_artifact_root
            conn.close()
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C18 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c18_asset_bytes_sweeper():
    assert main() == 0
