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

from app.config import settings
from app.services import artifact_store
from app.services.document_artifacts import (
    AtomKind,
    DocumentAsset,
    DocumentAtom,
    ParsedDocument,
)


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

    def test_same_bytes_do_not_alias_across_files(self, artifact_root):
        """PRR-015: identical content yields the same content-address, but the
        per-owner (file_id) path keeps two files' assets distinct on disk.
        """
        a1 = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="genA", data=b"same"
        )
        a2 = artifact_store.store_asset_bytes(
            file_id=2, vault_id=2, generation_hash="genA", data=b"same"
        )
        assert a1.asset_id == a2.asset_id
        assert a1.rel_path != a2.rel_path
        assert (artifact_root / a1.rel_path).exists()
        assert (artifact_root / a2.rel_path).exists()

    def test_read_path_via_resolve_confined(self, artifact_root):
        """PRR-016: the production read path (resolve_confined) locates a stored
        asset inside the vault root, and rejects traversal.
        """
        asset = artifact_store.store_asset_bytes(
            file_id=1, vault_id=1, generation_hash="genA", data=b"xyz"
        )
        resolved = artifact_store.resolve_confined(asset.rel_path, vault_id=1)
        assert resolved is not None
        assert resolved.exists()
        assert resolved.read_bytes() == b"xyz"
        # Traversal is refused at the read path.
        assert artifact_store.resolve_confined("../../etc/hosts", vault_id=1) is None


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

    def test_cross_file_shared_asset_keeps_both_rows(self, db):
        """Regression: the same asset_id across two files must not cross-delete.

        The schema must rely on the composite ``UNIQUE(file_id, generation_hash,
        asset_id)`` only — a global ``asset_id UNIQUE`` would make the second
        file's ``INSERT OR REPLACE`` delete the first file's asset row (breaking
        its provenance) when two files share identical extracted bytes.
        """
        from app.services.document_artifacts import DocumentAsset

        db.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (1, '/tmp/y.png', 'y.png', 'h2', 1, 'indexed')"
        )
        db.commit()

        shared = DocumentAsset(
            asset_id="shaa", file_id=1, generation_hash="genA", sha256="shaa",
            rel_path="genA/shaa", byte_size=1,
        )
        artifact_store.publish_generation(
            db, file_id=1, vault_id=1, generation_hash="genA",
            atoms=[_atom(0, asset_id="shaa", kind=AtomKind.IMAGE, raw="img")],
            assets=[shared],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p", implementation_version="1",
        )
        db.commit()

        shared2 = DocumentAsset(
            asset_id="shaa", file_id=2, generation_hash="genA", sha256="shaa",
            rel_path="genA/shaa", byte_size=1,
        )
        artifact_store.publish_generation(
            db, file_id=2, vault_id=1, generation_hash="genA",
            atoms=[_atom(0, asset_id="shaa", kind=AtomKind.IMAGE, raw="img")],
            assets=[shared2],
            stage_states=[{"stage": "parse", "status": "succeeded"}],
            parser_fingerprint="p", implementation_version="1",
        )
        db.commit()

        # Both files must still own their asset row (no cross-file REPLACE).
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE asset_id='shaa'"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE file_id=1 AND asset_id='shaa'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE file_id=2 AND asset_id='shaa'"
        ).fetchone()[0] == 1
        # No tombstone should have been enqueued (nothing was retired).
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_delete_pending"
        ).fetchone()[0] == 0


    def test_devault_collects_then_batch_tombstones_before_files_delete(self, db):
        """Regression (final-critic finding 1): vault-wide asset cleanup must
        collect + enqueue BEFORE the files delete cascades document_assets away.

        Replicates the corrected `delete_vault` ordering: collect every asset
        path for the vault, enqueue tombstones, only then delete the files rows.
        """
        from app.services.document_artifacts import DocumentAsset

        # Two files in vault 1, each with an asset.
        for i, (fid, pth) in enumerate([(1, "a.png"), (2, "b.png")], start=1):
            db.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (1, ?, ?, ?, 1, 'indexed')",
                (pth, pth, f"h{i}"),
            )
            db.commit()
            artifact_store.publish_generation(
                db, file_id=fid, vault_id=1, generation_hash="genA",
                atoms=[_atom(0, kind=AtomKind.IMAGE, raw="img")],
                assets=[DocumentAsset(
                    asset_id=f"sha{fid}", file_id=fid, generation_hash="genA",
                    sha256=f"sha{fid}", rel_path=f"1/genA/sha{fid}", byte_size=1,
                )],
                stage_states=[{"stage": "parse", "status": "succeeded"}],
                parser_fingerprint="p", implementation_version="1",
            )
            db.commit()

        paths = artifact_store.devault_asset_rel_paths(db, vault_id=1)
        assert sorted(paths) == ["1/genA/sha1", "1/genA/sha2"]
        artifact_store.enqueue_asset_cleanup(db, file_id=0, vault_id=1, rel_paths=paths)
        # Cascade the file rows away (what delete_vault does AFTER the collect).
        db.execute("DELETE FROM files WHERE vault_id = 1")
        db.commit()

        pending = {r["rel_path"] for r in db.execute(
            "SELECT rel_path FROM artifact_delete_pending WHERE vault_id=1"
        ).fetchall()}
        assert pending == {"1/genA/sha1", "1/genA/sha2"}


