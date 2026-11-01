"""Pre-fix acceptance checks for issue #704 (audit remediation, batch B-15).

Ingest signals + write-permit hardening. Each test pins one acceptance
criterion from the issue trace (AC1-AC10); AC11/AC12 are covered by the
existing suites named in the check wrappers, not by new tests here.

Harness idioms are reused verbatim from the frozen suites:
- ``_FakeEmbeddingService`` / ``_FakeVectorStore`` / pool + retry seeding
  from ``tests/test_failed_chunks_retry.py`` (AC2).
- ``_PartitionFailurePatch`` and the issue562 ``process_existing_file``
  setup from ``tests/test_issue562_ingest_error_redaction.py`` (AC3).
- the ``_publish_artifacts`` fixture shapes from
  ``tests/test_artifact_compensation.py`` (AC6).
"""

import ast
import asyncio
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies the way the sibling suites do.
for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *a, **k: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *a, **k: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    for _name, _sub in (
        ("unstructured", _unstructured),
        ("unstructured.partition", _unstructured.partition),
        ("unstructured.partition.auto", _unstructured.partition.auto),
        ("unstructured.chunking", _unstructured.chunking),
        ("unstructured.chunking.title", _unstructured.chunking.title),
        ("unstructured.documents", _unstructured.documents),
        ("unstructured.documents.elements", _unstructured.documents.elements),
    ):
        sys.modules[_name] = _sub

from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.services import artifact_store  # noqa: E402
from app.services.chunking import ProcessedChunk  # noqa: E402
from app.services.contextual_chunking import ContextualChunker  # noqa: E402
from app.services.document_artifacts import (  # noqa: E402
    AtomKind,
    DocumentAsset,
    DocumentAtom,
    ParsedDocument,
)
from app.services.document_processor import (  # noqa: E402
    INGEST_ERROR_PARSE_FAILED,
    DocumentProcessingError,
    DocumentProcessor,
    classify_ingest_error,
)
from app.services.embeddings import EmbeddingError  # noqa: E402

# ── Shared fakes (test_failed_chunks_retry.py idioms) ───────────────────────


class _FakeEmbeddingService:
    """Fake embedding service matching embed_batch(fail_fast=False) contract."""

    def __init__(self, fail_indices=None):
        self._fail = set(fail_indices or [])
        self._call_count = 0

    async def embed_batch(self, texts, fail_fast=False):
        self._call_count += 1
        idx = self._call_count - 1
        if idx in self._fail:
            return ([None], [0])
        return ([[0.1, 0.2, 0.3]], [])


class _FakeVectorStore:
    """Fake vector store capturing add_chunks + supporting get_chunks_by_uid."""

    def __init__(self):
        self.added_records = []
        self._existing_ids = set()

    async def add_chunks(self, records, generation_prefix=None):
        for r in records:
            self.added_records.append(r)
            self._existing_ids.add(r["id"])

    async def get_chunks_by_uid(self, chunk_uids):
        return [
            {"id": uid, "text": "existing"}
            for uid in chunk_uids
            if uid in self._existing_ids
        ]


# ── Parser-failure patch (test_issue562_ingest_error_redaction.py idiom) ─────


_partition_error_holder = {"exc": ValueError("simulated corrupt document structure")}


def _failing_partition(*_args, **_kwargs):
    raise _partition_error_holder["exc"]


class _PartitionFailurePatch:
    """Swap in the failing partition for the duration of a test only."""

    def __init__(self, exc=None):
        self._exc = exc

    def __enter__(self):
        import unstructured.partition.auto as auto_mod

        self._prev_exc = _partition_error_holder["exc"]
        if self._exc is not None:
            _partition_error_holder["exc"] = self._exc
        self._auto = auto_mod
        self._original = auto_mod.partition
        auto_mod.partition = _failing_partition
        return self

    def __exit__(self, *_exc):
        self._auto.partition = self._original
        _partition_error_holder["exc"] = self._prev_exc
        return False


# ── AC1: embedding/vector-store failures must not read as PARSE_FAILED ──────


def test_embedding_failure_not_classified_as_parse_failed():
    code = classify_ingest_error(EmbeddingError("provider down"))
    assert code != INGEST_ERROR_PARSE_FAILED


