"""Ollama embedding dialect parity and per-request config snapshot (issue #511).

Covers the acceptance behaviors of check C1 (EMBED-001) and check C2 (EMBED-002):

- Legacy Ollama ``/api/embeddings`` accepts ONLY per-item ``{"model", "prompt"}``
  bodies: ``embed_batch`` must fan out one prompt request per text and return
  the vectors in input order.
- Modern Ollama ``/api/embed`` accepts ONLY ``{"model", "input"}`` bodies:
  ``embed_single`` sends a scalar ``input``; ``embed_batch`` sends a list.
- One in-flight embedding request completes under the configuration (URL,
  mode, ollama style, model, prefixes) that issued it — a mid-flight settings
  flip must not change the payload, the response parsing, or the metrics of
  the request already in flight.
"""

import asyncio
import os
import sys
import threading

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from unittest.mock import patch

import pytest

from app.config import settings
from app.services.embeddings import EmbeddingError, EmbeddingService

LEGACY_URL = "http://localhost:11434/api/embeddings"
MODERN_URL = "http://localhost:11434/api/embed"
OPENAI_URL = "http://localhost:9001/v1/embeddings"  # port avoids port detection
TEI_URL = "http://localhost:8080/embed"

VEC_ONE = [1.0, 11.0, 21.0]
VEC_TWO = [2.0, 12.0, 22.0]


