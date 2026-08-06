"""Tests for the durable artifact store (issue #460).

Covers: confined asset filesystem (traversal/absolute/symlink rejection),
content-addressed idempotent storage, generation publication + replacement
(old-generation retirement only after new is durable), and the durable
tombstone sweep.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services import artifact_store
from app.services.document_artifacts import AtomKind, DocumentAtom


@pytest.fixture()
def artifact_root(tmp_path):
    """Monkeypatch the asset root to a per-test temp dir."""
    root = tmp_path / "vault-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    real = artifact_store.artifact_root
    artifact_store.artifact_root = lambda vault_id, settings_obj=None: root
    yield root
    artifact_store.artifact_root = real


@pytest.fixture()
def db(tmp_path):
    import tempfile as _t

    sqlite_path = str(tmp_path / "store.db")
    from app.models.database import init_db, run_migrations

    init_db(sqlite_path)
    run_migrations(sqlite_path)
    conn = __import__("sqlite3").connect(sqlite_path)
    conn.row_factory = __import__("sqlite3").Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, '/tmp/x.png', 'x.png', 'h', 1, 'indexed')"
    )
    conn.commit()
    yield conn
    conn.close()


def _atom(ordinal, asset_id=None, kind=AtomKind.TEXT, raw="x", generation_hash="genA"):
    return DocumentAtom(
        atom_id=f"atom-{ordinal}",
        schema_version=1,
        file_id=1,
        generation_hash=generation_hash,
        ordinal=ordinal,
        kind=kind,
        raw_text=raw,
        asset_id=asset_id,
    )


class TestPathConfinement:
    def test_accepts_internal_path(self, artifact_root):
        target = artifact_root / "gen" / "asset"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        resolved = artifact_store.resolve_confined("gen/asset", 1)
        assert resolved is not None
        assert resolved == target

    def test_rejects_traversal(self, artifact_root):
        assert artifact_store.resolve_confined("../../etc/passwd", 1) is None

    def test_rejects_absolute_path(self, artifact_root):
        assert artifact_store.resolve_confined(str(artifact_root) + "/../x", 1) is None

    @pytest.mark.skipif(os.name == "nt", reason="symlink privileges vary on Windows")
    def test_rejects_symlink_escape(self, artifact_root, tmp_path):
        outside = tmp_path / "outside"
        outside.write_bytes(b"secret")
        link = artifact_root / "link"
        try:
            link.symlink_to(outside, target_is_directory=False)
        except OSError:
            pytest.skip("symlink not permitted")
        assert artifact_store.resolve_confined("link", 1) is None


class TestAssetStorage:
    def test_store_is_content_addressed_and_confined(self, artifact_root):
        asset = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="genA", data=b"abc", mime_type="image/png"
        )
        assert asset.asset_id == asset.sha256
        dest = artifact_root / asset.rel_path
        assert dest.exists()
        assert dest.read_bytes() == b"abc"
        assert asset.byte_size == 3

    def test_idempotent_reprocess_same_asset_path(self, artifact_root):
        a1 = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="genA", data=b"abc"
        )
        a2 = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="genA", data=b"abc"
        )
        assert a1.asset_id == a2.asset_id
        assert a1.rel_path == a2.rel_path

    def test_unlink_removes_confined_file(self, artifact_root):
        asset = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="g", data=b"zz"
        )
        assert artifact_store.unlink_asset_rel(asset.rel_path, 1) is True
        assert not (artifact_root / asset.rel_path).exists()

    def test_unlink_refuses_out_of_root(self, artifact_root):
        assert artifact_store.unlink_asset_rel("../../x", 1) is False


class TestGenerationPublish:
    def test_publishes_rows_idempotently(self, db):
        atoms = [_atom(0), _atom(1)]
        artifact_store.publish_generation(
            db,
            file_id=1,
            vault_id=1,
            generation_hash="genA",
            atoms=atoms,
            assets=[],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p",
            implementation_version="1",
        )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genA'"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT active_generation_hash FROM files WHERE id=1"
        ).fetchone()[0] == "genA"

        # Reprocess unchanged generation -> idempotent (no duplicates).
        artifact_store.publish_generation(
            db,
            file_id=1,
            vault_id=1,
            generation_hash="genA",
            atoms=atoms,
            assets=[],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p",
            implementation_version="1",
        )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genA'"
        ).fetchone()[0] == 2

    def test_changed_generation_retires_old_and_tombstones_assets(self, db):
        from app.services.document_artifacts import DocumentAsset

        asset = DocumentAsset(
            asset_id="sha1", file_id=1, generation_hash="genA", sha256="sha1",
            rel_path="genA/sha1", byte_size=1,
        )
        artifact_store.publish_generation(
            db,
            file_id=1,
            vault_id=1,
            generation_hash="genA",
            atoms=[_atom(0, asset_id="sha1", kind=AtomKind.IMAGE, raw="img")],
            assets=[asset],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p",
            implementation_version="1",
        )
        db.commit()

        # New generation with a different hash retires genA rows + tombstones.
        assetB = DocumentAsset(
            asset_id="sha2", file_id=1, generation_hash="genB", sha256="sha2",
            rel_path="genB/sha2", byte_size=1,
        )
        artifact_store.publish_generation(
            db,
            file_id=1,
            vault_id=1,
            generation_hash="genB",
            atoms=[
                _atom(0, asset_id="sha2", kind=AtomKind.IMAGE, raw="img2", generation_hash="genB")
            ],
            assets=[assetB],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p",
            implementation_version="1",
        )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genA'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND generation_hash='genA'"
        ).fetchone()[0] == 0
        # Old asset path tombstoned (generation_hash is NULL: it is a retired
        # asset, not the active generation) for confined cleanup.
        pending = db.execute(
            "SELECT rel_path FROM artifact_delete_pending WHERE rel_path='genA/sha1'"
        ).fetchall()
        assert any(row["rel_path"] == "genA/sha1" for row in pending)
        assert db.execute(
            "SELECT COUNT(*) FROM document_atoms WHERE file_id=1 AND generation_hash='genB'"
        ).fetchone()[0] == 1


class TestSweep:
    def test_sweep_collects_pending_tombstones(self, db, artifact_root):
        # Write a real file, tombstone it, then sweep -> file removed + row gone.
        asset = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="g", data=b"data"
        )
        artifact_store.enqueue_asset_cleanup(
            db, file_id=1, vault_id=1, rel_paths=[asset.rel_path]
        )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_delete_pending"
        ).fetchone()[0] == 1
        removed, remaining = artifact_store.sweep_pending_asset_deletes(db)
        assert removed == 1
        assert remaining == 0
        assert not (artifact_root / asset.rel_path).exists()
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_delete_pending"
        ).fetchone()[0] == 0

    def test_sweep_out_of_root_path_stays_pending(self, db):
        artifact_store.enqueue_asset_cleanup(
            db, file_id=1, vault_id=1, rel_paths=["../../out"]
        )
        db.commit()
        removed, remaining = artifact_store.sweep_pending_asset_deletes(db)
        # Out-of-root paths are never unlinked; they remain for the reconciler.
        assert removed == 0
        assert remaining == 1