# ── AC2: full chunk recovery promotes a partial file to 'indexed' ───────────


class FullChunkRecoveryTest(unittest.TestCase):
    """A fully-successful retry of every failed chunk must clear 'partial'.

    Mirrors the seeding + public entry point of
    ``tests/test_failed_chunks_retry.py`` (file row + failed_chunks rows +
    ``retry_failed_chunks``), but seeds status='partial' with a single failed
    chunk so a full recovery has an unambiguous correct outcome.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="b15-ac2-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=5)
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at,"
                " updated_at) VALUES ('V', 'v', 'private', '2026-01-01',"
                " '2026-01-01')"
            )
            self.vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='V'"
            ).fetchone()[0]
            # A partially-indexed file: one chunk failed embedding.
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status,"
                " chunk_count, chunks_failed, vault_id, file_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    70,
                    "f.txt",
                    "/uploads/f.txt",
                    100,
                    "partial",
                    2,
                    1,
                    self.vault_id,
                    "abc12345",
                ),
            )
            meta = json.dumps(
                {
                    "raw_text": "failed chunk 1",
                    "chunk_index": 1,
                    "chunk_scale": "default",
                    "chunk_uid": "70_1",
                    "chunk_position": 1,
                    "parent_window_start": None,
                    "parent_window_end": None,
                    "page_number": None,
                    "chunk_bbox": None,
                    "total_chunks": 3,
                }
            )
            conn.execute(
                "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text,"
                " chunk_metadata) VALUES (?, ?, ?, ?)",
                (70, 1, "failed chunk 1", meta),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def tearDown(self):
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_chunk_recovery_promotes_partial_to_indexed(self):
        emb = _FakeEmbeddingService()  # no failures: the retry fully recovers
        vs = _FakeVectorStore()
        proc = DocumentProcessor(
            chunk_size_chars=500,
            chunk_overlap_chars=0,
            vector_store=vs,
            embedding_service=emb,
            pool=self.pool,
        )

        asyncio.run(proc.retry_failed_chunks(70))

        conn = self.pool.get_connection()
        try:
            status = conn.execute("SELECT status FROM files WHERE id = 70").fetchone()[
                0
            ]
        finally:
            self.pool.release_connection(conn)
        assert status == "indexed"


# ── AC3: failed re-ingest before the vector write keeps the prior 'indexed' ──


class FailedReingestKeepsIndexedTest(unittest.TestCase):
    """A parse failure before any destructive step must not bury 'indexed'.

    Mirrors the ``process_existing_file`` harness of
    ``tests/test_issue562_ingest_error_redaction.py`` (init_db, vault 7,
    settings.data_dir swap, ``_PartitionFailurePatch``) but seeds the row
    status='indexed' with live chunks in the (fake) vector store: the prior
    generation is still fully searchable, so the failure of the NEW attempt
    must leave the row's status truthful.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="b15-ac3-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (7, 'V', '')"
        )
        conn.commit()
        conn.close()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.vector_store = _FakeVectorStore()
        # The prior generation's 3 live chunks.
        self.vector_store._existing_ids = {
            "5_hash1234_default_0",
            "5_hash1234_default_1",
            "5_hash1234_default_2",
        }
        self.processor = DocumentProcessor(
            chunk_size_chars=2000,
            chunk_overlap_chars=200,
            vector_store=self.vector_store,
            pool=self.pool,
        )
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self.temp_dir)
        self.vault_uploads = Path(self.temp_dir) / "vaults" / "7" / "uploads"
        self.vault_uploads.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        settings.data_dir = self._original_data_dir
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_failed_reingest_before_vector_write_keeps_indexed(self):
        doc = self.vault_uploads / "reingest-probe.txt"
        doc.write_text("x", encoding="utf-8")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, chunk_count, file_hash) VALUES (5, 7, ?, 'reingest-probe.txt',"
            " 10, 'indexed', 3, 'hash1234')",
            (str(doc),),
        )
        conn.commit()
        conn.close()

        with _PartitionFailurePatch(), self.assertRaises(Exception):
            asyncio.run(self.processor.process_existing_file(5, str(doc), vault_id=7))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM files WHERE id = 5").fetchone()
        conn.close()
        assert row["status"] == "indexed"