class TestGenerationBounds:
    def test_publish_rejects_asset_count_over_limit(self, db, monkeypatch):
        """Regression (final-critic finding 6): max_assets_per_generation is a
        real runtime bound, not an inert config knob."""
        from app.services.document_artifacts import DocumentAsset

        monkeypatch.setattr(artifact_store.settings, "max_assets_per_generation", 1)
        assets = [
            DocumentAsset(asset_id=f"a{i}", file_id=1, generation_hash="genA",
                          sha256=f"a{i}", rel_path=f"1/genA/a{i}", byte_size=1)
            for i in (1, 2)
        ]
        with pytest.raises(ValueError, match="assets"):
            artifact_store.publish_generation(
                db, file_id=1, vault_id=1, generation_hash="genA",
                atoms=[_atom(0)], assets=assets,
                stage_states=[{"stage": "parse", "status": "succeeded"}],
                parser_fingerprint="p", implementation_version="1",
            )

    def test_publish_rejects_asset_bytes_over_limit(self, db, monkeypatch):
        from app.services.document_artifacts import DocumentAsset

        monkeypatch.setattr(artifact_store.settings, "max_asset_bytes_per_generation", 4)
        asset = DocumentAsset(asset_id="big", file_id=1, generation_hash="genA",
                              sha256="big", rel_path="1/genA/big", byte_size=100)
        with pytest.raises(ValueError, match="exceeding"):
            artifact_store.publish_generation(
                db, file_id=1, vault_id=1, generation_hash="genA",
                atoms=[_atom(0)], assets=[asset],
                stage_states=[{"stage": "parse", "status": "succeeded"}],
                parser_fingerprint="p", implementation_version="1",
            )
        # No partial rows / tombstones on a bound rejection.
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_delete_pending"
        ).fetchone()[0] == 0

    def test_publish_artifacts_enforces_asset_count_limit(
        self, db, artifact_root, monkeypatch
    ):
        """PRR-027: the configured asset-count bound is enforced through the
        DocumentProcessor publish path (not only via direct ``publish_generation``),
        and the processor compensates the materialized bytes.
        """
        from app.services.document_processor import DocumentProcessor

        class _Pool:
            def __init__(self, conn):
                self.conn = conn

            def get_connection(self):
                return self.conn

            def release_connection(self, _conn):
                pass

        monkeypatch.setattr(artifact_store.settings, "max_assets_per_generation", 1)
        asset = DocumentAsset(
            asset_id="a1", file_id=1, generation_hash="genA", sha256="a1",
            rel_path=artifact_store.compute_asset_rel_path(1, "genA", "a1"),
            mime_type="image/png", byte_size=1,
        )
        asset2 = DocumentAsset(
            asset_id="a2", file_id=1, generation_hash="genA", sha256="a2",
            rel_path=artifact_store.compute_asset_rel_path(1, "genA", "a2"),
            mime_type="image/png", byte_size=1,
        )
        parsed = ParsedDocument(
            atoms=[DocumentAtom(atom_id="a1", schema_version=1, file_id=1,
                                generation_hash="genA", ordinal=0, kind=AtomKind.IMAGE,
                                raw_text="img", asset_id="a1")],
            assets=(asset, asset2),
            parser_fingerprint="unstructured:test",
            asset_payloads={asset.asset_id: b"x", asset2.asset_id: b"y"},
        )
        proc_like = object.__new__(DocumentProcessor)
        proc_like.pool = _Pool(db)
        DocumentProcessor._publish_artifacts(
            proc_like, file_id=1, vault_id=1, generation_hash="genA", parsed=parsed
        )
        # The bound rejection is non-fatal but must persist NO rows and tombstone
        # the materialized bytes for the sweep.
        assert db.execute(
            "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM document_atoms WHERE file_id=1"
        ).fetchone()[0] == 0
        pending = {
            r["rel_path"]
            for r in db.execute("SELECT rel_path FROM artifact_delete_pending")
        }
        # The materializer content-addresses the payload bytes, so the actual
        # written paths derive from sha256(b"x")/sha256(b"y").
        import hashlib

        real_x = artifact_store.compute_asset_rel_path(1, "genA", hashlib.sha256(b"x").hexdigest())
        real_y = artifact_store.compute_asset_rel_path(1, "genA", hashlib.sha256(b"y").hexdigest())
        assert real_x in pending
        assert real_y in pending


