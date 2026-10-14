"""Companion regression tests for the #711 fix (beyond the frozen AC checks).

The frozen acceptance checks pin the #711 contracts at issue granularity;
these companion tests pin the adjacent branches the fix introduces so they
cannot silently regress:

- the planner's single-facet echo maps to the ORIGINAL query (identity, not
  just plan length) and is cached like any successful plan;
- a non-echo single-item (rule-3) answer also maps to ``[query]`` and is
  cached;
- a multi-item response containing an escaped echo is deduped and the
  original prepended exactly once;
- an empty/whitespace step-back response (like an LLM exception) is not
  cached and the next call retries the LLM;
- the follow-up rewrite SECURITY BOUNDARY names both tags it emits;
- a successful step-back transform is still written to Redis;
- ``_normalize_sub_query`` cannot merge interior-punctuation-distinct facets.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.query_transformer import (
    QueryPlanner,
    QueryTransformer,
    _normalize_sub_query,
)

_SINGLE_FACET_MARKER = "If the question has only one facet, respond with:\n"

QUERY = "What's the company's travel reimbursement policy and how does it apply to contractors abroad?"

STEP_BACK = "What are the general concepts in machine learning?"

TRANSFORM_QUERY = "What is gradient descent in neural networks?"


class FakeRedis:
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
def service_settings(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "stepback_enabled", True)
    monkeypatch.setattr(settings, "hyde_enabled", False)
    monkeypatch.setattr(settings, "query_transform_temperature", 0.0)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


def _echoing_llm():
    """LLM that complies with the planner prompt: returns the single-facet
    example exactly as the prompt rendered it (escaped)."""

    async def fake_chat_completion(*args, **kwargs):
        messages = kwargs.get("messages") or args[0]
        user_content = messages[1]["content"]
        start = user_content.index(_SINGLE_FACET_MARKER) + len(_SINGLE_FACET_MARKER)
        return user_content[start:]

    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(side_effect=fake_chat_completion)
    return llm


@pytest.mark.asyncio
async def test_single_facet_echo_plan_is_exactly_the_original_query(monkeypatch):
    """Echoing the prompt's example must yield [original] — identity, not just length.

    Guards against a fix that returns the escaped echo string (entity-laden
    text would then flow into retrieval while the plan length still looks
    correct).
    """
    monkeypatch.setattr(settings, "redis_url", None)
    planner = QueryPlanner(_echoing_llm())
    plan = await planner.plan(QUERY)
    assert plan == [QUERY]


@pytest.mark.asyncio
async def test_non_echo_single_item_maps_to_original_and_is_cached():
    """A reworded one-facet answer is the rule-3 outcome: [original], cached.

    The plan must be the original query (rule 3 says "return ONLY the original
    question as-is"), and — being a successful plan transform — the next call
    for the same query is served from cache without a second LLM call.
    """
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(
        return_value='["What is the travel reimbursement policy for contractors abroad?"]'
    )
    planner = QueryPlanner(llm)

    first = await planner.plan(QUERY)
    assert first == [QUERY]

    second = await planner.plan(QUERY)
    assert second == [QUERY]
    assert llm.chat_completion.call_count == 1


@pytest.mark.asyncio
async def test_multi_item_response_with_echo_dedupes_and_prepends_once(monkeypatch):
    """An escaped echo among real sub-queries is dropped; original prepended once."""
    monkeypatch.setattr(settings, "redis_url", None)
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(
        return_value=json.dumps(
            [
                "What&#x27;s the company&#x27;s travel reimbursement policy and "
                "how does it apply to contractors abroad?",
                "travel reimbursement rules for foreign contractors",
            ]
        )
    )
    planner = QueryPlanner(llm)

    plan = await planner.plan(QUERY)
    assert plan == [QUERY, "travel reimbursement rules for foreign contractors"]
    assert len(plan) == plan.count(QUERY) + 1  # original appears exactly once


@pytest.mark.asyncio
async def test_empty_stepback_response_is_not_cached_and_retries(service_settings):
    """A whitespace-only step-back response is a failure: not cached, retried."""
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(side_effect=["   ", STEP_BACK])
    transformer = QueryTransformer(llm)

    first = await transformer.transform(TRANSFORM_QUERY)
    second = await transformer.transform(TRANSFORM_QUERY)

    assert [kind for kind, _ in first] == ["original"]
    assert any(kind == "step_back" for kind, _ in second)
    assert llm.chat_completion.call_count == 2


@pytest.mark.asyncio
async def test_followup_boundary_names_both_emitted_tags():
    """The follow-up SECURITY BOUNDARY must name <user_query> AND <assistant_answer>."""
    mock_llm = MagicMock()
    mock_llm.chat_completion = AsyncMock(return_value="standalone question")
    with patch("app.services.query_transformer.settings") as mock_settings:
        mock_settings.redis_url = None
        mock_settings.chat_model = "test-model"
        mock_settings.query_transform_temperature = 0.0
        transformer = QueryTransformer(llm_client=mock_llm)
        await transformer.rewrite_followup(
            query="try again",
            chat_history=[
                {"role": "user", "content": "What is the refund policy?"},
                {"role": "assistant", "content": "Refunds take 5 days."},
            ],
        )
    system_prompt = mock_llm.chat_completion.call_args[1]["messages"][0]["content"]
    boundary = system_prompt[system_prompt.index("SECURITY BOUNDARY"):]
    assert "<user_query>" in boundary
    assert "<assistant_answer>" in boundary


@pytest.mark.asyncio
async def test_successful_stepback_still_written_to_redis(
    service_settings, monkeypatch
):
    """The positive counterpart to AC8: a cold SUCCESS still setex'es once."""
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    fake = FakeRedis()
    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(return_value=STEP_BACK)

    with patch("redis.from_url", return_value=fake):
        transformer = QueryTransformer(llm)
        variants = await transformer.transform(TRANSFORM_QUERY)

    assert [kind for kind, _ in variants] == ["original", "step_back"]
    setex_calls = [c for c in fake.calls if c[0] == "setex"]
    assert len(setex_calls) == 1
    cached = json.loads(fake.store[setex_calls[0][1]])
    assert cached == [["original", TRANSFORM_QUERY], ["step_back", STEP_BACK]]


def test_normalize_sub_query_no_interior_punctuation_collisions():
    """Normalization must never merge interior-punctuation-distinct facets."""
    assert _normalize_sub_query("C++") != _normalize_sub_query("C")
    assert _normalize_sub_query("plan A") != _normalize_sub_query("planA")
    # Interior punctuation is preserved verbatim (end-anchored strip only):
    # these pairs differ ONLY at interior positions, so they must stay
    # distinct — an unanchored punctuation strip would merge them.
    assert _normalize_sub_query("plan A. plan B") != _normalize_sub_query(
        "plan A plan B"
    )
    assert _normalize_sub_query("speed: fast, cost: low") == (
        "speed: fast, cost: low"
    )
    # The echo comparison it exists for: escaped vs raw, case and trailing
    # punctuation variants all compare equal to the original.
    assert _normalize_sub_query(
        "What&#x27;s the company&#x27;s travel policy?"
    ) == _normalize_sub_query("what's the company's travel policy")
    assert _normalize_sub_query("storage engines") == _normalize_sub_query(
        "Storage Engines!"
    )
