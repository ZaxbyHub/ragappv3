"""Issue #715 acceptance check — audit finding T1-32 (visibility filter).

``test_indexed_lookup_failure_warns``

Contract under test: when the SQLite lookup backing the atomic visibility
filter (``_get_indexed_file_ids``) fails with an operational DB error, the
eval retrieval path must emit a WARNING that names ``indexed_file_ids`` —
the filter silently degrading to "no filtering" is an observability-relevant
event (partial-ingest files may become visible to the LLM).

Root cause verified at HEAD (665bbd09): ``_get_indexed_file_ids``
(rag_engine.py ~L4010-4040) catches EVERY exception from ``_get_pool()`` at
DEBUG level and returns ``None``, so the WARNING branches around the
``asyncio.to_thread(self._get_indexed_file_ids, ...)`` call sites
(rag_engine.py ~L1859 / ~L4293 / ~L4371) are unreachable — an operational
DB failure produces zero WARNING records.

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    AssertionError: assert 0 >= 1

Harness reuse: mirrors the fake store/embedder pattern of the
``retrieve_eval_results`` tests in ``tests/test_rag_engine.py`` (and the
SSRF autouse fixture of ``tests/test_c08_embedding_admission.py``).
"""

import logging
import os
import sqlite3
import sys
from typing import List
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse the fakes from the existing RAG-engine unit tests.
from test_rag_engine import (  # noqa: E402
    FakeEmbeddingService,
    FakeLLMClient,
    FakeMemoryStore,
    FakeVectorStore,
)

from app.config import settings  # noqa: E402
from app.services.rag_engine import RAGEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests
    (same fixture as tests/test_rag_engine.py)."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


def _make_engine() -> RAGEngine:
    vector_results: List[dict] = [
        {
            "text": "chunk one",
            "file_id": "file1",
            "metadata": {"source_file": "doc.md"},
            "_distance": 0.2,
        },
    ]
    return RAGEngine(
        embedding_service=FakeEmbeddingService([0.1, 0.2]),
        vector_store=FakeVectorStore(vector_results),
        memory_store=FakeMemoryStore(),
        llm_client=FakeLLMClient(response="answer"),
    )


async def test_indexed_lookup_failure_warns(caplog):
    """A failed indexed_file_ids lookup must WARN naming 'indexed_file_ids'
    on the eval retrieval path — the DEBUG swallow makes the WARNING branches
    dead code today. T1-32 visibility-filter acceptance check (issue #715)."""
    engine = _make_engine()

    with patch(
        "app.services.rag_engine._get_pool",
        side_effect=sqlite3.OperationalError("database is locked"),
    ), patch.object(settings, "query_transformation_enabled", False):
        caplog.set_level(logging.WARNING, logger="app.services.rag_engine")
        await engine.retrieve_eval_results("test query", vault_id=1)

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "indexed_file_ids" in record.getMessage()
    ]
    assert len(warnings) >= 1, (
        "an operational failure of the indexed_file_ids lookup must emit at "
        "least one WARNING naming 'indexed_file_ids' (visibility filter "
        f"degraded silently); records={caplog.records!r}"
    )
