"""Implementation-review pins for issue #494 (reviewer round 1 nits).

Pins two deliberate design branches called out during the Phase 4.5 review:

1. The batch-embedding token-overflow 500 path must NOT charge the outage
   circuit breaker: an overflow response is a recoverable input-shape event
   handled by the batch-size retry ladder (split + retry), not a provider
   outage. (embeddings.py batch path: a 500 whose body matches the
   token-overflow signature returns normally from the breaker-wrapped
   closure; non-overflow 5xx raise inside it.)
2. The legacy-construction ordering fix: ``Settings(chunk_size=512)`` must
   yield ``chunk_size_chars == 2048`` at construction time (the check-time
   discovery that the mode="before" validators previously ran before the
   legacy fields populated).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings  # noqa: E402


class TestTokenOverflowDoesNotChargeBreaker(unittest.IsolatedAsyncioTestCase):
    """Overflow-500 stays uncharged (retry ladder territory); a plain 500 charges."""

    def setUp(self):
        from unittest.mock import MagicMock, patch

        from app.services import embeddings as emb_mod

        settings = MagicMock()
        settings.ollama_embedding_url = "http://embed-test-host:11434/v1/embeddings"
        settings.embedding_model = "nomic-embed-text"
        settings.embedding_doc_prefix = ""
        settings.embedding_query_prefix = ""
        settings.redis_url = None
        settings.embedding_cache_ttl_seconds = 3600
        settings.embedding_batch_max_retries = 2
        settings.embedding_batch_min_sub_size = 1
        self._settings_patcher = patch(
            "app.services.embeddings.settings", settings
        )
        self._settings_patcher.start()
        self._ssrf_patcher = patch("app.services.embeddings.assert_url_safe")
        self._ssrf_patcher.start()
        self._emb_mod = emb_mod
        from app.services.circuit_breaker import CircuitBreakerState

        breaker = emb_mod.embeddings_cb
        breaker._state = CircuitBreakerState.CLOSED
        breaker._fail_counter = 0  # the real counter (circuit_breaker.py:83)
        self._breaker = breaker
        self._state_cls = CircuitBreakerState

    def tearDown(self):
        self._ssrf_patcher.stop()
        self._settings_patcher.stop()
        self._breaker._state = self._state_cls.CLOSED
        self._breaker._fail_counter = 0

    def _install_transport(self, body: str):
        import httpx

        async def handler(request):
            return httpx.Response(500, text=body)

        self.service._client._transport = httpx.MockTransport(handler)

    async def test_overflow_500_leaves_breaker_closed_and_non_overflow_500_charges(self):
        from app.services.embeddings import EmbeddingService

        overflow_body = (
            '{"error":"input (5 tokens) is too large: input tokens"}'
        )
        # 1) overflow 500: handled by the split/retry ladder, breaker uncharged.
        self.service = EmbeddingService()
        self._install_transport(overflow_body)
        with self.assertRaises(Exception):
            await self.service._embed_batch_api(["x" * 10])
        self.assertEqual(
            self._breaker._fail_counter,
            0,
            "token-overflow 500 must stay uncharged: it is handled by the "
            "batch-size retry ladder, not a provider outage",
        )
        self.assertEqual(self._breaker._state, self._state_cls.CLOSED)

        # 2) non-overflow 500: outage — must charge the breaker.
        self._breaker._state = self._state_cls.CLOSED
        self._breaker._fail_counter = 0
        self.service = EmbeddingService()
        self._install_transport('{"error":"internal server error"}')
        with self.assertRaises(Exception):
            await self.service._embed_batch_api(["x" * 10])
        self.assertGreaterEqual(
            self._breaker._fail_counter,
            1,
            "a non-overflow 500 is a provider outage and must record at "
            "least one breaker failure inside the wrapped operation",
        )
        self._breaker._state = self._state_cls.CLOSED
        self._breaker._fail_counter = 0


class TestLegacyConstructionConversion(unittest.TestCase):
    def test_chunk_size_converts_at_construction(self):
        settings = Settings(
            ADMIN_SECRET_TOKEN="a" * 48,
            JWT_SECRET_KEY="b" * 48,
            USERS_ENABLED=False,
            chunk_size=512,
        )
        self.assertEqual(settings.chunk_size_chars, 2048)
        self.assertEqual(settings.chunk_size, 512)  # legacy echo preserved


if __name__ == "__main__":
    unittest.main()
