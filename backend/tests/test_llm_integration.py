"""
Unit tests for LLM integration services.

Tests EmbeddingService, ModelChecker, and LLMClient with mocked httpx.AsyncClient.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.llm_client import LLMClient, LLMError
from app.services.model_checker import ModelChecker


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"), \
         patch("app.services.model_checker.assert_url_safe"):
        yield


class TestEmbeddingService(unittest.TestCase):
    """Test cases for EmbeddingService."""

    def setUp(self):
        """Set up test fixtures."""
        self.settings_patcher = patch("app.services.embeddings.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.ollama_embedding_url = "http://localhost:11434"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.embedding_doc_prefix = ""
        self.mock_settings.embedding_query_prefix = ""
        self.mock_settings.embedding_batch_size = 512
        self.mock_settings.embedding_batch_max_retries = 3
        self.mock_settings.embedding_batch_min_sub_size = 1
        self.mock_settings.embedding_concurrent_batches = 4
        self.mock_settings.tri_vector_search_enabled = False
        self.mock_settings.flag_embedding_url = None

    def tearDown(self):
        """Clean up test fixtures."""
        self.settings_patcher.stop()

    def test_embed_single_returns_embedding_list(self):
        """Test embed_single returns a list of floats on successful response."""
        async def run_test():
            service = EmbeddingService()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
            mock_response.raise_for_status = MagicMock()

            service._client.post = AsyncMock(return_value=mock_response)

            result = await service.embed_single("test text")

            self.assertEqual(result, [0.1, 0.2, 0.3])
            service._client.post.assert_called_once()
            call_args = service._client.post.call_args
            self.assertIn("http://localhost:11434", call_args[0][0])

        asyncio.run(run_test())

    def test_embed_single_handles_non_200_status(self):
        """Test embed_single raises EmbeddingError on non-200 status."""
        async def run_test():
            service = EmbeddingService()
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.text = "Internal Server Error"

            service._client.post = AsyncMock(return_value=mock_response)

            with self.assertRaises(EmbeddingError) as context:
                await service.embed_single("test text")

            self.assertIn("500", str(context.exception))

        asyncio.run(run_test())

    def test_embed_single_handles_json_parse_error(self):
        """Test embed_single raises EmbeddingError on invalid JSON response."""
        async def run_test():
            service = EmbeddingService()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.side_effect = ValueError("Invalid JSON")
            mock_response.raise_for_status = MagicMock()

            service._client.post = AsyncMock(return_value=mock_response)

            with self.assertRaises(EmbeddingError) as context:
                await service.embed_single("test text")

            self.assertIn("Invalid", str(context.exception))

        asyncio.run(run_test())


class TestModelChecker(unittest.TestCase):
    """Test cases for ModelChecker."""

    def setUp(self):
        """Set up test fixtures."""
        self.settings_patcher = patch("app.services.model_checker.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.ollama_embedding_url = "http://localhost:11434"
        self.mock_settings.ollama_chat_url = "http://localhost:11434"
        self.mock_settings.instant_chat_url = "http://localhost:1234"
        self.mock_settings.embedding_model = "nomic-embed-text"
        self.mock_settings.chat_model = "qwen2.5:32b"
        self.mock_settings.instant_chat_model = "nvidia/nemotron-3-nano-4b"

    def tearDown(self):
        """Clean up test fixtures."""
        self.settings_patcher.stop()

    def test_check_model_availability_available_model(self):
        """Test _check_model_availability returns available=True when model exists."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "models": [
                    {"name": "nomic-embed-text:latest"},
                    {"name": "qwen2.5:32b"}
                ]
            }

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:11434",
                "nomic-embed-text"
            )

            self.assertTrue(result["available"])
            self.assertIsNone(result["error"])
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            self.assertEqual(call_args[0][0], "http://localhost:11434/api/tags")

        asyncio.run(run_test())

    def test_check_model_availability_missing_model(self):
        """Test _check_model_availability returns available=False when model not found."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "models": [
                    {"name": "llama2:latest"},
                    {"name": "mistral:7b"}
                ]
            }

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:11434",
                "nomic-embed-text"
            )

            self.assertFalse(result["available"])
            self.assertIn("not found", result["error"])
            self.assertIn("nomic-embed-text", result["error"])
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            self.assertEqual(call_args[0][0], "http://localhost:11434/api/tags")

        asyncio.run(run_test())

    def test_check_models_checks_thinking_and_instant_chat_models(self):
        """Test check_models includes both Thinking and Instant chat model probes."""
        async def run_test():
            checker = ModelChecker()
            checker._check_model_availability = AsyncMock(
                side_effect=[
                    {"available": True, "error": None},
                    {"available": True, "error": None},
                    {"available": False, "error": "Model not found"},
                ]
            )

            result = await checker.check_models()

            self.assertIn("embedding_model", result)
            self.assertIn("chat_model", result)
            self.assertIn("instant_chat_model", result)
            self.assertFalse(result["instant_chat_model"]["available"])
            calls = checker._check_model_availability.call_args_list
            self.assertEqual(calls[0].args[1:], ("http://localhost:11434", "nomic-embed-text"))
            self.assertEqual(calls[1].args[1:], ("http://localhost:11434", "qwen2.5:32b"))
            self.assertEqual(
                calls[2].args[1:],
                ("http://localhost:1234", "nvidia/nemotron-3-nano-4b"),
            )

        asyncio.run(run_test())

    def test_detect_provider_type_native_tei(self):
        """Bare port 8080 and explicit /embed paths resolve to native TEI."""
        checker = ModelChecker()
        # Bare TEI default port -> native TEI
        self.assertEqual(checker._detect_provider_type("http://localhost:8080"), "tei")
        self.assertEqual(checker._detect_provider_type("http://localhost:8080/"), "tei")
        # Explicit native /embed path -> native TEI (any port)
        self.assertEqual(
            checker._detect_provider_type("http://localhost:9000/embed"), "tei"
        )
        # Explicit OpenAI path must NOT be hijacked by TEI detection
        self.assertEqual(
            checker._detect_provider_type("http://localhost:8080/v1/embeddings"),
            "openai_compatible",
        )
        # LM Studio default port stays OpenAI-compatible
        self.assertEqual(
            checker._detect_provider_type("http://localhost:1234"), "openai_compatible"
        )
        # Ollama default port stays Ollama
        self.assertEqual(
            checker._detect_provider_type("http://localhost:11434"), "ollama"
        )

    def test_check_model_availability_tei_uses_info_endpoint(self):
        """Native TEI availability probes <root>/info and matches model_id."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "model_id": "microsoft/harrier-oss-v1-0.6b",
                "max_input_length": 8192,
            }
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:8080",
                "microsoft/harrier-oss-v1-0.6b",
            )

            self.assertTrue(result["available"])
            self.assertIsNone(result["error"])
            mock_client.get.assert_called_once()
            self.assertEqual(
                mock_client.get.call_args[0][0], "http://localhost:8080/info"
            )

        asyncio.run(run_test())

    def test_check_model_availability_tei_strips_embed_suffix(self):
        """An explicit /embed URL still probes the server-root /info."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            # Lenient last-segment match: config has the bare name, live is qualified.
            mock_response.json.return_value = {"model_id": "BAAI/bge-m3"}
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:8080/embed",
                "bge-m3",
            )

            self.assertTrue(result["available"])
            self.assertEqual(
                mock_client.get.call_args[0][0], "http://localhost:8080/info"
            )

        asyncio.run(run_test())

    def test_check_model_availability_tei_mismatch(self):
        """A mismatched live TEI model_id reports unavailable with detail."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"model_id": "BAAI/bge-m3"}
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:8080",
                "nomic-embed-text",
            )

            self.assertFalse(result["available"])
            self.assertIn("not found", result["error"])
            self.assertIn("BAAI/bge-m3", result["error"])

        asyncio.run(run_test())

    def test_check_model_availability_tei_non_dict_info(self):
        """A non-dict /info body reports unavailable without raising (LC5-12)."""
        async def run_test():
            checker = ModelChecker()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = ["unexpected", "list", "body"]
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await checker._check_model_availability(
                mock_client,
                "http://localhost:8080",
                "BAAI/bge-m3",
            )

            self.assertFalse(result["available"])
            self.assertIn("model_id", result["error"])

        asyncio.run(run_test())


