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


def test_failed_publish_rolls_back_discards_old_generation_retirement(
    tmp_path, artifact_root, monkeypatch
):
    """PRR-005 regression: a mid-publish failure must NOT discard a prior
    generation's live rows / bytes.

    We seed a real old generation (rows + on-disk bytes), then simulate
    ``publish_generation`` retiring that generation AND inserting a partial new
    row before raising. The compensation rollback must restore the old
    generation and drop both the partial row and the mid-way tombstone.
    """
    db = _new_db(tmp_path)
    old_asset = _asset(b"oldbytes")
    old_rel = artifact_store.compute_asset_rel_path(1, "genOLD", old_asset.asset_id)
    db.execute(
        "INSERT INTO document_assets "
        "(asset_id, file_id, generation_hash, sha256, rel_path, byte_size) "
        "VALUES (?, 1, 'genOLD', ?, ?, ?)",
        (old_asset.asset_id, old_asset.sha256, old_rel, len(b"oldbytes")),
    )
    db.execute(
        "INSERT INTO document_atoms "
        "(atom_id, file_id, generation_hash, ordinal, kind, schema_version) "
        "VALUES ('old-atom', 1, 'genOLD', 0, 'text', 1)"
    )
    db.commit()
    (artifact_root / old_rel).parent.mkdir(parents=True, exist_ok=True)
    (artifact_root / old_rel).write_bytes(b"oldbytes")

    new_asset = _asset()
    payloads = {new_asset.asset_id: b"abc"}

    def _fail_mid(conn, **_kw):
        # Simulate publish_generation retiring the OLD generation (rows + a
        # tombstone for its bytes) and inserting a partial new row mid-transaction.
        conn.execute(
            "DELETE FROM document_assets WHERE file_id=1 AND generation_hash='genOLD'"
        )
        conn.execute(
            "DELETE FROM document_atoms WHERE file_id=1 AND generation_hash='genOLD'"
        )
        conn.execute(
            "INSERT INTO artifact_delete_pending "
            "(file_id, vault_id, rel_path, generation_hash) VALUES (1, 1, ?, 'genOLD')",
            (old_rel,),
        )
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
        proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=_parsed(new_asset, payloads)
    )

    # The OLD generation was retired mid-transaction; rollback restored it.
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND generation_hash='genOLD'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genOLD'"
    ).fetchone()[0] == 1
    # The old on-disk bytes must NOT be tombstoned (they still have a live row).
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?", (old_rel,)
    ).fetchone()[0] == 0
    # The partial new-generation row is discarded.
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND generation_hash='genA'"
    ).fetchone()[0] == 0
    # Only the NEW generation's materialized bytes are tombstoned for the sweep.
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
        (new_asset.rel_path,),
    ).fetchone()[0] == 1
    db.close()


def test_compensation_failure_is_surfaced_not_silently_swallowed(
    tmp_path, artifact_root, monkeypatch, caplog
):
    """PRR-004/012: when the compensation (tombstone enqueue) itself fails after
    a publish failure, the error is surfaced at ERROR (not swallowed) and the
    publish failure remains non-fatal to the caller.
    """
    db = _new_db(tmp_path)
    asset = _asset()
    payloads = {asset.asset_id: b"abc"}

    def _fail_publish(conn, **_kw):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(
        "app.services.document_processor.artifact_store.publish_generation", _fail_publish
    )
    # Force the compensation itself to fail on its own commit.
    def _fail_tombstone(*_args, **_kw):
        raise RuntimeError("tombstone persistence failed")

    monkeypatch.setattr(
        "app.services.document_processor.artifact_store.enqueue_asset_cleanup",
        _fail_tombstone,
    )

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    import logging

    with caplog.at_level(logging.ERROR, logger="app.services.document_processor"):
        DocumentProcessor._publish_artifacts(
            proc_like,
            file_id=1,
            vault_id=1,
            generation_hash="genA",
            parsed=_parsed(asset, payloads),
        )

    # The compensation failure is surfaced at ERROR (not silently swallowed)
    # so orphaned bytes require operator reconciliation.
    assert any(
        "compensation failed" in r.message
        for r in caplog.records
        if r.name == "app.services.document_processor"
    )
    # No partial rows remain (publish rolled back) and no tombstone was written
    # (the compensation failed), matching the surfaced-orphan-bytes state.
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
    ).fetchone()[0] == 0
    db.close()


def test_materialize_failure_re_raises_and_tombstones_partial_bytes(
    tmp_path, artifact_root, monkeypatch
):
    """PRR-002 regression: a mid-materialize (disk I/O) failure occurs before any
    rows are published. The bytes written so far must be tombstoned for the sweep
    and the failure re-raised so the caller marks the file errored (it is NOT a
    non-fatal publish failure).
    """
    db = _new_db(tmp_path)
    a1 = _asset(b"a1bytes")
    a2 = _asset(b"a2bytes")
    payloads = {a1.asset_id: b"a1bytes", a2.asset_id: b"a2bytes"}
    parsed = ParsedDocument(
        atoms=[
            DocumentAtom(
                atom_id="a-0",
                schema_version=1,
                file_id=1,
                generation_hash="genA",
                ordinal=0,
                kind=AtomKind.IMAGE,
                raw_text="img",
                asset_id=a1.asset_id,
            )
        ],
        assets=(a1, a2),
        parser_fingerprint="unstructured:test",
        asset_payloads=payloads,
    )

    real_materialize = DocumentProcessor._materialize_asset

    def _fail_second(proc, asset, payloads_, file_id, vault_id, generation_hash):
        if asset.asset_id == a2.asset_id:
            raise OSError("disk full")
        return real_materialize(proc, asset, payloads_, file_id, vault_id, generation_hash)

    monkeypatch.setattr(DocumentProcessor, "_materialize_asset", _fail_second)

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    with pytest.raises(OSError):
        DocumentProcessor._publish_artifacts(
            proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=parsed
        )

    # No rows were published.
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
    ).fetchone()[0] == 0
    # The first (already-materialized) asset's bytes are tombstoned for the sweep.
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
        (a1.rel_path,),
    ).fetchone()[0] == 1
    db.close()
