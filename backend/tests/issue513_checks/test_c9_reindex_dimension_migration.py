"""C9 - AC9 (INGEST-010, contract updated by issue #691): dimension-changing
ingests are the admin reindex job's exclusive business.

Contracts under test:

  Part 1 - a bare-call ingest (``process_existing_file`` with NO
  ``vector_target`` — the async upload worker's shape) against a live index
  at a DIFFERENT embedding dimension must FAIL CLOSED: it raises
  ``DocumentProcessingError`` whose message names both dimensions and
  instructs running the admin reindex job, and the prior old-dimension index
  is fully intact afterwards (dim, row count, old-dimension searchability).
  Until #691 this call auto-migrated — committing a staged rebuild holding
  only the current file's rows, which wiped every other indexed file's
  vectors.

  Part 2 - the same refusal under ``reupload_safe_order=False``: the probe
  raises before any write, so the prior old-dimension vectors survive
  (still retrievable) and the ingest is reported failed.

  Part 3 - the reindex job's faithful shape still migrates: open
  ``begin_dimension_rebuild(NEW_DIM)``, reprocess through
  ``process_existing_file(vector_target=handle)`` (every vector-store call
  threads ``target=handle`` — pinned by kwarg-recording spies on
  ``init_table``/``add_chunks``/``delete_old_generation_by_file``/
  ``count_by_file``), the live OLD_DIM table keeps serving until the
  validated swap, and after ``commit_dimension_rebuild`` the new-dimension
  content is searchable with the old-generation rows gone.

  Part 4 - abort preservation: a partially-filled rebuild that is aborted
  leaves the live index fully intact (dim, rows, searchability) and drops
  the ``chunks_dim_rebuild`` temp table.

Pre-#691 expectation (RC-8 / INGEST-010): the bare call auto-migrated —
that behavior is the data-loss bug #691 removes; see the issue and the B02
acceptance checks (``tests/test_b02_bare_call_dim_rebuild.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""

OLD_DIM = 4
NEW_DIM = 6


class _FakeEmbeddingService:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ], []


def _record(rec_id: str, file_id: int, text: str, dim: int) -> dict:
    return {
        "id": rec_id,
        "text": text,
        "file_id": str(file_id),
        "vault_id": "1",
        "chunk_index": 0,
        "metadata": "{}",
        "embedding": [((i + 1) * 0.25) for i in range(dim)],
    }


async def _table_dim(store) -> int:  # noqa: ANN001
    schema = await store.table.schema()
    field = schema.field("embedding")
    return int(field.type.list_size)


def _cleanup_tmp(*paths) -> None:  # noqa: ANN001
    """Best-effort rmtree of the part's mkdtemp dirs (issue #691 review).

    Pools/stores may still hold open handles on Windows, so failures to
    delete are ignored -- this reduces residue, it cannot add failures.
    """
    import shutil

    for path in paths:
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


async def _reprocess_file(store, pool, db_vault_id: int, file_id: int, upload_path: Path, new_dim: int, vector_target=None):  # noqa: ANN001, ANN202
    """Drive the ingest step for one file.

    Without ``vector_target`` this is the bare call the async upload worker
    makes; with one, it is the reindex job's shape
    (``BackgroundProcessor._process_reindex_job`` threads the open rebuild
    handle through as ``vector_target``).
    """
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor

    chunk = ProcessedChunk(
        text="reindexed content with the new embedding model",
        metadata={"chunk_scale": "default", "raw_text": "reindexed content"},
        chunk_index=0,
    )
    processor = DocumentProcessor(
        pool=pool,
        embedding_service=_FakeEmbeddingService(dim=new_dim),
        vector_store=store,
    )
    with patch.object(
        processor,
        "_process_document_file",
        new=AsyncMock(
            return_value=(
                [chunk],
                "reindexed content",
                ParsedDocument(atoms=()),
            )
        ),
    ), patch(
        "app.services.document_processor.compute_file_hash", return_value="hashc9file"
    ), patch(
        "app.services.document_processor.compute_parent_windows"
    ):
        await processor.process_existing_file(
            file_id=file_id,
            file_path=str(upload_path),
            vault_id=db_vault_id,
            vector_target=vector_target,
        )


async def _scenario() -> tuple[str, str, str, str]:
    """Returns (part1, part2, part3, part4) reasons; empty string means pass."""
    import sqlite3

    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessingError
    from app.services.vector_store import (
        DIMENSION_REBUILD_TABLE,
        VectorStore,
    )

    tmp2: Path | None = None
    tmp3: Path | None = None
    tmp4: Path | None = None
    tmp = Path(tempfile.mkdtemp(prefix="c9_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, 'indexed')",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "hashc9file", 12),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()
    upload_path = tmp / "doc.txt"
    upload_path.write_text("seed content", encoding="utf-8")

    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")

    part1_reason = ""
    part2_reason = ""
    part3_reason = ""
    part4_reason = ""

    with patch.object(settings, "data_dir", tmp), \
            patch.object(settings, "wiki_enabled", False), \
            patch.object(settings, "wiki_compile_on_ingest", False), \
            patch.object(settings, "kms_enabled", False), \
            patch.object(settings, "multi_scale_indexing_enabled", False), \
            patch.object(settings, "contextual_chunking_enabled", False), \
            patch.object(settings, "optimize_mode", "manual"):

        # Seed the old-dimension native index.
        await store.init_table(OLD_DIM)
        await store.add_chunks([_record(f"{file_id}_0", file_id, "seed content", OLD_DIM)])
        seeded_count = await store.count_by_file(str(file_id))
        if seeded_count != 1:
            _cleanup_tmp(tmp)
            return f"harness invalid: seeded index has {seeded_count} rows", "", "", ""

        # ---- Part 1: bare-call dimension change must FAIL CLOSED (#691) ----
        refusal: DocumentProcessingError | None = None
        other_err: Exception | None = None
        try:
            await _reprocess_file(store, pool, vault_id, file_id, upload_path, NEW_DIM)
        except DocumentProcessingError as exc:
            refusal = exc
        except Exception as exc:  # noqa: BLE001
            other_err = exc
        if other_err is not None:
            part1_reason = (
                f"bare-call dimension change raised an unexpected error type: "
                f"{type(other_err).__name__}({other_err}) (issue #691)"
            )
        elif refusal is None:
            part1_reason = (
                "bare-call dimension-changing ingest did not refuse: "
                "auto-migration or silent success is the #691 data-loss bug"
            )
        else:
            message = str(refusal)
            if not (
                f"dimension {NEW_DIM}" in message
                and f"dimension {OLD_DIM}" in message
                and "reindex" in message.lower()
            ):
                part1_reason = (
                    f"refusal error is not actionable (must name both "
                    f"dimensions and the reindex remediation): {message}"
                )
            else:
                # The prior old-dimension index must be fully intact.
                dim = await _table_dim(store)
                count = await store.count_by_file(str(file_id))
                searchable = False
                if dim == OLD_DIM and count >= 1:
                    try:
                        results = await store.search(
                            _record("q", file_id, "q", OLD_DIM)["embedding"], limit=5
                        )
                        searchable = any(r.get("file_id") == str(file_id) for r in results)
                    except Exception:  # noqa: BLE001
                        searchable = False
                if dim != OLD_DIM or count < 1 or not searchable:
                    part1_reason = (
                        f"refused ingest damaged the prior index: dim={dim}, "
                        f"rows={count}, old-dimension content searchable={searchable}"
                    )
                else:
                    # The persisted (user-visible) error must be the
                    # actionable stable code, not the generic PARSE_FAILED
                    # reason (issue #691 review round).
                    conn = sqlite3.connect(db_path)
                    persisted = conn.execute(
                        "SELECT error_message FROM files WHERE id = ?",
                        (file_id,),
                    ).fetchone()[0]
                    conn.close()
                    if not persisted.startswith("DIMENSION_CHANGED:"):
                        part1_reason = (
                            f"refusal persisted as {persisted!r}, not the "
                            f"actionable DIMENSION_CHANGED code (issue #691)"
                        )

        # ---- Part 2: the same refusal under delete-first ordering ----
        tmp2 = Path(tempfile.mkdtemp(prefix="c9_db2_"))
        db_path2 = str(tmp2 / "app.db")
        init_db(db_path2)
        conn = sqlite3.connect(db_path2)
        conn.execute("INSERT INTO vaults (name) VALUES ('v2')")
        vault_id2 = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vault_id2, str(tmp2 / "doc.txt"), "doc.txt", "hashc9file", 12),
        )
        conn.commit()
        file_id2 = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        conn.close()
        (tmp2 / "doc.txt").write_text("seed content two", encoding="utf-8")

        pool2 = get_pool(db_path2, max_size=3)
        store2 = VectorStore(db_path=tmp2 / "lancedb")
        await store2.init_table(OLD_DIM)
        await store2.add_chunks([_record(f"{file_id2}_0", file_id2, "seed content two", OLD_DIM)])
        old_count = await store2.count_by_file(str(file_id2))
        if old_count != 1:
            _cleanup_tmp(tmp, tmp2)
            return (
                part1_reason,
                f"harness invalid: second seed has {old_count} rows",
                "",
                "",
            )

        rebuild_failed = False
        with patch.object(settings, "reupload_safe_order", False):
            try:
                await _reprocess_file(store2, pool2, vault_id2, file_id2, tmp2 / "doc.txt", NEW_DIM)
            except Exception:  # noqa: BLE001
                rebuild_failed = True
        survivors = await store2.count_by_file(str(file_id2))
        if not rebuild_failed:
            part2_reason = (
                "harness invalid: the dimension-changing ingest did not refuse "
                f"(unexpectedly succeeded; survivors={survivors}) (issue #691)"
            )
        elif survivors < old_count:
            part2_reason = (
                f"refused ingest destroyed the prior index: {old_count} -> {survivors} "
                f"old-dimension rows retrievable for file_id={file_id2} (issue #691)"
            )

        # ---- Part 3: the reindex job's vector_target path still migrates ----
        tmp3 = Path(tempfile.mkdtemp(prefix="c9_db3_"))
        db_path3 = str(tmp3 / "app.db")
        init_db(db_path3)
        conn = sqlite3.connect(db_path3)
        conn.execute("INSERT INTO vaults (name) VALUES ('v3')")
        vault_id3 = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vault_id3, str(tmp3 / "doc.txt"), "doc.txt", "hashc9file", 12),
        )
        conn.commit()
        file_id3 = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        conn.close()
        (tmp3 / "doc.txt").write_text("seed content three", encoding="utf-8")

        pool3 = get_pool(db_path3, max_size=3)
        store3 = VectorStore(db_path=tmp3 / "lancedb")
        await store3.init_table(OLD_DIM)
        # Old-generation rows for the reprocessed file: with them present, a
        # dropped target= on delete_old_generation_by_file is observable.
        await store3.add_chunks(
            [
                _record(f"{file_id3}_0", file_id3, "old-gen three", OLD_DIM),
                _record(f"{file_id3}_1", file_id3, "old-gen three b", OLD_DIM),
            ]
        )
        baseline3 = await store3.count_by_file(str(file_id3))
        if baseline3 != 2:
            _cleanup_tmp(tmp, tmp2, tmp3)
            return (
                part1_reason,
                part2_reason,
                f"harness invalid: third seed has {baseline3} rows",
                "",
            )

        handle3 = await store3.begin_dimension_rebuild(NEW_DIM)

        # Kwarg-recording spies: behavioral assertions alone discriminate only
        # the add_chunks/delete sites (a dropped target= at init_table merely
        # drifts the store's private dim cache and a dropped count target
        # passes while the live count is >= 1), so pin all FOUR threading
        # sites directly (issue #691 plan, C9 Part 3).
        spy_names = (
            "init_table",
            "add_chunks",
            "delete_old_generation_by_file",
            "count_by_file",
        )
        originals = {name: getattr(store3, name) for name in spy_names}
        spy_calls: dict[str, list[dict]] = {name: [] for name in spy_names}

        def _install_spy(name: str, fn) -> None:  # noqa: ANN001
            async def wrapper(*args, **kwargs):  # noqa: ANN001, ANN202
                spy_calls[name].append(kwargs)
                return await fn(*args, **kwargs)

            setattr(store3, name, wrapper)

        for _name, _fn in originals.items():
            _install_spy(_name, _fn)
        try:
            await _reprocess_file(
                store3, pool3, vault_id3, file_id3, tmp3 / "doc.txt", NEW_DIM,
                vector_target=handle3,
            )
            # Live OLD_DIM table untouched during the window; new-dim rows
            # landed ONLY in the rebuild temp table.
            during_dim = await _table_dim(store3)
            during_live = await originals["count_by_file"](str(file_id3))
            temp_rows = await handle3.table.count_rows()
            await store3.commit_dimension_rebuild(handle3)
        finally:
            for _name, _fn in originals.items():
                setattr(store3, _name, _fn)

        after_dim = await _table_dim(store3)
        after_count = await store3.count_by_file(str(file_id3))
        after_searchable = False
        try:
            results3 = await store3.search(
                _record("q", file_id3, "q", NEW_DIM)["embedding"], limit=5
            )
            after_searchable = any(
                r.get("file_id") == str(file_id3) for r in results3
            )
        except Exception:  # noqa: BLE001
            after_searchable = False

        unthreaded = {
            name: [
                kw for kw in spy_calls[name] if kw.get("target") is not handle3
            ]
            for name in spy_names
        }
        missing = [name for name in spy_names if not spy_calls[name]]
        if missing or any(unthreaded.values()):
            part3_reason = (
                f"vector_target threading regressed: no-call sites={missing}, "
                f"calls without target=handle: "
                f"{ {k: len(v) for k, v in unthreaded.items() if v} } (issue #691)"
            )
        elif during_dim != OLD_DIM or during_live != baseline3 or temp_rows < 1:
            part3_reason = (
                f"rebuild window disturbed the live index: dim={during_dim}, "
                f"live rows={during_live} (baseline {baseline3}), "
                f"temp rows={temp_rows}"
            )
        elif after_dim != NEW_DIM or after_count != 1 or not after_searchable:
            part3_reason = (
                f"post-commit store wrong: dim={after_dim}, rows={after_count} "
                f"(old-gen 2 -> 1 new-gen row expected), new-dim content "
                f"searchable={after_searchable}"
            )

        # ---- Part 4: abort preserves the old index and drops the temp ----
        tmp4 = Path(tempfile.mkdtemp(prefix="c9_db4_"))
        db_path4 = str(tmp4 / "app.db")
        init_db(db_path4)
        conn = sqlite3.connect(db_path4)
        conn.execute("INSERT INTO vaults (name) VALUES ('v4')")
        vault_id4 = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vault_id4, str(tmp4 / "doc.txt"), "doc.txt", "hashc9file", 12),
        )
        conn.commit()
        file_id4 = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        conn.close()
        (tmp4 / "doc.txt").write_text("seed content four", encoding="utf-8")

        pool4 = get_pool(db_path4, max_size=3)
        store4 = VectorStore(db_path=tmp4 / "lancedb")
        await store4.init_table(OLD_DIM)
        await store4.add_chunks([_record(f"{file_id4}_0", file_id4, "old-gen four", OLD_DIM)])
        baseline4 = await store4.count_by_file(str(file_id4))
        if baseline4 != 1:
            _cleanup_tmp(tmp, tmp2, tmp3, tmp4)
            return (
                part1_reason,
                part2_reason,
                part3_reason,
                f"harness invalid: fourth seed has {baseline4} rows",
            )

        handle4 = await store4.begin_dimension_rebuild(NEW_DIM)
        await store4.add_chunks(
            [_record("c9-new-4", file_id4, "fresh new-dim row", NEW_DIM)],
            target=handle4,
        )
        temp_rows4 = await handle4.table.count_rows()
        if temp_rows4 < 1:
            _cleanup_tmp(tmp, tmp2, tmp3, tmp4)
            return (
                part1_reason,
                part2_reason,
                part3_reason,
                "harness invalid: partial rebuild fill wrote 0 rows; the abort "
                "assertions would pass vacuously",
            )
        await store4.abort_dimension_rebuild(handle4)
        aborted_dim = await _table_dim(store4)
        aborted_count = await store4.count_by_file(str(file_id4))
        aborted_searchable = False
        try:
            results4 = await store4.search(
                _record("q", file_id4, "q", OLD_DIM)["embedding"], limit=5
            )
            aborted_searchable = any(
                r.get("file_id") == str(file_id4) for r in results4
            )
        except Exception:  # noqa: BLE001
            aborted_searchable = False
        table_names4 = await store4.db.table_names()
        if aborted_dim != OLD_DIM or aborted_count != baseline4 or not aborted_searchable:
            part4_reason = (
                f"aborted rebuild damaged the live index: dim={aborted_dim}, "
                f"rows={aborted_count}, old-dimension content "
                f"searchable={aborted_searchable}"
            )
        elif DIMENSION_REBUILD_TABLE in table_names4:
            part4_reason = (
                f"aborted rebuild left the temp table '{DIMENSION_REBUILD_TABLE}' "
                f"behind (tables: {table_names4})"
            )

    _cleanup_tmp(tmp, tmp2, tmp3, tmp4)
    return part1_reason, part2_reason, part3_reason, part4_reason


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    part1_reason, part2_reason, part3_reason, part4_reason = asyncio.run(_scenario())
    for reason in (part1_reason, part2_reason, part3_reason, part4_reason):
        if reason:
            print(f"C9 CHECK: FAIL: {reason}")
            return 1
    print("C9 CHECK: PASS")
    return 0


def test_c9_reindex_dimension_migration() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