# ── AC4: enrichment_ms must not be a dead _STAGE_TIMING_FIELDS key ──────────


def test_enrichment_ms_is_not_a_dead_timing_key():
    from app.services import document_processor as dp

    declared = "enrichment_ms" in dp._STAGE_TIMING_FIELDS
    source_path = Path(dp.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    recorded = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_add_elapsed_ms"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "enrichment_ms"
        for node in ast.walk(tree)
    )
    dead = declared and not recorded
    assert int(dead) == 0


# ── AC5 (PRESERVING): image docstring must keep its #703 fix ────────────────


def test_image_docstring_does_not_promise_recorded_not_searchable():
    doc = inspect.getdoc(DocumentProcessor._process_image_file) or ""
    stale = "recorded but not searchable" in doc
    assert int(stale) == 0


# ── AC6: a compensated publish failure must leave a durable stage marker ────


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def get_connection(self):
        return self.conn

    def release_connection(self, _conn):
        pass


def _asset(data=b"abc"):
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


def _parsed_doc(asset, payloads=None):
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


def test_publish_failure_leaves_durable_stage_marker(tmp_path, monkeypatch):
    """After a publish failure (materialization succeeded, publish_generation
    raised), a durable marker row must be findable in ingestion_stage_states
    for (file_id, generation_hash) with stage='publish' and status='failed_retryable' (a base-schema-admitted status; bare 'failed' violates the table CHECK — corrected pre-freeze).

    Fixture mirrors tests/test_artifact_compensation.py: real SQLite schema,
    one files row, one deferred asset; the function under test is patched at
    the ``document_processor.artifact_store`` reference so the raise happens
    with publish_reached=True (materialization already done).
    """
    sqlite_path = str(tmp_path / "db.sqlite")
    init_db(sqlite_path)
    run_migrations(sqlite_path)
    db = sqlite3.connect(sqlite_path)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size,"
        " status) VALUES (1, '/tmp/x.png', 'x.png', 'h', 1, 'indexed')"
    )
    db.commit()

    root = tmp_path / "vault-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        artifact_store, "artifact_root", lambda vault_id, settings_obj=None: root
    )

    asset = _asset()
    payloads = {asset.asset_id: b"abc"}

    def _publish_boom(_conn, **_kw):
        raise RuntimeError("publish boom")

    monkeypatch.setattr(
        "app.services.document_processor.artifact_store.publish_generation",
        _publish_boom,
    )

    proc_like = object.__new__(DocumentProcessor)
    proc_like.pool = _FakePool(db)
    try:
        DocumentProcessor._publish_artifacts(
            proc_like,
            file_id=1,
            vault_id=1,
            generation_hash="genA",
            parsed=_parsed_doc(asset, payloads),
        )
    except Exception:
        # Base behavior for publish_reached=True is a non-raise; a post-fix
        # re-raise is acceptable as long as the durable marker exists.
        pass

    marker_count = db.execute(
        "SELECT COUNT(*) FROM ingestion_stage_states WHERE file_id = 1"
        " AND generation_hash = 'genA' AND stage = 'publish'"
        " AND status = 'failed_retryable'"
    ).fetchone()[0]
    db.close()
    assert marker_count == 1


# ── AC7: _contextualize_single_chunk docstring must match the failure flag ──


def test_contextualize_docstring_matches_failure_flag():
    doc = inspect.getdoc(ContextualChunker._contextualize_single_chunk) or ""
    stale = "Always sets contextualized=True" in doc
    assert int(stale) == 0


# ── AC8: finalize status + parsed_text must commit together ─────────────────