@pytest.fixture(autouse=True)
def allow_local_services_for_mocked_embedding_urls():
    """Dialect tests use mocked localhost clients; keep the opt-in local."""
    previous = os.environ.get("ALLOW_LOCAL_SERVICES")
    os.environ["ALLOW_LOCAL_SERVICES"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        else:
            os.environ["ALLOW_LOCAL_SERVICES"] = previous


class FakeResponse:
    def __init__(self, status_code, text="", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class LegacyFakeClient:
    """Strict legacy Ollama /api/embeddings fixture.

    Accepts exactly ``{"model", "prompt"}`` per request; any request carrying
    ``"input"`` is rejected with HTTP 400 (mirrors the frozen check C1).
    """

    def __init__(self):
        self.requests = []  # list of (url, payload dict)
        self.timeouts_remaining = 0  # per-text timeouts to inject before success

    async def post(self, url, json=None):
        payload = dict(json) if isinstance(json, dict) else {}
        self.requests.append((url, payload))
        keys = set(payload.keys())
        if "input" in payload:
            return FakeResponse(400, "legacy endpoint rejects input")
        if keys == {"model", "prompt"}:
            if self.timeouts_remaining > 0:
                self.timeouts_remaining -= 1
                import httpx

                raise httpx.TimeoutException("legacy fake timeout")
            prompt = str(payload["prompt"])
            if "one" in prompt:
                return FakeResponse(200, json_body={"embedding": list(VEC_ONE)})
            if "two" in prompt:
                return FakeResponse(200, json_body={"embedding": list(VEC_TWO)})
            return FakeResponse(200, json_body={"embedding": [0.1, 0.2, 0.3]})
        return FakeResponse(400, "legacy endpoint rejects payload keys %s" % sorted(keys))


class ModernFakeClient:
    """Strict modern Ollama /api/embed fixture.

    Accepts exactly ``{"model", "input"}`` where ``input`` is a scalar string
    or a list of strings; rejects every other shape with HTTP 400.
    """

    LIST_VECS = [[0.1, 0.2], [0.3, 0.4]]

    def __init__(self):
        self.requests = []

    async def post(self, url, json=None):
        payload = dict(json) if isinstance(json, dict) else {}
        self.requests.append((url, payload))
        keys = set(payload.keys())
        if keys != {"model", "input"}:
            return FakeResponse(400, "modern endpoint rejects keys %s" % sorted(keys))
        inp = payload["input"]
        if isinstance(inp, list):
            if not all(isinstance(t, str) for t in inp):
                return FakeResponse(400, "modern endpoint rejects non-string list")
            return FakeResponse(
                200,
                json_body={"embeddings": [list(v) for v in self.LIST_VECS[: len(inp)]]},
            )
        if isinstance(inp, str):
            return FakeResponse(200, json_body={"embeddings": [[0.1, 0.2]]})
        return FakeResponse(
            400, "modern endpoint rejects input type %s" % type(inp).__name__
        )


class GatedFakeClient:
    """Records every request per-URL; the FIRST response is gated.

    Replies in the dialect of the URL it is asked to call, so a follow-up
    request under a flipped config can also complete honestly (check C2).
    """

    def __init__(self):
        self.requests = []
        self.recorded = asyncio.Event()
        self.gate = asyncio.Event()

    async def post(self, url, json=None):
        payload = dict(json) if isinstance(json, dict) else {}
        self.requests.append((url, payload))
        self.recorded.set()
        await self.gate.wait()
        if "/v1/embeddings" in url:
            return FakeResponse(200, json_body={"data": [{"embedding": [1.0, 1.1]}]})
        if url.rstrip("/").endswith("/embed"):
            # Modern Ollama /api/embed and native TEI /embed both answer with
            # {"embeddings": [[...]]}; echo one row per input string.
            inp = payload.get("input", payload.get("inputs"))
            n = len(inp) if isinstance(inp, list) else 1
            return FakeResponse(200, json_body={"embeddings": [[3.0, 3.1]] * n})
        return FakeResponse(200, json_body={"embedding": [5.0, 5.1]})


def _make_service(fake_client):
    service = EmbeddingService()
    service._client = fake_client
    return service


# ---------------------------------------------------------------------------
# Dialect detection (EMBED-001)
# ---------------------------------------------------------------------------


class TestOllamaDialectDetection:
    """Modern /api/embed must be distinguished from legacy /api/embeddings."""

    def test_modern_embed_url_stays_ollama_mode(self):
        with patch.object(settings, "ollama_embedding_url", MODERN_URL):
            service = EmbeddingService()
            assert service.provider_mode == "ollama"
            assert service.embeddings_url == MODERN_URL

    def test_legacy_embeddings_url_stays_ollama_mode(self):
        with patch.object(settings, "ollama_embedding_url", LEGACY_URL):
            service = EmbeddingService()
            assert service.provider_mode == "ollama"
            assert service.embeddings_url == LEGACY_URL

    def test_bare_ollama_url_resolves_legacy(self):
        with patch.object(settings, "ollama_embedding_url", "http://localhost:11434"):
            service = EmbeddingService()
            assert service.provider_mode == "ollama"
            assert service.embeddings_url == LEGACY_URL

    def test_endpoint_style_helper(self):
        with patch.object(settings, "ollama_embedding_url", MODERN_URL):
            service = EmbeddingService()
            assert service._ollama_endpoint_style(MODERN_URL) == "modern"
            assert service._ollama_endpoint_style(LEGACY_URL) == "legacy"
            assert service._ollama_endpoint_style("http://localhost:11434") == "legacy"
            # A URL that is not ollama-mode at all has no ollama style.
            assert service._ollama_endpoint_style(OPENAI_URL) is None
            assert service._ollama_endpoint_style(TEI_URL) is None

    def test_openai_and_tei_detection_unchanged(self):
        with patch.object(settings, "ollama_embedding_url", OPENAI_URL):
            service = EmbeddingService()
            assert service.provider_mode == "openai"
        with patch.object(settings, "ollama_embedding_url", TEI_URL):
            service = EmbeddingService()
            assert service.provider_mode == "tei"

    def test_resolved_cache_invalidated_on_style_change(self):
        """Flipping between legacy and modern URLs re-resolves the style."""
        with patch.object(settings, "ollama_embedding_url", LEGACY_URL):
            service = EmbeddingService()
            assert service.provider_mode == "ollama"
            assert service._ollama_endpoint_style(service.embeddings_url) == "legacy"
        with patch.object(settings, "ollama_embedding_url", MODERN_URL):
            assert service.provider_mode == "ollama"
            assert service._ollama_endpoint_style(service.embeddings_url) == "modern"


# ---------------------------------------------------------------------------
# Legacy dialect payloads (EMBED-001 part a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLegacyOllamaDialect:
    async def test_embed_batch_fans_out_prompt_requests_in_order(self):
        fake = LegacyFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vectors = await service.embed_batch(["passage one", "passage two"], batch_size=8)

        assert vectors == [VEC_ONE, VEC_TWO]
        assert len(fake.requests) == 2
        for _, payload in fake.requests:
            assert set(payload.keys()) == {"model", "prompt"}, (
                "legacy requests must use 'prompt' and never 'input': %r" % sorted(payload)
            )

    async def test_embed_single_sends_prompt_body(self):
        fake = LegacyFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vector = await service.embed_single("one")

        assert vector == VEC_ONE
        assert set(fake.requests[0][1].keys()) == {"model", "prompt"}

    async def test_embed_batch_batch_size_one(self):
        """batch_size=1 on legacy still fans out per-item prompt requests."""
        fake = LegacyFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vectors = await service.embed_batch(["passage one", "passage two"], batch_size=1)

        assert vectors == [VEC_ONE, VEC_TWO]
        assert all(set(p.keys()) == {"model", "prompt"} for _, p in fake.requests)

    async def test_embed_batch_legacy_timeout_retries_item(self):
        """A per-item timeout on the legacy fan-out gets backoff retry."""
        fake = LegacyFakeClient()
        fake.timeouts_remaining = 1
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vectors = await service.embed_batch(["passage one"], batch_size=4)

        assert vectors == [VEC_ONE]
        # One failed attempt + one retry for the single item.
        assert len(fake.requests) == 2

    async def test_embed_batch_legacy_fail_fast_false_records_failed_index(self):
        """A permanently failing legacy item records its batch index (fail_fast=False)."""
        fake = LegacyFakeClient()
        # "boom" text never matches "one"/"two" and the strict fixture rejects
        # nothing — instead force failures by monkeying the fixture behavior:
        # reject every request whose prompt mentions "boom".
        original_post = fake.post

        async def rejecting_post(url, json=None):
            if json and "boom" in str(json.get("prompt", "")):
                return FakeResponse(500, "legacy endpoint exploded")
            return await original_post(url, json=json)

        fake.post = rejecting_post
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            embeddings, failed = await service.embed_batch(
                ["passage one", "boom"], batch_size=1, fail_fast=False
            )

        assert failed == [1]
        assert embeddings[0] == VEC_ONE
        assert embeddings[1] is None

    async def test_embed_batch_legacy_fail_fast_raises(self):
        fake = LegacyFakeClient()

        async def rejecting_post(url, json=None):
            if json and "boom" in str(json.get("prompt", "")):
                return FakeResponse(500, "legacy endpoint exploded")
            return await LegacyFakeClient.post(fake, url, json=json)

        fake.post = rejecting_post
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            with pytest.raises(EmbeddingError):
                await service.embed_batch(["passage one", "boom"], batch_size=1)


# ---------------------------------------------------------------------------
# Modern dialect payloads (EMBED-001 part b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestModernOllamaDialect:
    async def test_embed_single_sends_scalar_input(self):
        fake = ModernFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", MODERN_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vector = await service.embed_single("q")

        assert vector == [0.1, 0.2]
        _, payload = fake.requests[-1]
        assert isinstance(payload.get("input"), str), (
            "modern single requests must send a scalar 'input', got %r" % payload.get("input")
        )
        assert set(payload.keys()) == {"model", "input"}

    async def test_embed_batch_sends_list_input(self):
        fake = ModernFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", MODERN_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            vectors = await service.embed_batch(["s-one", "s-two"], batch_size=8)

        assert vectors == ModernFakeClient.LIST_VECS
        batch_payloads = [p for _, p in fake.requests if isinstance(p.get("input"), list)]
        assert batch_payloads, "modern batch must send a list 'input'"
        assert set(batch_payloads[0].keys()) == {"model", "input"}

    async def test_embed_batch_modern_count_mismatch_raises(self):
        fake = ModernFakeClient()

        async def short_post(url, json=None):
            payload = dict(json) if isinstance(json, dict) else {}
            fake.requests.append((url, payload))
            # Return fewer embeddings than inputs.
            return FakeResponse(200, json_body={"embeddings": [[0.1, 0.2]]})

        fake.post = short_post
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "ollama_embedding_url", MODERN_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            service = _make_service(fake)
            with pytest.raises(EmbeddingError, match="count mismatch"):
                await service.embed_batch(["a", "b"], batch_size=8)


# ---------------------------------------------------------------------------
# Per-request config snapshot (EMBED-002, check C2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRequestConfigSnapshot:
    async def test_held_request_completes_under_original_config(self):
        fake = GatedFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""), \
                patch.object(settings, "ollama_embedding_url", OPENAI_URL):
            service = _make_service(fake)

            task = asyncio.create_task(service.embed_single("held query"))
            await asyncio.wait_for(fake.recorded.wait(), timeout=10)

            # Mid-flight settings flip: OpenAI dialect -> TEI dialect.
            settings.ollama_embedding_url = TEI_URL
            fake.gate.set()

            vector = await asyncio.wait_for(task, timeout=10)

        assert list(vector) == [1.0, 1.1]

    async def test_held_request_metrics_name_original_config(self):
        fake = GatedFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""), \
                patch.object(settings, "ollama_embedding_url", OPENAI_URL):
            service = _make_service(fake)

            task = asyncio.create_task(service.embed_single("held query"))
            await asyncio.wait_for(fake.recorded.wait(), timeout=10)
            settings.ollama_embedding_url = TEI_URL
            fake.gate.set()
            await asyncio.wait_for(task, timeout=10)

            metrics = dict(service.last_metrics or {})
            assert metrics.get("provider_url") == OPENAI_URL
            assert metrics.get("mode") == "openai"

    async def test_next_request_uses_new_config(self):
        fake = GatedFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""), \
                patch.object(settings, "ollama_embedding_url", OPENAI_URL):
            service = _make_service(fake)

            task = asyncio.create_task(service.embed_single("held query"))
            await asyncio.wait_for(fake.recorded.wait(), timeout=10)
            settings.ollama_embedding_url = TEI_URL
            fake.gate.set()
            await asyncio.wait_for(task, timeout=10)

            # The SECOND request must use the new (TEI) URL and body dialect.
            await asyncio.wait_for(service.embed_single("next"), timeout=10)
            tei_requests = [
                (url, payload)
                for url, payload in fake.requests
                if url == TEI_URL and "inputs" in payload
            ]
            assert tei_requests, (
                "no TEI request with an {'inputs': ...} body after the flip; "
                "observed: %r" % [(u, sorted(p)) for u, p in fake.requests]
            )

    async def test_embed_batch_snapshots_once_for_whole_batch(self):
        """A batch in flight when settings flip completes under the issuing config."""
        fake = GatedFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""), \
                patch.object(settings, "ollama_embedding_url", MODERN_URL):
            service = _make_service(fake)

            task = asyncio.create_task(service.embed_batch(["b-one", "b-two"], batch_size=8))
            await asyncio.wait_for(fake.recorded.wait(), timeout=10)
            settings.ollama_embedding_url = LEGACY_URL
            fake.gate.set()
            vectors = await asyncio.wait_for(task, timeout=10)

        # The gate replies with the modern {"embeddings": [[...]]} shape and the
        # batch must parse it under the modern snapshot it was issued with.
        assert vectors == [[3.0, 3.1]] * 2

    async def test_prefix_snapshot_keys_cache_entry(self):
        """The L1 cache key is derived from the issuing (snapshot) prefix."""
        fake = GatedFakeClient()
        with patch.object(settings, "redis_url", ""), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", "OLD: "), \
                patch.object(settings, "embedding_query_prefix", "OLDQ: "), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL):
            service = _make_service(fake)

            task = asyncio.create_task(service.embed_passage("snapshot text"))
            await asyncio.wait_for(fake.recorded.wait(), timeout=10)
            # Mid-flight prefix flip must not change the in-flight request.
            settings.embedding_doc_prefix = "NEW: "
            fake.gate.set()
            await asyncio.wait_for(task, timeout=10)

            # Restoring the ORIGINAL prefix must hit the cache entry written
            # under the snapshot key (no second provider request).
            settings.embedding_doc_prefix = "OLD: "
            result = await asyncio.wait_for(service.embed_passage("snapshot text"), timeout=10)
            assert result == [5.0, 5.1]
            assert len(fake.requests) == 1


