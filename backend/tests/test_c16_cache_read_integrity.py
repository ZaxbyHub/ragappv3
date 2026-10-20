"""Acceptance checks (C16, C3/C9) — Redis cache read integrity.

Freezes the cache-read acceptance criteria for the QueryTransformer
step-back Redis cache:

- C3 (DISCRIMINATING): a raw failure-shaped ``"[]"`` entry stored under
  the CURRENT-VERSION cache key must never be served as an empty
  variant list — one transform() of a normal query must still return at
  least the original variant. RED at the pre-fix base (the truthy raw
  string is json.loads'd to ``[]`` and served as-is, so transform
  returns an empty list).
- C9 (PRESERVING): a LEGACY versionless cache key (the pre-#711 keying
  scheme: md5 over the same payload WITHOUT the ``version`` field) must
  MISS — a transform() must still go to the LLM exactly once. GREEN at
  the base (current keys are content-addressed with PROMPT_VERSION, so
  the legacy digest never collides).

Fake-redis and settings fixture shapes follow
test_c06_stepback_failure_cache.py.
"""

import hashlib
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
    """Same settings shape as test_c06_stepback_failure_cache.py."""
    monkeypatch.setattr(settings, "redis_url", "redis://check")
    monkeypatch.setattr(settings, "stepback_enabled", True)
    monkeypatch.setattr(settings, "hyde_enabled", False)
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.mark.asyncio
async def test_raw_empty_entry_at_current_key_is_never_served(
    transformer_settings,
):
    """C3: a raw ``[]`` entry under the CURRENT-version key must not be
    served as an empty variant list."""
    fake = FastFakeRedis()
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)

    with patch("redis.from_url", return_value=fake):
        transformer = QueryTransformer(llm)
        assert transformer._redis_client is fake

        cache_key = transformer._make_cache_key(
            transformer._cache_model, "step_back", QUERY
        )
        # The raw failure shape: a JSON-empty list stored as the raw string.
        fake.store[cache_key] = "[]"

        variants = await transformer.transform(QUERY)

    kinds = [kind for kind, _ in variants]
    assert kinds == ["original"], (
        "a raw '[]' entry under the current-version cache key must be "
        "treated as a cache MISS (or repaired), never served as an empty "
        f"variant list; transform returned {kinds!r}"
    )


@pytest.mark.asyncio
async def test_legacy_versionless_cache_key_is_a_miss(transformer_settings):
    """C9: a legacy versionless key must MISS — one LLM call, not zero."""
    fake = FastFakeRedis()
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)

    with patch("redis.from_url", return_value=fake):
        transformer = QueryTransformer(llm)
        assert transformer._redis_client is fake
        assert transformer._cache_model == "check-model"

        # Pre-#711 keying scheme: same payload WITHOUT the "version" field.
        legacy_payload = json.dumps(
            {"model": "check-model", "type": "step_back", "query": QUERY},
            sort_keys=True,
        )
        legacy_key = (
            "query_transform:"
            + hashlib.md5(
                legacy_payload.encode(), usedforsecurity=False
            ).hexdigest()
        )
        # The pre-#711 failure shape this entry used to carry.
        fake.store[legacy_key] = json.dumps([["original", QUERY]])

        variants = await transformer.transform(QUERY)

    assert llm.chat_completion.call_count == 1, (
        "a legacy versionless cache entry must MISS: a hit would serve the "
        "stale pre-#711 entry with zero LLM calls, but exactly one LLM "
        "call was expected"
    )
    kinds = [kind for kind, _ in variants]
    assert kinds == ["original", "step_back"], (
        f"after the legacy-key miss the transform must run normally; got {kinds!r}"
    )
