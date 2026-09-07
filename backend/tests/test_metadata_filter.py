"""MetadataFilter resolution and engine wiring (issue #510 AC-16).

Covers the typed filter model (extra=forbid), vault-scoped translation into a
LanceDB ``file_id IN (...)`` expression against a REAL temp SQLite DB
(files/tags/document_tags), the zero-match sentinel (a filter is never
silently dropped), failure fallbacks, and the engine-level wiring that lands
the resolved expression on ``vector_store.search``.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.api.routes.chat import ChatRequest
from app.models.database import SQLiteConnectionPool
from app.services.metadata_filter import (
    ZERO_MATCH_FILTER_EXPR,
    MetadataFilter,
    resolve_metadata_filter,
)

_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_name TEXT,
    email_sender TEXT,
    document_date TEXT
);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(vault_id, name)
);
CREATE TABLE document_tags (
    file_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (file_id, tag_id)
);
"""

_SEED = """
INSERT INTO files (id, vault_id, file_name, email_sender, document_date) VALUES
    (7,  1, 'q1-report.pdf',  'alice@corp.example', '2024-03-10'),
    (9,  1, 'q2-report.pdf',  NULL,                 '2024-06-01'),
    (11, 1, 'old-2023.pdf',   NULL,                 '2023-12-31'),
    (13, 1, 'undated.pdf',    NULL,                 NULL),
    (21, 2, 'other-vault.pdf','bob@corp.example',   '2024-04-01');
INSERT INTO tags (id, vault_id, name) VALUES
    (1, 1, 'finance'),
    (2, 1, 'legal'),
    (3, 2, 'finance');
INSERT INTO document_tags (file_id, tag_id) VALUES
    (7, 1), (9, 1), (7, 2), (21, 3);
"""


