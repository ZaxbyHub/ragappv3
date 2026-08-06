"""Failure-compensation tests for ``document_processor._publish_artifacts`` (#460).

Regression for reviewer finding: when ``publish_generation`` raises after asset
bytes were already written to disk, the compensation must (a) drop any partial
new-generation rows via ``retire_generation_quick``, and (b) tombstone the
written bytes for the background sweep — with a compensation failure surfaced
rather than silently swallowed. Also guards that a successful publish does NOT
tombstone its own (now-live) assets.
"""
from __future__ import annotations

import os
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


def _parsed(ordinal, asset):
    return ParsedDocument(
        atoms=[
            DocumentAtom(
                atom_id=f"a-{ordinal}",
                schema_version=1,
                file_id=1,
                generation_hash="genA",
                ordinal=ordinal,
                kind=AtomKind.IMAGE,
                raw_text="img",
                asset_id=asset.asset_id,
            )
        ],
        assets=[asset],
        parser_fingerprint="unstructured:test",
    )


@pytest.fixture()
def artifact_root(tmp_path):
    root = tmp_path / "vault-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    real = artifact_store.artifact_root
    artifact_store.artifact_root = lambda vault_id, settings_obj=None: root
    yield root
    artifact_store.artifact_root = real


def test_failed_publish_tombstones_written_bytes(tmp_path, artifact_root, monkeypatch):
    db = _new_db(tmp_path)

    def _raise(*_a, **_k):
        raise ValueError("constraint failure")

    monkeypatch.setattr(
        "app.services.document_processor.artifact_store.publish_generation", _raise
    )

    # Write the asset bytes to disk just like store_asset_bytes would.
    asset = DocumentAsset(
        asset_id="sha1", file_id=1, generation_hash="genA", sha256="sha1",
        rel_path="genA/sha1", byte_size=3,
    )
    target = artifact_root / asset.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"abc")

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    DocumentProcessor._publish_artifacts(
        proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=_parsed(0, asset)
    )

    # No partial rows leaked.
    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND generation_hash='genA'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genA'"
    ).fetchone()[0] == 0
    # The written bytes are tombstoned for the sweep.
    pending = db.execute(
        "SELECT rel_path, generation_hash FROM artifact_delete_pending "
        "WHERE rel_path='genA/sha1'"
    ).fetchone()
    assert pending is not None
    assert pending["generation_hash"] == "genA"
    db.close()


def test_successful_publish_does_not_tombstone_live_assets(tmp_path, artifact_root):
    db = _new_db(tmp_path)
    asset = DocumentAsset(
        asset_id="sha1", file_id=1, generation_hash="genA", sha256="sha1",
        rel_path="genA/sha1", byte_size=3,
    )
    target = artifact_root / asset.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"abc")

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    # No monkeypatch: publish succeeds normally.
    DocumentProcessor._publish_artifacts(
        proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=_parsed(0, asset)
    )

    assert db.execute(
        "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND rel_path='genA/sha1'"
    ).fetchone()[0] == 1
    # Successful publish must NOT enqueue a tombstone for its own live asset.
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path='genA/sha1'"
    ).fetchone()[0] == 0
    db.close()
