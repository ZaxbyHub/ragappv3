"""C13 acceptance checks — POST /api/search read path contracts.

AC4: WHEN ``POST /api/search`` is served while the vector store's table is
already open THE SYSTEM SHALL make zero ``init_table`` calls. At base the
handler unconditionally re-runs ``await vector_store.init_table(dim)``
(search.py, POST /search body) — RED at base, GREEN after the fix.

AC5: WHEN ``init_table`` is called again on an already-open store that has an
IVF-PQ index and a stale freshness baseline (``_last_index_build_row_count``
100, ``count_rows()`` 500) THE SYSTEM SHALL leave it at 100. At base
``_init_table_unlocked`` re-seeds the baseline from ``count_rows()`` — RED at
base, GREEN after the fix.

AC7: WHEN ``POST /api/search`` returns two records whose ``_distance`` values
are 0.1 (closest) and 0.9 THE SYSTEM SHALL return a self-consistent score
contract: either every result has ``score_type == "distance"``, or the closest
has the higher ``score``. At base ``score`` is the raw ``_distance`` (lower ==
closer, unlabelled) so the contract is unsatisfiable for clients — RED at
base, GREEN after the fix.

The route-level tests (AC4/AC7) mirror the fake-store / fake-embedder
dependency-override pattern of
backend/tests/test_search_route_semaphore_sanitization_adversarial.py.
"""

import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure test env so imported modules don't bail on missing env vars
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key-for-tests")
os.environ.setdefault("USERS_ENABLED", "False")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402


class _FakeEmbeddingService:
    """Fake embedding service that returns a fixed-size embedding vector."""

    def __init__(self, dim: int = 384):
        self.dim = dim

    async def embed_single(self, text: str):
        """Return a deterministic fake embedding."""
        import hashlib

        h = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return [float((h >> (i % 64)) & 1) * 0.1 for i in range(self.dim)]


class _FakeVectorStore:
    """Fake vector store with an ALREADY-OPEN table that counts init_table calls.

    ``search`` returns the constructor-supplied records (dicts carrying the
    fields the POST /search serializer reads: id / text / file_id /
    chunk_index / metadata / _distance).
    """

    def __init__(self, records):
        self.records = records
        # Non-None table attribute = the store is already initialized/open.
        self.table = MagicMock(name="already_open_table")
        self.init_table_calls = 0

    async def init_table(self, embedding_dim: int) -> None:
        self.init_table_calls += 1

    async def search(self, embedding, limit: int, vault_id=None):
        return self.records


class TestSearchReadPath(unittest.IsolatedAsyncioTestCase):
    """C13 read-path acceptance checks (AC4 / AC5 / AC7)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @contextmanager
    def _app_with_overrides(self, records):
        """Build a test client whose vector store serves `records` unchanged."""
        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_embedding_service,
            get_evaluate_policy,
            get_vector_store,
        )
        from app.main import app as main_app

        fake_vs = _FakeVectorStore(records)
        fake_emb = _FakeEmbeddingService(dim=384)

        main_app.dependency_overrides[get_vector_store] = lambda: fake_vs
        main_app.dependency_overrides[get_embedding_service] = lambda: fake_emb
        main_app.dependency_overrides[get_db] = self._get_test_db
        main_app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
        }

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        main_app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy

        client = TestClient(main_app, raise_server_exceptions=False)
        try:
            yield client, fake_vs
        finally:
            for dep in (
                get_vector_store,
                get_embedding_service,
                get_db,
                get_current_active_user,
                get_evaluate_policy,
            ):
                main_app.dependency_overrides.pop(dep, None)

    def _get_test_db(self):
        """Yield a mock DB connection."""
        conn = MagicMock()
        conn.cursor.return_value = MagicMock()
        yield conn

    @staticmethod
    def _record(chunk_id: str, distance: float) -> dict:
        """One search record shaped like a LanceDB hit (dict form)."""
        return {
            "id": chunk_id,
            "text": f"text for {chunk_id}",
            "file_id": "7",
            "vault_id": "1",
            "chunk_index": 0,
            "metadata": {},
            "_distance": distance,
        }

    def test_search_does_not_init_table(self):
        """AC4: WHEN POST /api/search is served while the vector store's table
        is already open THE SYSTEM SHALL make zero init_table calls.
        RED at base (master), GREEN after the fix."""
        records = [self._record("7_0", 0.1), self._record("7_1", 0.9)]
        with self._app_with_overrides(records) as (client, store):
            resp = client.post(
                "/api/search",
                json={"query": "test query", "limit": 5},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            # The route read path must not re-initialize an already-open table.
            self.assertEqual(store.init_table_calls, 0)

    async def test_reinit_does_not_reseed_index_baseline(self):
        """AC5: WHEN init_table is called again on an already-open store that
        has an IVF-PQ index and a stale freshness baseline
        (_last_index_build_row_count 100, count_rows() 500) THE SYSTEM SHALL
        leave it at 100. RED at base (master), GREEN after the fix."""
        from app.services.vector_store import VectorStore

        store = VectorStore(db_path=Path(self.temp_dir) / "lance")

        # Mocked LanceDB connection + already-open table with an IVF-PQ index.
        mock_table = MagicMock(name="chunks_table")
        mock_table.count_rows = AsyncMock(return_value=500)
        mock_db = MagicMock(name="lancedb_async_connection")
        mock_db.table_names = AsyncMock(return_value=["chunks"])
        mock_db.open_table = AsyncMock(return_value=mock_table)
        store.db = mock_db
        store.table = mock_table

        # Stale in-memory freshness baseline from the last index build.
        store._last_index_build_row_count = 100
        store._last_index_build_generation = 7

        with patch(
            "app.services.vector_store.has_index", new=AsyncMock(return_value=True)
        ):
            await store.init_table(384)

        # Re-init on an already-open store must not reseed the baseline from
        # count_rows() — the pre-existing freshness bookkeeping stays intact.
        self.assertEqual(store._last_index_build_row_count, 100)

    def test_score_contract_matches_distance_semantics(self):
        """AC7: WHEN POST /api/search returns two records whose _distance
        values are 0.1 (closest) and 0.9 THE SYSTEM SHALL return a
        self-consistent score contract: either every result has
        score_type == "distance", or the closest has the higher score.
        RED at base (master), GREEN after the fix."""
        records = [
            self._record("close_1", 0.1),  # closest hit
            self._record("far_1", 0.9),  # farthest hit
        ]
        with self._app_with_overrides(records) as (client, _store):
            resp = client.post(
                "/api/search",
                json={"query": "test query", "limit": 5},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            results = resp.json()["results"]
            self.assertEqual(len(results), 2, results)

            by_id = {r["id"]: r for r in results}
            closest = by_id["close_1"]
            farthest = by_id["far_1"]

            ok = (
                all(r.get("score_type") == "distance" for r in results)
                or closest["score"] > farthest["score"]
            )
            self.assertIs(
                ok,
                True,
                "score contract is self-inconsistent: distances are 0.1 (id=close_1) "
                f"and 0.9 (id=far_1), got scores {closest['score']} / {farthest['score']} "
                f"with no score_type labels: {results}",
            )


if __name__ == "__main__":
    unittest.main()
