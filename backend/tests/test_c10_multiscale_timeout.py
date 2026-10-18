"""Issue #715 acceptance check — audit finding T1-32 (multi-scale timeout).

``test_scale_timeout_propagates``

Contract under test: a ``SearchSemaphoreTimeoutError`` raised by one scale's
search inside the multi-scale fan-out must PROPAGATE out of
``VectorStore.search`` (a search-capacity / server-busy signal maps to HTTP
503 upstream), instead of being silently converted into a logged warning and
an empty per-scale result list while the surviving scales are fused into a
seemingly healthy answer.

Root cause verified at HEAD (665bbd09): ``VectorStore.search`` gathers the
per-scale searches with ``return_exceptions=True`` (vector_store.py
~L1759-1776) and converts ANY exception — including the capacity-timeout
signal raised by ``_acquire_search_semaphore`` / ``SearchSemaphoreTimeoutError``
(class at vector_store.py L129) — into ``logger.warning(...)`` plus an empty
per-scale list, then fuses the survivors. The timeout is indistinguishable
from a soft per-scale failure.

This check asserts the DESIRED post-fix behavior and is RED at HEAD with::

    Failed: DID NOT RAISE

Harness reuse: mirrors the store setup of
``tests/test_multiscale_search.py::TestMultiScaleSearchSemaphore::
test_all_scales_fail_returns_empty_list`` (mocked db/table, replaced
``_search_single_scale``, patched multi-scale settings) — except here one
scale survives and one raises the capacity timeout.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.vector_store import (  # noqa: E402
    SearchSemaphoreTimeoutError,
    VectorStore,
)

_EMBEDDING_DIM = 384


def _make_store(tmp_dir: str) -> VectorStore:
    """Build a VectorStore the same way the multi-scale semaphore tests do."""
    store = VectorStore(db_path=Path(tmp_dir) / "test_lancedb")
    store.table = MagicMock()
    store.table.list_indices = AsyncMock(return_value=[])
    store.table.count_rows = AsyncMock(return_value=0)
    store._embedding_dim = _EMBEDDING_DIM
    store._search_semaphore = asyncio.Semaphore(4)
    return store


async def test_scale_timeout_propagates():
    """A SearchSemaphoreTimeoutError from one scale's search must propagate
    out of multi-scale VectorStore.search, not be fused away. T1-32
    multi-scale timeout acceptance check (issue #715)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        store = _make_store(temp_dir)

        async def fake_search_single_scale(
            embedding,
            scale,
            fetch_k,
            filter_expr=None,
            vault_id=None,
            query_text="",
            hybrid=True,
            hybrid_alpha=0.5,
            **kwargs,
        ):
            if scale == "1024":
                # Capacity signal: the semaphore acquisition timed out for
                # this scale's search (raised through the production
                # _sem_search_single_scale wrapper).
                raise SearchSemaphoreTimeoutError(
                    "Search semaphore acquisition timed out after 30.0s; "
                    f"concurrency=4 (simulated for scale {scale})"
                )
            return [
                {
                    "id": f"doc_{scale}",
                    "text": f"From {scale}",
                    "_rrf_score": 0.5,
                }
            ]

        store._search_single_scale = fake_search_single_scale

        # Mock db with table_names to indicate table exists (same as the
        # existing multi-scale semaphore tests).
        mock_db = MagicMock()
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        store.db = mock_db

        with patch("app.services.vector_store.settings") as mock_settings:
            mock_settings.multi_scale_indexing_enabled = True
            mock_settings.multi_scale_rrf_k = 60
            mock_settings.multi_scale_chunk_sizes = "512,1024"
            mock_settings.search_semaphore_timeout_seconds = 30.0

            # DESIRED: the capacity timeout is a server-busy signal for the
            # whole search — it must escape search(), not be swallowed into
            # a warning + fused survivor results.
            with pytest.raises(SearchSemaphoreTimeoutError):
                await store.search(
                    embedding=[0.0] * _EMBEDDING_DIM,
                    limit=10,
                )