@pytest.fixture
def real_pool(tmp_path):
    """A real SQLiteConnectionPool over a temp DB with minimal metadata schema."""
    db_path = str(Path(tmp_path) / "meta.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.executescript(_SCHEMA)
        conn.executescript(_SEED)
        conn.commit()
    finally:
        conn.close()
    pool = SQLiteConnectionPool(db_path)
    yield pool
    pool.close_all()


@pytest.fixture
def pool_patched(monkeypatch, real_pool):
    """Point app.models.database.get_pool (imported inside _resolve_file_ids)
    at the temp-DB pool."""
    monkeypatch.setattr("app.models.database.get_pool", lambda *a, **kw: real_pool)
    return real_pool


class TestResolveMetadataFilterTranslation:
    def test_date_range_resolves_document_date_files(self, pool_patched):
        expr = resolve_metadata_filter(
            {"date_from": "2024-01-01", "date_to": "2024-12-31"}, vault_id=1
        )
        # In-range vault-1 files only; undated file 13 excluded (unknown date
        # cannot satisfy a range), 2023 file 11 out of range.
        assert expr == "file_id IN ('7', '9')"

    def test_tags_resolve_via_vault_scoped_join(self, pool_patched):
        expr = resolve_metadata_filter({"tags": ["finance"]}, vault_id=1)
        # Only vault-1 files tagged finance (vault-2 file 21 is excluded).
        assert expr == "file_id IN ('7', '9')"

    def test_author_resolves_email_sender(self, pool_patched):
        expr = resolve_metadata_filter({"author": "alice@corp.example"}, vault_id=1)
        assert expr == "file_id IN ('7')"

    def test_combined_tags_and_date(self, pool_patched):
        expr = resolve_metadata_filter(
            {"tags": ["finance"], "date_from": "2024-06-01"}, vault_id=1
        )
        assert expr == "file_id IN ('9')"

    def test_accepts_model_instance(self, pool_patched):
        expr = resolve_metadata_filter(
            MetadataFilter(author="bob@corp.example"), vault_id=2
        )
        assert expr == "file_id IN ('21')"

    def test_vault_scoping_excludes_other_vaults_files(self, pool_patched):
        """A file matching the filter in ANOTHER vault must never resolve."""
        expr = resolve_metadata_filter(
            {"author": "bob@corp.example"}, vault_id=1  # bob's file is vault 2
        )
        assert expr == ZERO_MATCH_FILTER_EXPR


class TestResolveMetadataFilterEdgeCases:
    def test_none_payload_returns_none(self, pool_patched):
        assert resolve_metadata_filter(None, vault_id=1) is None

    def test_all_none_fields_return_none(self, pool_patched):
        assert resolve_metadata_filter({}, vault_id=1) is None
        assert resolve_metadata_filter(
            MetadataFilter(), vault_id=1
        ) is None

    def test_zero_match_yields_sentinel_not_none(self, pool_patched):
        expr = resolve_metadata_filter({"author": "nobody@nowhere"}, vault_id=1)
        assert expr is not None, "A user filter must never be silently dropped"
        assert expr == ZERO_MATCH_FILTER_EXPR == "file_id IN ('')"

    def test_resolution_failure_yields_sentinel_not_none(self, monkeypatch):
        """If the metadata DB is unavailable the sentinel still applies — the
        filter is not silently ignored."""
        def _raise(*a, **kw):
            raise RuntimeError("metadata db unavailable")

        monkeypatch.setattr("app.models.database.get_pool", _raise)
        expr = resolve_metadata_filter({"tags": ["finance"]}, vault_id=1)
        assert expr is not None
        assert expr == ZERO_MATCH_FILTER_EXPR


class TestMetadataFilterModelValidation:
    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            MetadataFilter(favorite_color=1)

    def test_bad_date_rejected(self):
        with pytest.raises(ValidationError):
            MetadataFilter(date_from="not-a-date")

    def test_valid_fields_accepted(self):
        parsed = MetadataFilter(
            date_from="2024-01-01",
            date_to="2024-12-31",
            tags=["finance"],
            author="alice@corp.example",
        )
        assert parsed.date_from.isoformat() == "2024-01-01"
        assert parsed.tags == ["finance"]

    def test_chat_request_rejects_unknown_filter_field(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="hi", metadata_filter={"favorite_color": 1})

    def test_chat_request_accepts_valid_filter(self):
        req = ChatRequest(message="hi", metadata_filter={"tags": ["finance"]})
        assert isinstance(req.metadata_filter, MetadataFilter)
        assert req.metadata_filter.tags == ["finance"]


class TestEngineLevelFilterWiring:
    """query(metadata_filter=...) must land a truthy filter_expr on every
    vector_store.search call — even when resolution fails (sentinel)."""

    @pytest.mark.asyncio
    async def test_engine_forwards_resolved_filter_expr_to_search(self, monkeypatch):
        from app.services.rag_engine import RAGEngine

        class _Store:
            def __init__(self):
                self.filter_exprs = []

            async def search(self, embedding, limit, vault_id=None, query_text="",
                             hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
                self.filter_exprs.append(filter_expr)
                return [{
                    "id": "7_0", "file_id": "7",
                    "text": "Paris is the capital of France.",
                    "_distance": 0.1, "metadata": {},
                }]

            def get_fts_exceptions(self):
                return 0

        def _raise(*a, **kw):
            raise RuntimeError("metadata db unavailable")

        monkeypatch.setattr("app.models.database.get_pool", _raise)

        store = _Store()

        class _Embed:
            async def embed_single(self, text):
                return [0.1, 0.2, 0.3]

            async def embed_passage(self, text):
                return [0.1, 0.2, 0.3]

        class _Memory:
            def detect_memory_intent(self, text):
                return None

        class _LLM:
            base_url = "stub"
            model = "stub"

            def __init__(self):
                self.last_metrics = {}

            async def chat_completion(self, messages, **kw):
                return "The capital of France is Paris, a famous city."

            async def chat_completion_stream(self, messages, **kw):
                yield "The capital of France is Paris, a famous city."

        engine = RAGEngine(
            embedding_service=_Embed(),
            vector_store=store,
            memory_store=_Memory(),
            llm_client=_LLM(),
            reranking_service=None,
        )

        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            mock_settings.agentic_rag_enabled = False
            mock_settings.default_chat_mode = "thinking"
            mock_settings.query_transformation_enabled = False
            mock_settings.memory_retrieval_enabled = False
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.context_distillation_enabled = False
            mock_settings.context_distillation_synthesis_enabled = False
            mock_settings.context_max_tokens = 0
            mock_settings.parent_retrieval_enabled = False
            mock_settings.retrieval_recency_weight = 0.0
            mock_settings.rrf_legacy_mode = False
            mock_settings.exact_match_promote = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.hybrid_alpha = 0.6
            mock_settings.maintenance_mode = False
            mock_settings.max_distance_threshold = 1.0
            mock_settings.rag_relevance_threshold = 0.5
            mock_settings.retrieval_top_k = 10
            mock_settings.retrieval_window = 0
            mock_settings.initial_retrieval_top_k = 10
            mock_settings.reranker_top_n = 5
            mock_settings.thinking_max_tokens = 1024
            mock_settings.rag_trace_in_response = False
            mock_settings.kms_enabled = False

            done = None
            async for chunk in engine.query(
                "what is the capital",
                [],
                stream=False,
                vault_id=1,
                metadata_filter={"author": "alice@corp.example"},
            ):
                if chunk.get("type") == "done":
                    done = chunk

        assert done is not None
        assert store.filter_exprs, "vector_store.search was never called"
        assert all(
            expr for expr in store.filter_exprs
        ), "resolution failure must still apply the truthy zero-match sentinel"
        assert ZERO_MATCH_FILTER_EXPR in store.filter_exprs