# ---------------------------------------------------------------------------
# Redis L2 cache off the event loop (FULL-ENH-04 embeddings sites)
# ---------------------------------------------------------------------------


class RecordingRedisClient:
    """Sync fake redis client recording the thread each call runs on."""

    def __init__(self, delay: float = 0.0):
        self.get_thread_idents: list = []
        self.setex_thread_idents: list = []
        self.delay = delay
        self._store: dict = {}

    def ping(self):
        return True

    def get(self, key):
        import time

        self.get_thread_idents.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self.setex_thread_idents.append(threading.get_ident())
        self._store[key] = value


@pytest.mark.asyncio
class TestRedisCallsOffEventLoop:
    async def test_redis_get_runs_off_the_event_loop_thread(self):
        fake_redis = RecordingRedisClient()
        with patch("app.services.embeddings.redis") as mock_redis_module, \
                patch.object(settings, "redis_url", "redis://localhost:6379/0"), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            mock_redis_module.from_url.return_value = fake_redis
            service = EmbeddingService()
            service._client = LegacyFakeClient()

            await service.embed_single("one")

        assert fake_redis.get_thread_idents, "redis get was never called"
        loop_thread = threading.get_ident()
        for ident in fake_redis.get_thread_idents:
            assert ident != loop_thread, "redis get ran on the event-loop thread"

    async def test_redis_setex_runs_off_the_event_loop_thread(self):
        fake_redis = RecordingRedisClient()
        with patch("app.services.embeddings.redis") as mock_redis_module, \
                patch.object(settings, "redis_url", "redis://localhost:6379/0"), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            mock_redis_module.from_url.return_value = fake_redis
            service = EmbeddingService()
            service._client = LegacyFakeClient()

            await service.embed_single("one")

        assert fake_redis.setex_thread_idents, "redis setex was never called"
        loop_thread = threading.get_ident()
        for ident in fake_redis.setex_thread_idents:
            assert ident != loop_thread, "redis setex ran on the event-loop thread"

    async def test_redis_timeout_degrades_to_cache_miss(self):
        """A redis get exceeding redis_io_timeout_seconds falls through to the provider."""
        fake_redis = RecordingRedisClient(delay=2.5)  # > redis_io_timeout_seconds (1.0)
        with patch("app.services.embeddings.redis") as mock_redis_module, \
                patch.object(settings, "redis_url", "redis://localhost:6379/0"), \
                patch.object(settings, "ollama_embedding_url", LEGACY_URL), \
                patch.object(settings, "embedding_model", "m"), \
                patch.object(settings, "embedding_doc_prefix", ""), \
                patch.object(settings, "embedding_query_prefix", ""):
            mock_redis_module.from_url.return_value = fake_redis
            service = EmbeddingService()
            fake_http = LegacyFakeClient()
            service._client = fake_http

            vector = await service.embed_single("one")

        assert vector == VEC_ONE, "timed-out redis get must degrade to provider call"
        assert len(fake_http.requests) == 1
