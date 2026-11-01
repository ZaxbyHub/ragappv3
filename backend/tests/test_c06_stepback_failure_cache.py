"""Acceptance checks for issue #711 (C-06) — step-back caching contracts.

Freezes the #711 acceptance criteria for the QueryTransformer step-back
cache:

- AC7: a FAILED step-back transformation must not be cached — the next call
  for the same query must retry the LLM instead of serving the
  failure-shaped (original-only) variants from cache. RED at the #711 base
  (the failure result lands in the LRU cache and the second call never
  reaches the LLM), GREEN once #711 lands.
- AC8: a Redis cache HIT must not re-issue setex — the entry already exists
  with its original TTL; refreshing it on every hit both defeats the TTL and
  adds needless Redis I/O. RED at the #711 base (transform() setex'es even
  on a hit), GREEN once #711 lands.

Settings fixture shape and fake Redis follow test_redis_bounded_io.py.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.query_transformer import QueryTransformer

QUERY = "What is gradient descent in neural networks?"
STEP_BACK = "What are the general concepts in machine learning?"


class FastFakeRedis:
    """Dict-backed fake with get/setex and a call log."""

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


@pytest.fixture
def transformer_settings(monkeypatch):
    """TestQueryTransformerBoundedRedis-style settings for transform()."""
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "stepback_enabled", True)
    monkeypatch.setattr(settings, "hyde_enabled", False)
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.mark.asyncio
async def test_failed_stepback_is_not_cached(transformer_settings):
    """AC7: an LLM failure on step-back must not populate the cache."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(side_effect=[RuntimeError("transient"), STEP_BACK])
    transformer = QueryTransformer(llm)

    await transformer.transform(QUERY)
    second = await transformer.transform(QUERY)

    has_step_back = any(kind == "step_back" for kind, _ in second)
    assert has_step_back is True, (
        "the second transform must retry the LLM (transient failure was "
        "cached) and produce a step-back variant"
    )
    assert llm.chat_completion.call_count == 2


@pytest.mark.asyncio
async def test_redis_hit_does_not_refresh_ttl(
    transformer_settings, monkeypatch
):
    """AC8: a Redis cache hit must not re-write (setex) the cached entry."""
    monkeypatch.setattr(settings, "redis_url", "redis://fake")

    fake = FastFakeRedis()
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value="should never be called")

    with patch("redis.from_url", return_value=fake):
        transformer = QueryTransformer(llm)
        assert transformer._redis_client is fake

        cache_key = transformer._make_cache_key(
            transformer._cache_model, "step_back", QUERY
        )
        fake.store[cache_key] = json.dumps(
            [["original", QUERY], ["step_back", "broader"]]
        )

        variants = await transformer.transform(QUERY)

    assert [kind for kind, _ in variants] == ["original", "step_back"]
    setex_calls = len([c for c in fake.calls if c[0] == "setex"])
    assert setex_calls == 0, "a cache hit must not refresh the cached entry"
