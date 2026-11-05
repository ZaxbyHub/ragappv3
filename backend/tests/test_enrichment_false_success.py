"""Enrichment false-success contracts (issue #1066).

Three swallow points in the post-index enrichment pipeline collapse real
generation failures into a stored ``enrichment_status='complete'`` with NULL
``enrichment_error``:

* ``ChunkEnrichmentService._generate_enrichment`` catches
  JSONDecodeError/KeyError/TypeError, logs, and returns an enrichment object
  with empty fields;
* ``ChunkEnrichmentService._enrich_single`` catches ``(LLMError, Exception)``
  and returns an empty ``ChunkEnrichment``;
* ``LLMClient.chat_completion`` returns only the content string —
  ``finish_reason`` (e.g. "length", the 512-token cap cutting the output) is
  stored only on the per-client ``last_metrics`` dict, which the enrichment
  caller never reads.

When every candidate enrichment comes back empty,
``DocumentProcessor.run_enrichment_job`` marks the file 'complete'; a mixed
batch swaps the successful siblings and ALSO marks the file 'complete'.

These specs drive the REAL ``DocumentProcessor.run_enrichment_job`` against a
real temp sqlite DB and a REAL ``ChunkEnrichmentService``, with only the LLM
transport faked, and pin the contract that unparsed output, token-capped
output, a mixed batch, and a provider-down call are stored as
``enrichment_status='error'`` with a non-empty ``enrichment_error`` — never
'complete'. RED at base, GREEN after the fix.
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.models import database as _database_module
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.chunk_enrichment import ChunkEnrichmentService
from app.services.chunking import ProcessedChunk
from app.services.document_processor import DocumentProcessor
from app.services.llm_client import LLMError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Protocol kwargs chat_completion receives today that are NOT per-call
# metrics sinks; every other dict-like kwarg a future change passes is
# treated as one (e.g. a ``metrics_sink``) and receives the finish_reason.
_NON_SINK_KWARGS = {"response_format"}


class FakeLLMClient:
    """LLM transport fake faithful to LLMClient's observable surface.

    The REAL client's ``chat_completion`` returns only the content string;
    ``finish_reason`` is observable solely through ``self.last_metrics``
    (llm_client.py sets ``self.last_metrics["finish_reason"]`` per call).
    This fake mirrors that contract exactly: it returns the scripted content
    string, records the scripted metrics on ``self.last_metrics`` after each
    call, and — should a future caller pass a per-call dict-like metrics kwarg
    — writes the same finish_reason into that kwarg too.

    ``calls`` is a list of ``(kind, finish_reason)`` tuples, one per accepted
    call, where kind is "content" or "raise".
    """

    def __init__(self, call_specs):
        # Each spec: {"content": str, "finish_reason": "stop"|"length"|...}
        # or {"raise": <exception instance>}.
        self._call_specs = list(call_specs)
        self.calls = []
        self.metrics_history = []
        self.last_metrics = {}

    async def chat_completion(self, messages, **kwargs):
        if not self._call_specs:
            raise AssertionError(
                "FakeLLMClient received an unexpected extra chat_completion "
                f"call (messages={messages!r})"
            )
        spec = self._call_specs.pop(0)

        if "raise" in spec:
            self.last_metrics = {
                "provider_url": "fake",
                "model": "fake",
                "status": "request_error",
            }
            self.metrics_history.append(dict(self.last_metrics))
            self.calls.append(("raise", None))
            raise spec["raise"]

        content = spec["content"]
        finish_reason = spec.get("finish_reason", "stop")
        # The real client's observable per-call contract (llm_client.py
        # ~726-738): last_metrics carries the finish_reason; the return value
        # is the content string alone.
        self.last_metrics = {
            "provider_url": "fake",
            "model": "fake",
            "finish_reason": finish_reason,
            "status": "ok",
        }
        self.metrics_history.append(dict(self.last_metrics))
        self.calls.append(("content", finish_reason))
        # Tolerate any additional per-call metrics kwarg a future change
        # might pass: write the same finish_reason into it.
        for name, value in kwargs.items():
            if name in _NON_SINK_KWARGS:
                continue
            if isinstance(value, dict):
                value["finish_reason"] = finish_reason
        return content


def _make_embedding_service():
    """Working embedding service double with REAL scalar attributes.

    ``DocumentProcessor._validate_chunk_sizes`` and
    ``_embedding_cache_identity`` read these attributes numerically/strlen'd,
    so a bare MagicMock (auto-fabricated attributes) raises TypeError. The
    embed_batch contract mirrors the real service's fail_fast=False shape:
    (vectors, failed_batch_indices).
    """
    service = MagicMock()
    service.MAX_TEXT_LENGTH = 8192
    service.embedding_doc_prefix = ""
    service.embedding_model = "fake-embed-model"
    service.provider_mode = "fake"
    service.embedding_dim = 4
    service.embed_batch = AsyncMock(
        side_effect=lambda texts, fail_fast=False: (
            [[0.1, 0.2, 0.3, 0.4] for _ in texts],
            [],
        )
    )
    return service


def _chunk(chunk_index: int, text: str, total: int) -> ProcessedChunk:
    """A canonical default-scale enrichment candidate with non-blank text."""
    return ProcessedChunk(
        text=text,
        metadata={"chunk_scale": "default", "total_chunks": total},
        chunk_index=chunk_index,
    )


# ---------------------------------------------------------------------------
# Harness: real sqlite DB + real DocumentProcessor + real ChunkEnrichmentService
# ---------------------------------------------------------------------------


class _EnrichmentJobHarness:
    """One real-DB enrichment run context (mirrors test_document_processor.py)."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp_path = Path(tmp_path)
        # Name the DB file so settings.sqlite_path (data_dir / "app.db")
        # resolves to THIS database: the enrichment enablement lookup goes
        # through the app-global pool on settings.sqlite_path
        # (document_processor.py is_enrichment_enabled_for_file/_vault) and
        # the vault/file override columns are NULL, inheriting the GLOBAL
        # setting.
        self.db_path = self.tmp_path / "app.db"
        init_db(str(self.db_path))
        run_migrations(str(self.db_path))
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) "
            "VALUES (1, 'Default', '')"
        )
        conn.commit()
        conn.close()

        self.source_path = self.tmp_path / "source.txt"
        self.source_path.write_text("source document body", encoding="utf-8")

        # The enablement lookup falls back to the GLOBAL setting when the
        # vault/file override columns are NULL, so force it on around each
        # run (restored automatically by monkeypatch).
        monkeypatch.setattr(settings, "data_dir", self.tmp_path)
        monkeypatch.setattr(settings, "chunk_enrichment_enabled", True)

        self.pool = SQLiteConnectionPool(str(self.db_path), max_size=2)
        self.processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        # The swap tail then reaches the terminal status write:
        # _swap_in_enriched_vector_records returns early and
        # _verify_vector_rows_visible returns early.
        self.processor.vector_store = None
        self.processor.embedding_service = _make_embedding_service()

    def insert_indexed_file(self, file_id: int, file_hash: str) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            INSERT INTO files
                (id, vault_id, file_path, file_name, file_hash, file_size,
                 status, chunk_count)
            VALUES (?, 1, ?, 'source.txt', ?, 1, 'indexed', 1)
            """,
            (file_id, str(self.source_path), file_hash),
        )
        conn.commit()
        conn.close()

    async def run_job(self, *, file_id, file_hash, chunks, document_text, client):
        """Run the REAL enrichment pipeline with only the LLM transport faked."""
        self.processor._llm_client = client
        self.processor._chunk_enrichment_service = ChunkEnrichmentService(
            llm_client=client, concurrency=2
        )
        await self.processor.run_enrichment_job(
            file_id=file_id,
            file_path=str(self.source_path),
            vault_id=1,
            file_hash=file_hash,
            chunks=chunks,
            document_text=document_text,
        )

    def enrichment_row(self, file_id: int) -> sqlite3.Row:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT status, enrichment_status, enrichment_error "
                "FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
        finally:
            conn.close()

    def close(self) -> None:
        # Best-effort cleanup; the app-global pool registry may hold a second
        # pool open on the same temp file (Windows file locking).
        try:
            self.pool.close_all()
        except Exception:
            pass
        try:
            with _database_module._pool_cache_lock:
                global_pool = _database_module._pool_cache.pop(
                    str(self.db_path), None
                )
            if global_pool is not None:
                global_pool.close_all()
        except Exception:
            pass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = _EnrichmentJobHarness(tmp_path, monkeypatch)
    yield h
    h.close()


def _assert_stored_as_error(row: sqlite3.Row, context: str) -> None:
    """The failure contract: 'error' + non-empty message, never 'complete'."""
    assert row["enrichment_status"] != "complete", (
        f"{context}: enrichment_status must never be 'complete' for a "
        f"failed enrichment generation (row={dict(row)})"
    )
    assert row["enrichment_status"] == "error", (
        f"{context}: expected enrichment_status='error', got "
        f"{row['enrichment_status']!r} (enrichment_error="
        f"{row['enrichment_error']!r})"
    )
    assert isinstance(row["enrichment_error"], str) and row[
        "enrichment_error"
    ].strip(), (
        f"{context}: enrichment_error must be a non-empty string, got "
        f"{row['enrichment_error']!r}"
    )
    # The document itself stays indexed; only the optional job failed.
    assert row["status"] == "indexed"


# ---------------------------------------------------------------------------
# Frozen contract nodes (issue #1066)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparsed_or_length_enrichment_is_not_stored_complete(harness):
    """Unparsed or token-capped enrichment output is stored as an error.

    Covers both arms of the OR plus a mixed batch, each through the REAL
    DocumentProcessor.run_enrichment_job / ChunkEnrichmentService against a
    real sqlite DB:

    * arm 1 — parse failure: the model returns unterminated JSON, so
      ``_generate_enrichment`` swallows JSONDecodeError and yields an empty
      enrichment;
    * arm 2 — token-capped output: the model returns a CLOSED valid JSON
      object but the call finished because of the token cap; the fake client
      exposes that exactly the way the real client does today
      (``last_metrics["finish_reason"] == "length"``, content string
      returned);
    * arm 3 — mixed batch: one candidate returns a closed object with
      finish "stop", a sibling returns truncated JSON.

    For every arm the files row ends ``enrichment_status='error'`` with a
    non-empty ``enrichment_error``, never 'complete'. RED at base (every arm
    stores 'complete' with NULL enrichment_error), GREEN after the fix.
    """
    arm_rows = []

    # ---- arm 1: unterminated JSON (parse failure), finish "stop" ----
    harness.insert_indexed_file(file_id=101, file_hash="parsefail0000")
    client_parse = FakeLLMClient(
        [
            {
                "content": '{"summary": "truncated at the token cap mid-str',
                "finish_reason": "stop",
            }
        ]
    )
    await harness.run_job(
        file_id=101,
        file_hash="parsefail0000",
        chunks=[_chunk(0, "Alpha chunk body with real content.", 1)],
        document_text="Alpha chunk body with real content.",
        client=client_parse,
    )
    # The LLM was genuinely called — this is the parse-failure arm, not a
    # no-op path.
    assert len(client_parse.calls) == 1
    arm_rows.append(
        ("unterminated-JSON enrichment", harness.enrichment_row(101))
    )

    # ---- arm 2: closed valid JSON, but finish_reason "length" ----
    harness.insert_indexed_file(file_id=102, file_hash="lengthcap0000")
    client_length = FakeLLMClient(
        [
            {
                "content": (
                    '{"summary": "Closed summary within the returned '
                    'payload.", "questions": ["What does this chunk '
                    'cover?"], "entities": ["LanceDB", "IvfPq"]}'
                ),
                "finish_reason": "length",
            }
        ]
    )
    await harness.run_job(
        file_id=102,
        file_hash="lengthcap0000",
        chunks=[_chunk(0, "Beta chunk body with real content.", 1)],
        document_text="Beta chunk body with real content.",
        client=client_length,
    )
    assert len(client_length.calls) == 1
    # The fake exposed the cap the way the real client does: the observable
    # per-call metrics say the generation was cut by the token limit.
    assert client_length.last_metrics.get("finish_reason") == "length"
    arm_rows.append(("length-capped enrichment", harness.enrichment_row(102)))

    # ---- arm 3: mixed batch (one closed + "stop", one truncated JSON) ----
    harness.insert_indexed_file(file_id=103, file_hash="mixedbatch000")
    client_mixed = FakeLLMClient(
        [
            {
                "content": (
                    '{"summary": "Good sibling summary.", '
                    '"questions": ["Q1"], "entities": ["E1"]}'
                ),
                "finish_reason": "stop",
            },
            {
                "content": '{"summary": "sibling output cut off mid-obj',
                "finish_reason": "stop",
            },
        ]
    )
    await harness.run_job(
        file_id=103,
        file_hash="mixedbatch000",
        chunks=[
            _chunk(0, "Gamma chunk body with real content.", 2),
            _chunk(1, "Delta chunk body with real content.", 2),
        ],
        document_text="Gamma and Delta chunk bodies.",
        client=client_mixed,
    )
    assert len(client_mixed.calls) == 2
    arm_rows.append(("mixed-batch enrichment", harness.enrichment_row(103)))

    # Every arm ran; every arm must be stored as an enrichment error.
    observed_statuses = {
        context: row["enrichment_status"] for context, row in arm_rows
    }
    expected_statuses = {context: "error" for context in observed_statuses}
    assert observed_statuses == expected_statuses, (
        "failed enrichment generations must be stored as "
        f"enrichment_status='error', never 'complete'; observed rows: "
        f"{ {context: dict(row) for context, row in arm_rows} }"
    )
    for context, row in arm_rows:
        _assert_stored_as_error(row, context)


@pytest.mark.asyncio
async def test_enrichment_call_exception_is_not_stored_complete(harness):
    """A provider-down LLM call is stored as an error, never as success.

    The fake client raises ``LLMError("provider down")`` on the call;
    ``_enrich_single`` currently swallows it into an empty enrichment, so the
    job stores 'complete'. The contract: the files row ends
    ``enrichment_status='error'`` with a non-empty ``enrichment_error``.
    RED at base (stores 'complete' with NULL enrichment_error), GREEN after
    the fix.
    """
    harness.insert_indexed_file(file_id=201, file_hash="providerdown00")
    client = FakeLLMClient([{"raise": LLMError("provider down")}])

    await harness.run_job(
        file_id=201,
        file_hash="providerdown00",
        chunks=[_chunk(0, "Epsilon chunk body with real content.", 1)],
        document_text="Epsilon chunk body with real content.",
        client=client,
    )
    assert len(client.calls) == 1
    assert client.calls[0][0] == "raise"
    _assert_stored_as_error(
        harness.enrichment_row(201), "provider-down enrichment call"
    )


# ---------------------------------------------------------------------------
# Supporting leg (not frozen): the parsed-empty no-op stays a success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parsed_empty_enrichment_object_stays_complete(harness):
    """A cleanly parsed, all-empty enrichment object is a no-op, not a failure.

    Pins the boundary of the contract: a CLOSED valid JSON object whose
    fields are all empty, delivered with finish_reason "stop", means the
    curator deliberately produced nothing to store — the optional job
    completes with NULL enrichment_error and no re-embed happens. Holds at
    base and after the fix.
    """
    harness.insert_indexed_file(file_id=301, file_hash="parsedempty00")
    client = FakeLLMClient(
        [
            {
                "content": '{"summary": "", "questions": [], "entities": []}',
                "finish_reason": "stop",
            }
        ]
    )

    await harness.run_job(
        file_id=301,
        file_hash="parsedempty00",
        chunks=[_chunk(0, "Zeta chunk body with real content.", 1)],
        document_text="Zeta chunk body with real content.",
        client=client,
    )
    assert len(client.calls) == 1
    # The parsed-empty arm must not reach the embedding path.
    harness.processor.embedding_service.embed_batch.assert_not_awaited()

    row = harness.enrichment_row(301)
    assert row["enrichment_status"] == "complete"
    assert row["enrichment_error"] is None
    assert row["status"] == "indexed"