class FinalizeStatusParsedTextAtomicityTest(unittest.TestCase):
    """A kill between the two finalize sessions must not leave a torn row.

    ``_finalize_indexed_success`` commits status='indexed' in one
    ``_write_session`` and writes parsed_text in a second one whose failure
    is swallowed. Wrapping ``_write_session`` so entry #2 simulates a process
    kill after the status commit reproduces the tear; the row must never end
    up (indexed, stale parsed_text).
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="b15-ac8-")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (7, 'V', '')"
        )
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, parsed_text, chunk_count)"
            " VALUES (1, 7, 'x', 'x.txt', 1, 'processing', 'old', 0)"
        )
        conn.commit()
        conn.close()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        self.processor = DocumentProcessor(
            chunk_size_chars=500, chunk_overlap_chars=0, pool=self.pool
        )

    def tearDown(self):
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_finalize_status_and_parsed_text_commit_together(self):
        chunk = ProcessedChunk(text="body", metadata={}, chunk_index=0)
        real_write_session = DocumentProcessor._write_session.__get__(self.processor)
        entries = {"n": 0}

        @asynccontextmanager
        async def kill_after_status_commit():
            entries["n"] += 1
            if entries["n"] == 2:
                # Simulated kill AFTER the first session committed status:
                # the acquisition itself fails exactly like the real second
                # session's failure would surface to the caller's except.
                raise sqlite3.OperationalError("simulated kill after status commit")
            async with real_write_session() as conn:
                yield conn

        self.processor._write_session = kill_after_status_commit

        try:
            asyncio.run(
                self.processor._finalize_indexed_success(
                    file_id=1,
                    vault_id=7,
                    chunks=[chunk],
                    document_text="new",
                    chunks_failed_count=0,
                )
            )
        except Exception:
            # Post-fix the kill may surface (rolled back) instead of being
            # swallowed; both are acceptable, only the row state is pinned.
            pass

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, parsed_text FROM files WHERE id = 1"
        ).fetchone()
        conn.close()
        torn = (row["status"] == "indexed") and (row["parsed_text"] != "new")
        assert int(torn) == 0


# ── AC9: committing writers must check out through _write_session ───────────

_RAW_CHECKOUT_ATTRS = {
    "connection",
    "connection_async",
    "get_connection",
    "get_connection_async",
}


def _is_raw_pool_checkout(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _RAW_CHECKOUT_ATTRS):
        return False
    value = func.value
    if (
        isinstance(value, ast.Attribute)
        and value.attr == "pool"
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
    ):
        return True
    return isinstance(value, ast.Name) and value.id == "pool"


def test_committing_writers_use_write_permit():
    backend_root = Path(__file__).resolve().parents[1]
    targets = [
        backend_root / "app" / "services" / "document_processor.py",
        backend_root / "app" / "services" / "document_progress.py",
    ]
    total = 0
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "_write_session":
                continue
            commits = any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "commit"
                for sub in ast.walk(node)
            )
            if not commits:
                continue
            total += sum(1 for sub in ast.walk(node) if _is_raw_pool_checkout(sub))
    assert total == 0


# ── AC10: _insert_or_get_file_record must retry transient sqlite errors ──────


class _ProbeCursor:
    def fetchone(self):
        return None

    @property
    def lastrowid(self):
        return 4242


class _LockedOnceProxy:
    """Connection proxy whose execute() fails exactly once, sqlite-style."""

    def __init__(self):
        self.execute_calls = 0

    def execute(self, *_args, **_kwargs):
        self.execute_calls += 1
        if self.execute_calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return _ProbeCursor()

    def rollback(self):
        pass

    def commit(self):
        pass


class InsertOrGetFileRecordRetryTest(unittest.TestCase):
    def test_insert_or_get_file_record_retries_transient_sqlite_error(self):
        # Post-CHECK_WRONG-amend shape: a retry-then-succeed run must RETURN the
        # row id (the original assertRaises wrapper could never go GREEN under
        # a correct implementation where attempt 2 succeeds; the issue's AC10
        # spec asks for attempts >= 2 and "retry and return the row id").
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(b"probe")
            probe_path = tmp_file.name
        try:
            proxy = _LockedOnceProxy()
            processor = DocumentProcessor.__new__(DocumentProcessor)
            result = None
            try:
                result = processor._insert_or_get_file_record(
                    probe_path, "deadbeef", proxy, vault_id=1
                )
            except DocumentProcessingError:
                # Base behavior: the interior except sqlite3.Error converts the
                # transient OperationalError before the decorator can retry.
                result = None
        finally:
            os.unlink(probe_path)
        assert proxy.execute_calls >= 2
        assert result == 4242


if __name__ == "__main__":
    unittest.main()
