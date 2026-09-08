"""Tests for bounded optional Redis I/O (issue #511 FULL-ENH-04, AC14).

Contract:
- ``redis_call`` runs a sync Redis client call off the event loop under
  ``settings.redis_io_timeout_seconds``; a hung call raises
  ``asyncio.TimeoutError`` (which the surrounding cache try/except treats as
  a cache miss) and args/kwargs are forwarded faithfully.
- QueryTransformer.transform and QueryPlanner.plan complete < 3s with a
  3s-blocking fake Redis, the event loop stays responsive (watchdog max gap
  < 250 ms), and results are correct (cache-miss degradation).
- a fast dict-backed fake keeps working (the caches remain functional).
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.llm_client import LLMClient
from app.services.query_transformer import QueryPlanner, QueryTransformer
from app.services.redis_io import redis_call

REDIS_DELAY = 3.0  # a "hung" Redis call
GAP_THRESHOLD_MS = 250.0
ELAPSED_LIMIT = 3.0


class SlowFakeRedis:
    """Sync redis stub whose get/setex BLOCK for REDIS_DELAY seconds."""

    def __init__(self, delay: float = REDIS_DELAY):
        self.delay = delay
        self.calls = []

    def ping(self):
        return True

    def get(self, key):
        time.sleep(self.delay)
        self.calls.append(("get", time.monotonic()))
        return None

    def setex(self, key, ttl, value):
        time.sleep(self.delay)
        self.calls.append(("setex", time.monotonic()))
        return True


class FastFakeRedis:
    """Dict-backed fake mirroring test_hyde.FakeRedisCache (+ call log)."""

    def __init__(self):
        self.store = {}
        self.calls = []

    def get(self, key):
        self.calls.append(("get", key))
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.calls.append(("setex", key, ttl))
        self.store[key] = value
        return True


class FailingFakeRedis:
    """Every call raises (Redis down at request time)."""

    def get(self, key):
        raise ConnectionError("redis gone")

    def setex(self, key, ttl, value):
        raise ConnectionError("redis gone")


class Watchdog:
    def __init__(self):
        self.max_gap = 0.0
        self._last = None
        self._stop = asyncio.Event()

    async def run(self):
        self._last = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            if now - self._last > self.max_gap:
                self.max_gap = now - self._last
            self._last = now

    def stop(self):
        self._stop.set()


async def run_with_watchdog(coro_fn):
    wd = Watchdog()
    task = asyncio.create_task(wd.run())
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    try:
        result = await coro_fn()
    finally:
        elapsed = time.perf_counter() - t0
        await asyncio.sleep(0.02)
        wd.stop()
        await task
    return result, wd.max_gap, elapsed


def _mock_llm(return_value='["facet one sub-query", "facet two sub-query"]'):
    client = MagicMock(spec=LLMClient)
    client.model = "check-model"
    client.chat_completion = AsyncMock(return_value=return_value)
    return client


class TestRedisCallUnit:
    @pytest.mark.asyncio
    async def test_returns_value_and_forwards_args(self):
        fake = FastFakeRedis()
        fake.store["k"] = b"value"
        assert await redis_call(fake.get, "k") == b"value"
        await redis_call(fake.setex, "k2", 60, "v2")
        assert fake.store["k2"] == "v2"

    @pytest.mark.asyncio
    async def test_forwards_kwargs(self):
        seen = {}

        def fake_setex(key, ttl=None, value=None):
            seen.update(key=key, ttl=ttl, value=value)
            return True

        await redis_call(fake_setex, "k", ttl=30, value="v")
        assert seen == {"key": "k", "ttl": 30, "value": "v"}

    @pytest.mark.asyncio
    async def test_hung_call_times_out_quickly(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_io_timeout_seconds", 0.2)
        fake = SlowFakeRedis(delay=5.0)
        t0 = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await redis_call(fake.get, "k")
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, f"timeout not bounded: {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_timeout_is_subclass_of_exception(self):
        """The existing cache try/except blocks catch bare Exception — the
        wait_for timeout must degrade to a cache miss, not escape."""
        assert issubclass(asyncio.TimeoutError, Exception)

    @pytest.mark.asyncio
    async def test_redis_error_propagates(self):
        fake = FailingFakeRedis()
        with pytest.raises(ConnectionError):
            await redis_call(fake.get, "k")


class TestQueryPlannerBoundedRedis:
    QUERY = "compare the storage engines and the auth model"

    @pytest.mark.asyncio
    async def test_slow_redis_bounded_and_loop_responsive(
        self, monkeypatch, caplog
    ):
        import logging

        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)
        slow = SlowFakeRedis()

        with patch("redis.from_url", lambda url: slow), caplog.at_level(
            logging.WARNING, logger="app.services.query_transformer"
        ):
            planner = QueryPlanner(_mock_llm())
            assert planner._redis_client is slow

            plan, gap, elapsed = await run_with_watchdog(
                lambda: planner.plan(self.QUERY)
            )

        assert isinstance(plan, list) and self.QUERY in plan
        gap_ms = gap * 1000
        assert gap_ms < GAP_THRESHOLD_MS, (
            f"event loop blocked by redis I/O: max gap {gap_ms:.0f} ms"
        )
        assert elapsed < ELAPSED_LIMIT, (
            f"plan not bounded: elapsed {elapsed:.2f} s"
        )
        # Both cache sites were ATTEMPTED and timed out — the abandoned
        # 3s threads do not finish inside the window (slow.calls stays
        # empty by design), so assert the degradation warnings instead.
        warnings = [r.getMessage() for r in caplog.records]
        assert any("QueryPlanner Redis cache get failed" in w for w in warnings)
        assert any("QueryPlanner Redis cache set failed" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_failing_redis_degrades_to_cache_miss(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)

        with patch("redis.from_url", lambda url: FailingFakeRedis()):
            planner = QueryPlanner(_mock_llm())
            plan = await planner.plan(self.QUERY)

        assert self.QUERY in plan

    @pytest.mark.asyncio
    async def test_fast_fake_redis_cache_still_functions(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
        monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)
        fake = FastFakeRedis()
        llm = _mock_llm()

        with patch("redis.from_url", lambda url: fake):
            planner = QueryPlanner(llm)
            first = await planner.plan(self.QUERY)
            assert llm.chat_completion.call_count == 1
            assert len(fake.store) == 1  # setex stored the plan

            second = await planner.plan(self.QUERY)
            assert second == first
            assert llm.chat_completion.call_count == 1, (
                "redis cache hit must skip the LLM call"
            )


class TestQueryTransformerBoundedRedis:
    QUERY = "What is gradient descent in neural networks?"
    STEP_BACK = "What are the general concepts in machine learning?"

    @pytest.fixture
    def transformer_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", None)
        monkeypatch.setattr(settings, "stepback_enabled", True)
        monkeypatch.setattr(settings, "hyde_enabled", False)
        monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
        monkeypatch.setattr(settings, "chat_model", "check-model")
        monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)

    @pytest.mark.asyncio
    async def test_slow_redis_bounded_and_loop_responsive(
        self, transformer_settings
    ):
        slow = SlowFakeRedis()
        llm = _mock_llm(self.STEP_BACK)
        transformer = QueryTransformer(llm)
        transformer._redis_client = slow

        variants, gap, elapsed = await run_with_watchdog(
            lambda: transformer.transform(self.QUERY)
        )

        assert variants[0] == ("original", self.QUERY)
        assert variants[1] == ("step_back", self.STEP_BACK)
        gap_ms = gap * 1000
        assert gap_ms < GAP_THRESHOLD_MS, (
            f"event loop blocked by redis I/O: max gap {gap_ms:.0f} ms"
        )
        assert elapsed < ELAPSED_LIMIT, (
            f"transform not bounded: elapsed {elapsed:.2f} s"
        )

    @pytest.mark.asyncio
    async def test_slow_redis_hyde_path_bounded(
        self, transformer_settings, monkeypatch
    ):
        """All FOUR optional cache sites in transform() (step-back get/setex,
        HyDE get/setex) are bounded — enable HyDE and shrink the I/O timeout
        so four bounded calls still finish well under the limit."""
        monkeypatch.setattr(settings, "hyde_enabled", True)
        monkeypatch.setattr(settings, "hyde_temperature", 0.0)
        monkeypatch.setattr(settings, "redis_io_timeout_seconds", 0.3)
        slow = SlowFakeRedis()
        llm = _mock_llm(self.STEP_BACK)
        transformer = QueryTransformer(llm)
        transformer._redis_client = slow

        # Step-back first, HyDE passage second.
        llm.chat_completion = AsyncMock(
            side_effect=[self.STEP_BACK, "Hypothetical passage text long enough."]
        )
        variants, gap, elapsed = await run_with_watchdog(
            lambda: transformer.transform(self.QUERY)
        )

        assert [v[0] for v in variants] == ["original", "step_back", "hyde"]
        assert elapsed < ELAPSED_LIMIT, f"elapsed {elapsed:.2f} s"
        assert gap * 1000 < GAP_THRESHOLD_MS

    @pytest.mark.asyncio
    async def test_failing_redis_degrades_to_cache_miss(
        self, transformer_settings
    ):
        llm = _mock_llm(self.STEP_BACK)
        transformer = QueryTransformer(llm)
        transformer._redis_client = FailingFakeRedis()

        variants = await transformer.transform(self.QUERY)
        assert variants[0] == ("original", self.QUERY)
        assert variants[1] == ("step_back", self.STEP_BACK)
