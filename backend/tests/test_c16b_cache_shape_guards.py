"""Guardrail tests for the cached-payload shape guards (#918/#924 class).

Non-frozen companions to the frozen c16 checks: they pin the planner-side
and transformer-side shape guards' behavior beyond the frozen AC vectors —
a wrong-shape Redis payload (dict, string, or empty list) is discarded and
best-effort deleted, never served, on BOTH cache read paths.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.query_transformer import QueryPlanner, QueryTransformer

QUERY = "What is gradient descent in neural networks?"
# Planner-side queries must FAIL _is_simple_query or plan() returns before
# the cache is ever consulted (same discriminator as the frozen C8 check).
PLAN_QUERY = "compare pricing tiers and reliability guarantees across vendors together and separately"
STEP_BACK = "What are the general concepts in machine learning?"
PLAN_JSON = '["facet one about pricing", "facet two about reliability"]'


class FakeRedis:
    """Dict-backed fake with get/setex/delete and a call log."""

    def __init__(self, store):
        self.store = store
        self.deleted = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1


def _transformer(fake, llm):
    with patch("redis.from_url", return_value=fake):
        return QueryTransformer(llm)


def _planner(fake, llm):
    with patch("redis.from_url", return_value=fake):
        return QueryPlanner(llm)


@pytest.fixture
def transformer_settings(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://check")
    monkeypatch.setattr(settings, "stepback_enabled", True)
    monkeypatch.setattr(settings, "hyde_enabled", False)
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.fixture
def planner_settings(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://check")
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"not": "a list"}',  # dict shape
        '"a bare string"',  # string shape
        "[]",  # empty list (the frozen C3 vector, here on the delete path)
        '[["original"]]',  # wrong tuple arity
        '[[1, 2]]',  # non-string entries
    ],
)
async def test_transformer_discards_wrong_shape_cache_entries(
    transformer_settings, payload
):
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)
    fake = FakeRedis({})
    transformer = _transformer(fake, llm)
    key = transformer._make_cache_key(transformer._cache_model, "step_back", QUERY)
    fake.store[key] = payload

    variants = await transformer.transform(QUERY)

    kinds = [kind for kind, _ in variants]
    assert kinds == ["original"]
    assert llm.chat_completion.call_count == 0, (
        "a wrong-shape entry must be answered from the original-only arm "
        "without spending an LLM call"
    )
    assert key in fake.deleted, "the corrupt entry must be best-effort deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"not": "a list"}',
        '"a bare string"',
        "[]",
        '["ok", 7]',
    ],
)
async def test_planner_discards_wrong_shape_cache_entries(
    planner_settings, payload
):
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=PLAN_JSON)
    fake = FakeRedis({})
    planner = _planner(fake, llm)
    key = planner._make_cache_key(PLAN_QUERY)
    fake.store[key] = payload

    plan = await planner.plan(PLAN_QUERY)

    # The miss falls through to the LLM and produces a real plan.
    assert llm.chat_completion.call_count == 1
    assert isinstance(plan, list) and plan and all(isinstance(sq, str) for sq in plan)
    assert key in fake.deleted, "the corrupt entry must be best-effort deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"not": "a string"}',  # dict shape — would be appended as the passage
        "[]",  # empty list — self-heals by truthiness today; pin the guard
        "123",  # bare number
    ],
)
async def test_hyde_cache_discards_wrong_shape_entries(
    transformer_settings, monkeypatch, payload
):
    """The HyDE Redis hit path validates payload shape (#918 class, HyDE arm)."""
    monkeypatch.setattr(settings, "hyde_enabled", True)
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(
        side_effect=[
            "What are the general concepts?",
            "A short factual passage about the concept.",
        ]
    )
    fake = FakeRedis({})
    transformer = _transformer(fake, llm)
    hyde_key = transformer._make_cache_key(transformer._cache_model, "hyde", QUERY)
    fake.store[hyde_key] = payload

    variants = await transformer.transform(QUERY)

    kinds = [kind for kind, _ in variants]
    assert kinds == ["original", "step_back", "hyde"], (
        "a corrupt HyDE entry must miss and regenerate, never serve its raw shape"
    )
    hyde_texts = [text for kind, text in variants if kind == "hyde"]
    assert isinstance(hyde_texts[0], str)
    assert hyde_key in fake.deleted, "the corrupt HyDE entry must be best-effort deleted"


@pytest.mark.asyncio
async def test_planner_valid_hit_is_served_as_a_copy(planner_settings):
    """A VALID cached plan is served (no LLM call) and as a copy."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=PLAN_JSON)
    fake = FakeRedis({})
    planner = _planner(fake, llm)
    key = planner._make_cache_key(PLAN_QUERY)
    fake.store[key] = json.dumps(["alpha facet", "beta facet"])

    plan1 = await planner.plan(PLAN_QUERY)
    assert llm.chat_completion.call_count == 0, "a valid hit must not call the LLM"
    plan1.append("POISONED")
    plan2 = await planner.plan(PLAN_QUERY)
    assert "POISONED" not in plan2
    assert plan2 == ["alpha facet", "beta facet"]


@pytest.mark.asyncio
async def test_planner_lru_hit_path_returns_a_copy(planner_settings):
    """The LRU-HIT return itself is a copy (implementation-review P3).

    The planner's Redis hit returns early without warming the LRU, so the
    LRU-hit arm is reached only when a plan was cached by the LLM path and
    Redis then misses. Mutating a plan returned from the LRU hit must not
    poison the next call.
    """
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=PLAN_JSON)
    fake = FakeRedis({})
    planner = _planner(fake, llm)
    key = planner._make_cache_key(PLAN_QUERY)

    # Call 1: LLM path — populates both the fake Redis and the LRU.
    first = await planner.plan(PLAN_QUERY)
    assert llm.chat_completion.call_count == 1
    # Make Redis miss from here on so calls 2 and 3 take the LRU-hit path.
    fake.store.clear()

    second = await planner.plan(PLAN_QUERY)
    assert llm.chat_completion.call_count == 1, "call 2 must be an LRU hit"
    second.append("POISONED-LRU")
    third = await planner.plan(PLAN_QUERY)
    assert "POISONED-LRU" not in third, (
        "the LRU-hit return must be a copy: a caller mutation must not "
        "poison the cached plan for later calls"
    )
    assert third == first
