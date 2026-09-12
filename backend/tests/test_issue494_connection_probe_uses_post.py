"""
Issue #494 acceptance check — AC16 (OPS-007): the settings connection test
probes the embeddings endpoint with GET, which is POST-only, so a healthy
provider reports disconnected.

Root cause (verified at base a543361):
- settings.py ``test_connection`` (GET /api/settings/connection) probes each
  target with ``await client.get(url)``. Embedding endpoints only accept
  POST (native TEI /embed, OpenAI-compatible /v1/embeddings), so a healthy
  provider answers 405 to the GET and the endpoint reports ok=False.

Check: with the outbound HTTP client mocked so that POST to the embeddings
URL succeeds (200, valid embedding response) and GET returns 405
(method-not-allowed, the healthy-provider behaviour for a wrong verb), the
connection result for "embeddings" must be ok=True.
At the pre-fix base the probe uses GET/405 -> ok=False -> this check fails.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors the other backend test files)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto

from fastapi.testclient import TestClient  # noqa: E402

TEST_DB_PATH = None
TEST_DATA_DIR = None


def _setup_test_db():
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"
    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


_setup_test_db()

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


class TestConnectionProbeUsesPostForEmbeddings(unittest.TestCase):
    """AC16 — DISCRIMINATING: the embeddings probe must use the POST verb."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        from app.api.deps import get_db
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        self._get_db = get_db

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        self._test_pool.close_all()

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    @patch("app.api.routes.settings.httpx.AsyncClient")
    def test_ac16_embeddings_probe_succeeds_when_only_post_works(self, mock_async_client):
        """Healthy provider: POST -> 200 embedding response, GET -> 405.

        The transport is fully mocked (no network). If the probe used the
        correct verb it sees the 200 and reports ok=True; the pre-fix GET
        sees 405 and reports ok=False.
        """
        mock_client = AsyncMock()
        ok_post = MagicMock()
        ok_post.status_code = 200
        method_not_allowed = MagicMock()
        method_not_allowed.status_code = 405
        mock_client.post = AsyncMock(return_value=ok_post)
        mock_client.get = AsyncMock(return_value=method_not_allowed)
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)

        originals = (
            settings.ollama_embedding_url,
            settings.ollama_chat_url,
            settings.reranker_url,
        )
        settings.ollama_embedding_url = "http://localhost:11434/v1/embeddings"
        settings.ollama_chat_url = "http://localhost:11434"
        settings.reranker_url = ""  # keep the target list to embeddings+chat
        try:
            resp = self.client.get("/api/settings/connection")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertIn("embeddings", data)

            print("AC16 CHECK: FAIL")
            self.assertTrue(
                data["embeddings"]["ok"],
                f"embeddings target reported disconnected although POST to the "
                f"embeddings endpoint returns 200 (GET returns 405, i.e. the "
                f"provider is healthy but the probe uses the wrong verb). "
                f"Result: {data['embeddings']}",
            )
            self.assertEqual(data["embeddings"]["status"], 200)
            # The POST path really was exercised (not a coincidental 2xx GET).
            self.assertGreaterEqual(mock_client.post.await_count, 1)
        finally:
            (
                settings.ollama_embedding_url,
                settings.ollama_chat_url,
                settings.reranker_url,
            ) = originals


if __name__ == "__main__":
    unittest.main()