class TestLLMClient(unittest.TestCase):
    """Test cases for LLMClient."""

    def setUp(self):
        """Set up test fixtures."""
        self.settings_patcher = patch("app.services.llm_client.settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.ollama_chat_url = "http://localhost:11434"
        self.mock_settings.chat_model = "qwen2.5:32b"

    def tearDown(self):
        """Clean up test fixtures."""
        self.settings_patcher.stop()

    def test_chat_completion_extracts_content(self):
        """Test chat_completion extracts content from successful response."""
        async def run_test():
            # Create mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "choices": [
                    {"message": {"content": "Hello, world!"}}
                ]
            }
            mock_response.raise_for_status = MagicMock()

            # Create mock client
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)

            # Create LLMClient and patch its _client
            client = LLMClient()
            client._client = mock_client

            messages = [{"role": "user", "content": "Hello"}]
            result = await client.chat_completion(messages)

            self.assertEqual(result, "Hello, world!")
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            self.assertEqual(call_args[0][0], "http://localhost:11434/v1/chat/completions")
            self.assertEqual(call_args[1]["json"]["model"], "qwen2.5:32b")
            self.assertEqual(call_args[1]["json"]["messages"], messages)
            self.assertEqual(call_args[1]["json"]["stream"], False)
            self.assertEqual(call_args[1]["json"]["temperature"], 0.7)
            self.assertIn("max_tokens", call_args[1]["json"])

        asyncio.run(run_test())

    def test_chat_completion_handles_non_200_status(self):
        """Test chat_completion raises LLMError on non-200 status."""
        async def run_test():
            # Create mock response with 503 status
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.text = "Service Unavailable"
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=mock_response
            )

            # Create mock client
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)

            # Create LLMClient and patch its _client
            client = LLMClient()
            client._client = mock_client

            messages = [{"role": "user", "content": "Hello"}]

            with self.assertRaises(LLMError) as context:
                await client.chat_completion(messages)

            self.assertIn("503", str(context.exception))
            self.assertIn("Service Unavailable", str(context.exception))

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
