"""Failure-compensation tests for ``document_processor._publish_artifacts`` (#460).

Regression for the final-critic findings:
- Asset bytes are materialized on disk only inside ``_publish_artifacts`` (just
  before the rows are published), never before, so a pre-publish failure or a
  missing vector-store cannot leave orphaned bytes.
- On a publish failure the compensation rolls back the partial transaction
  (discarding any partial rows AND any old-generation retirement it performed
  mid-way) and tombstones the materialized bytes for the sweep.
- A successful publish does NOT tombstone its own (now-live) assets.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services import artifact_store
from app.services.document_artifacts import (
    AtomKind,
    DocumentAsset,
    DocumentAtom,
    ParsedDocument,
)
from app.services.document_processor import DocumentProcessor


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def get_connection(self):
        return self.conn

    def release_connection(self, _conn):
        pass


def _new_db(tmp_path):
    sqlite_path = str(tmp_path / "db.sqlite")
    from app.models.database import init_db, run_migrations

    init_db(sqlite_path)
    run_migrations(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, '/tmp/x.png', 'x.png', 'h', 1, 'indexed')"
    )
    conn.commit()
    return conn


def _parsed(asset, payloads=None):
    return ParsedDocument(
        atoms=[
            DocumentAtom(
                atom_id="a-0",
                schema_version=1,
                file_id=1,
                generation_hash="genA",
                ordinal=0,
                kind=AtomKind.IMAGE,
                raw_text="img",
                asset_id=asset.asset_id,
            )
        ],
        assets=(asset,),
        parser_fingerprint="unstructured:test",
        asset_payloads=payloads or {},
    )


def _asset(data=b"abc"):
    import hashlib

    asset_id = hashlib.sha256(data).hexdigest()
    return DocumentAsset(
        asset_id=asset_id,
        file_id=1,
        generation_hash="genA",
        sha256=asset_id,
        rel_path=artifact_store.compute_asset_rel_path(1, "genA", asset_id),
        mime_type="image/png",
        byte_size=len(data),
    )


@pytest.fixture()
def artifact_root(tmp_path):
    root = tmp_path / "vault-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    real = artifact_store.artifact_root
    artifact_store.artifact_root = lambda vault_id, settings_obj=None: root
    yield root
    artifact_store.artifact_root = real


def test_failed_publish_rolls_back_and_tombstones_bytes(
    tmp_path, artifact_root, monkeypatch
):
    db = _new_db(tmp_path)
    asset = _asset()
    payloads = {asset.asset_id: b"abc"}

    def _fail_mid(conn, **_kw):
        # Simulate a mid-transaction failure: publish already inserted a partial
        # asset row (and may have retired old generations) before raising.
        conn.execute(
            "INSERT INTO document_assets "
            "(asset_id, file_id, generation_hash, sha256, rel_path, byte_size) "
            "VALUES ('partial', 1, 'genA', 'partial', '1/genA/partial', 1)"
        )
        raise RuntimeError("mid-transaction failure")

    monkeypatch.setattr(
        "app.services.document_processor.artifact_store.publish_generation", _fail_mid
    )

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    DocumentProcessor._publish_artifacts(
        proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=_parsed(asset, payloads)
    )

    # Rollback discarded the partial publish rows (both the partial insert AND
    # any real atom/asset insert that could have happened).
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM document_atoms WHERE file_id=1"
    ).fetchone()[0] == 0
    # The materialized bytes are tombstoned for the sweep (they have no row now).
    pending = db.execute(
        "SELECT rel_path, generation_hash FROM artifact_delete_pending "
        "WHERE rel_path=?",
        (asset.rel_path,),
    ).fetchone()
    assert pending is not None
    assert pending["generation_hash"] == "genA"
    db.close()


def test_successful_publish_writes_bytes_and_does_not_tombstone_live_assets(
    tmp_path, artifact_root
):
    db = _new_db(tmp_path)
    asset = _asset()
    payloads = {asset.asset_id: b"abc"}

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    # No monkeypatch: publish succeeds normally; the deferred bytes are
    # materialized on disk as part of the publish.
    DocumentProcessor._publish_artifacts(
        proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=_parsed(asset, payloads)
    )

    row = db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND rel_path=?",
        (asset.rel_path,),
    ).fetchone()[0]
    assert row == 1
    # Bytes exist on disk exactly at the planned/per-owner path.
    assert (artifact_root / asset.rel_path).exists()
    assert (artifact_root / asset.rel_path).read_bytes() == b"abc"
    # Successful publish must NOT enqueue a tombstone for its own live asset.
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
        (asset.rel_path,),
    ).fetchone()[0] == 0
    db.close()
