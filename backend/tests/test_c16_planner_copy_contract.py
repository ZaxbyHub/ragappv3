"""Acceptance check (C16, C8) — planner cache must hand out copies.

Freezes the acceptance criterion that a caller mutating a previously
returned plan must not poison the planner cache:

- C8 (DISCRIMINATING): after ``plan1 = await planner.plan(QUERY)`` and a
  caller-side ``plan1.append("POISONED-BY-CALLER")``, a second
  ``await planner.plan(QUERY)`` must return the un-poisoned plan. At the
  pre-fix base the LRU hit returns the very same list object the caller
  mutated, so the poison leaks through (RED).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services.query_transformer import (
    QueryPlanner,
    _is_simple_query,
)

QUERY = (
    "compare pricing tiers and reliability guarantees across vendors "
    "together and separately"
)


@pytest.fixture
def planner_settings(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "chat_model", "check-model")
    monkeypatch.setattr(settings, "query_transform_cache_ttl_sec", 3600)


@pytest.mark.asyncio
async def test_planner_cache_hit_returns_a_copy_not_the_cached_list(
    planner_settings,
):
    """C8: mutating a returned plan must not poison the cache."""
    assert _is_simple_query(QUERY) is False, (
        "test premise: QUERY must fail the simple-query heuristic so the "
        "planner reaches its LLM decomposition path"
    )

    llm = MagicMock()
    llm.model = "check-model"
    llm.chat_completion = AsyncMock(
        return_value=json.dumps(
            [
                "What pricing tiers do vendors offer",
                "What reliability guarantees do vendors offer",
            ]
        )
    )
    planner = QueryPlanner(llm)

    plan1 = await planner.plan(QUERY)
    snapshot = list(plan1)

    # Caller-side mutation of the returned list object.
    plan1.append("POISONED-BY-CALLER")

    plan2 = await planner.plan(QUERY)

    assert plan2 == snapshot, (
        "the planner cache must hand out a COPY on every hit: the second "
        f"plan() returned the caller-mutated cached list {plan2!r} instead "
        f"of the original plan {snapshot!r}"
    )
    assert "POISONED-BY-CALLER" not in plan2, (
        "a caller mutating a previously returned plan must not poison the "
        "planner cache for subsequent identical queries"
    )
