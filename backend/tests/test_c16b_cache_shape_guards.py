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
        self.calls = []

    def get(self, key):
        self.calls.append(("get", key))
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.calls.append(("setex", key, ttl))
        self.store[key] = value
        return True

    def delete(self, key):
        self.calls.append(("delete", key))
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
async def test_planner_redis_hit_serves_without_llm(planner_settings):
    """A VALID cached plan is served with zero LLM calls (renamed per review
    F-010: the Redis-hit path returns a fresh json.loads object, so this
    test never exercised copy semantics — the LRU-hit copy pin lives in
    test_planner_lru_hit_path_returns_a_copy). The seed starts with the
    caller's own query because that is the only shape plan() writes."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=PLAN_JSON)
    fake = FakeRedis({})
    planner = _planner(fake, llm)
    key = planner._make_cache_key(PLAN_QUERY)
    fake.store[key] = json.dumps([PLAN_QUERY, "alpha facet", "beta facet"])

    plan1 = await planner.plan(PLAN_QUERY)
    assert llm.chat_completion.call_count == 0, "a valid hit must not call the LLM"
    assert plan1 == [PLAN_QUERY, "alpha facet", "beta facet"]


@pytest.mark.asyncio
async def test_planner_discards_original_less_plan(planner_settings):
    """A shape-valid plan whose first element is not the caller's query is
    corrupt (review PRR-006/F-003): plan() always prepends the original, so
    such a payload can only be a poisoned or foreign write."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=PLAN_JSON)
    fake = FakeRedis({})
    planner = _planner(fake, llm)
    key = planner._make_cache_key(PLAN_QUERY)
    fake.store[key] = json.dumps(["sub-query A", "sub-query B"])

    plan = await planner.plan(PLAN_QUERY)

    assert llm.chat_completion.call_count == 1, "an original-less plan must miss"
    assert key in fake.deleted
    assert plan[0] == PLAN_QUERY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '[["step_back", "broader question"]]',  # dropped the original arm
        '[["original", "a different query entirely"], ["step_back", "s"]]',
        'not json at all {',  # unparseable (review PRR-008)
    ],
)
async def test_transformer_discards_payloads_missing_the_original_arm(
    transformer_settings, payload
):
    """The servability guard requires the caller's ('original', query) arm
    first — a shape-valid-but-original-less (or unparseable) payload is
    discarded, best-effort deleted, and answered with the original-only arm
    without an LLM spend."""
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
        "no LLM spend on a poisoned key: the corrupt arm degrades to the "
        "original-only arm"
    )
    assert key in fake.deleted


@pytest.mark.asyncio
async def test_corrupt_step_back_entry_still_appends_hyde(transformer_settings,
                                                            monkeypatch):
    """A corrupt step-back entry falls through to the HyDE block like the
    step-back-FAILURE arm does (review PRR-002/F-002): with hyde_enabled a
    corrupt hit returns [original, hyde], not [original] alone."""
    monkeypatch.setattr(settings, "hyde_enabled", True)
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(
        side_effect=[
            "A short factual passage about the concept.",  # HyDE call only
        ]
    )
    fake = FakeRedis({})
    transformer = _transformer(fake, llm)
    key = transformer._make_cache_key(transformer._cache_model, "step_back", QUERY)
    fake.store[key] = "[]"

    variants = await transformer.transform(QUERY)

    kinds = [kind for kind, _ in variants]
    assert kinds == ["original", "hyde"], (
        "the corrupt arm must still run the HyDE block (fall-through, like "
        f"the failure arm); got {kinds!r}"
    )
    assert llm.chat_completion.call_count == 1, "only the HyDE call, no step-back"
    assert key in fake.deleted


@pytest.mark.asyncio
async def test_corrupt_discard_performs_no_cache_write(transformer_settings):
    """The discard arm must never write a negative entry (the
    DECISION(no-negative-cache) contract on the NEW path, review PRR-018):
    after the corrupt transform, the key is absent from Redis AND the LRU,
    so the next call retries cleanly."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)
    fake = FakeRedis({})
    transformer = _transformer(fake, llm)
    key = transformer._make_cache_key(transformer._cache_model, "step_back", QUERY)
    fake.store[key] = "[]"

    await transformer.transform(QUERY)

    assert key not in fake.store, "no cache write may follow a corrupt discard"
    assert all(call[0] != "setex" for call in fake.calls), (
        "the discard arm must not issue setex (no-negative-cache)"
    )
    assert key not in transformer._lru_cache, "the LRU must not be poisoned either"


@pytest.mark.asyncio
async def test_corrupt_entry_self_heals_on_next_call(transformer_settings):
    """After the corrupt discard (delete, original-only), the NEXT transform
    regenerates via the LLM and rewrites a valid entry (review PRR-027l)."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)
    fake = FakeRedis({})
    transformer = _transformer(fake, llm)
    key = transformer._make_cache_key(transformer._cache_model, "step_back", QUERY)
    fake.store[key] = "[]"

    first = await transformer.transform(QUERY)
    assert [k for k, _ in first] == ["original"]
    assert llm.chat_completion.call_count == 0

    second = await transformer.transform(QUERY)
    assert [k for k, _ in second] == ["original", "step_back"]
    assert llm.chat_completion.call_count == 1
    assert key in fake.store, "the next call must rewrite a valid entry (self-heal)"


@pytest.mark.asyncio
async def test_delete_failure_degrades_gracefully(transformer_settings):
    """A raising best-effort delete must not break the request (review
    PRR-027m): the corrupt arm still answers original-only, and the
    per-site label appears in the warning."""
    import logging

    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)

    class NoDeleteRedis(FakeRedis):
        def delete(self, key):
            raise RuntimeError("delete boom")

    fake = NoDeleteRedis({})
    transformer = _transformer(fake, llm)
    key = transformer._make_cache_key(transformer._cache_model, "step_back", QUERY)
    fake.store[key] = "[]"

    with caplog_context() as records:
        variants = await transformer.transform(QUERY)

    assert [k for k, _ in variants] == ["original"]
    assert llm.chat_completion.call_count == 0
    assert any(
        "Redis cache delete failed" in r.getMessage()
        for r in records
        if r.levelno >= logging.WARNING
    )


def caplog_context():
    """Minimal caplog shim: capture warning records from the module logger."""
    import logging

    records = []
    collector = logging.Handler()
    collector.emit = lambda record: records.append(record)

    class _Ctx:
        def __enter__(self):
            logging.getLogger("app.services.query_transformer").addHandler(collector)
            return records

        def __exit__(self, *exc):
            logging.getLogger("app.services.query_transformer").removeHandler(collector)
            return False

    return _Ctx()


@pytest.mark.asyncio
async def test_hyde_cache_discards_whitespace_passage(transformer_settings,
                                                       monkeypatch):
    """A whitespace-only cached HyDE passage is corrupt (review PRR-007):
    generate_hyde can never write one (min 20 chars after strip), so it
    must be discarded and regenerated, not served verbatim."""
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
    fake.store[hyde_key] = '"   "'

    variants = await transformer.transform(QUERY)

    hyde_texts = [text for kind, text in variants if kind == "hyde"]
    assert hyde_texts == ["A short factual passage about the concept."], (
        "a whitespace-only cached passage must be regenerated, never served"
    )
    assert hyde_key in fake.deleted


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