class TestBackgroundTaskLifecycle:
    def test_stop_cancels_artifact_sweep_task(self):
        """Regression (final-critic finding 7): ``BackgroundProcessor.stop`` must
        cancel the artifact-delete sweep task, mirroring the vector sweep, so it
        is not left sleeping (and firing against a closed pool) across restarts.
        """
        import asyncio
        import types

        from app.services.background_tasks import BackgroundProcessor

        proc = object.__new__(BackgroundProcessor)
        proc._running = True
        proc.queue = asyncio.Queue()
        proc.enrichment_queue = asyncio.Queue()
        proc.shutdown_event = asyncio.Event()
        proc._worker_tasks = []
        proc._enrichment_worker_task = None
        proc._vector_delete_sweep_task = None
        proc._reindex_worker_task = None
        proc.processor = types.SimpleNamespace(vector_store=None)

        async def _sweep_forever():
            await asyncio.sleep(3600)

        async def _scenario():
            sweep = asyncio.create_task(_sweep_forever())
            proc._artifact_delete_sweep_task = sweep
            await proc.stop(timeout=1.0)
            return sweep

        sweep = asyncio.run(_scenario())
        assert sweep.cancelled()

    def test_stop_drains_atom_enrichment_queue(self):
        """Regression (review finding): graceful stop must drain the atom
        (multimodal) enrichment queue before cancelling its worker, rather than
        dropping queued artifacts."""
        import asyncio
        import types

        from app.services.background_tasks import (
            AtomEnrichmentTaskItem,
            BackgroundProcessor,
        )

        processed = []

        proc = object.__new__(BackgroundProcessor)
        proc._running = True
        proc.queue = asyncio.Queue()
        proc.enrichment_queue = asyncio.Queue()
        proc.atom_enrichment_queue = asyncio.Queue()
        proc.shutdown_event = asyncio.Event()
        proc._worker_tasks = []
        proc._enrichment_worker_task = None
        proc._vector_delete_sweep_task = None
        proc._reindex_worker_task = None
        proc.processor = types.SimpleNamespace(vector_store=None)

        async def _atom_worker():
            while True:
                item = await proc.atom_enrichment_queue.get()
                processed.append(item.file_id)
                proc.atom_enrichment_queue.task_done()

        proc.atom_enrichment_queue.put_nowait(
            AtomEnrichmentTaskItem(
                file_id=7, vault_id=1, generation_hash="g", file_hash="h",
                document_title="d", attempt=0,
            )
        )

        async def _scenario():
            worker = asyncio.create_task(_atom_worker())
            proc._atom_enrichment_worker_task = worker
            await proc.stop(timeout=5.0)
            return worker

        worker = asyncio.run(_scenario())
        # The enqueued artifact was processed before the worker was cancelled.
        assert processed == [7]
        # A completed worker may have finished; if still running it must be cancelled.
        assert worker.cancelled() or worker.done()

    def test_stop_does_not_cancel_atom_worker_when_queued(self):
        """Parameterized guard: with a live queued item and a slow consumer, the
        worker must NOT be cancelled before the queue drains (bounded by timeout)."""
        import asyncio
        import types

        from app.services.background_tasks import (
            AtomEnrichmentTaskItem,
            BackgroundProcessor,
        )

        processed = []

        proc = object.__new__(BackgroundProcessor)
        proc._running = True
        proc.queue = asyncio.Queue()
        proc.enrichment_queue = asyncio.Queue()
        proc.atom_enrichment_queue = asyncio.Queue()
        proc.shutdown_event = asyncio.Event()
        proc._worker_tasks = []
        proc._enrichment_worker_task = None
        proc._vector_delete_sweep_task = None
        proc._reindex_worker_task = None
        proc.processor = types.SimpleNamespace(vector_store=None)

        async def _atom_worker():
            while True:
                item = await proc.atom_enrichment_queue.get()
                await asyncio.sleep(0)
                processed.append(item.file_id)
                proc.atom_enrichment_queue.task_done()

        proc.atom_enrichment_queue.put_nowait(
            AtomEnrichmentTaskItem(
                file_id=9, vault_id=1, generation_hash="g", file_hash="h",
                document_title="d", attempt=0,
            )
        )

        async def _scenario():
            worker = asyncio.create_task(_atom_worker())
            proc._atom_enrichment_worker_task = worker
            await proc.stop(timeout=5.0)
            return worker

        worker = asyncio.run(_scenario())
        assert processed == [9]
        assert worker.cancelled() or worker.done()


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

    def test_sweep_attempts_stop_growing_after_threshold(self, db):
        """Regression: attempts must be persisted but capped once a path is stuck.

        Repeated sweeps must not rewrite an unboundedly growing counter and must
        leave the row pending (removed only on success), not silently drop it.
        """
        artifact_store.enqueue_asset_cleanup(
            db, file_id=1, vault_id=1, rel_paths=["../../stuck"]
        )
        db.commit()
        prev = 0
        for _ in range(12):
            removed, remaining = artifact_store.sweep_pending_asset_deletes(db)
            assert removed == 0
            assert remaining == 1
            attempts = db.execute(
                "SELECT attempts FROM artifact_delete_pending WHERE id = 1"
            ).fetchone()[0]
            assert attempts >= prev  # monotonically non-decreasing
            assert attempts <= artifact_store._MAX_UNLINK_ATTEMPTS + 1  # capped
            prev = attempts
        # Still present (never silently dropped) and capped.
        assert db.execute(
            "SELECT COUNT(*) FROM artifact_delete_pending WHERE id = 1"
        ).fetchone()[0] == 1
