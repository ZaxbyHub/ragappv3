"""Regression tests for LLMHealthChecker.check_embeddings timeout handling.

check_embeddings previously overwrote the timeout attribute of an INJECTED
embedding service with the checker's short probe timeout (default 5s) for the
duration of the probe. The injected service is the shared production instance,
so this (a) falsely failed slow-but-healthy embedding backends and (b) raced
concurrent real embed calls reading the same mutable attribute. The checker's
short timeout must apply only to a self-created probe service.
"""
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub optional heavy deps (matching test_chat_streaming.py pattern)
for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

from app.services.llm_health import LLMHealthChecker  # noqa: E402


class TestCheckEmbeddingsTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_injected_service_timeout_untouched(self):
        service = MagicMock()
        service.timeout = 120.0
        service.embed_single = AsyncMock(return_value=[0.0] * 8)

        checker = LLMHealthChecker(embedding_service=service, timeout=5.0)
        result = await checker.check_embeddings()

        self.assertTrue(result["ok"])
        self.assertEqual(
            service.timeout,
            120.0,
            "Injected production embedding service timeout must not be "
            "modified by the health probe",
        )
        service.embed_single.assert_awaited_once_with("ping")

    async def test_injected_service_slow_backend_not_clobbered_to_probe_timeout(self):
        # The probe must run under the service's own timeout; assert the
        # attribute is never even transiently reassigned during the call.
        writes = []

        class TimeoutSpy:
            def __init__(self):
                self._timeout = 120.0

            @property
            def timeout(self):
                return self._timeout

            @timeout.setter
            def timeout(self, value):
                writes.append(value)
                self._timeout = value

        service = TimeoutSpy()
        service.embed_single = AsyncMock(return_value=[0.0] * 8)

        checker = LLMHealthChecker(embedding_service=service, timeout=5.0)
        result = await checker.check_embeddings()

        self.assertTrue(result["ok"])
        self.assertEqual(
            writes, [],
            f"check_embeddings wrote to the injected service's timeout: {writes}",
        )

    async def test_error_reported_not_raised(self):
        service = MagicMock()
        service.timeout = 120.0
        service.embed_single = AsyncMock(side_effect=RuntimeError("boom"))

        checker = LLMHealthChecker(embedding_service=service, timeout=5.0)
        result = await checker.check_embeddings()

        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])
        self.assertEqual(service.timeout, 120.0)


if __name__ == "__main__":
    unittest.main()
