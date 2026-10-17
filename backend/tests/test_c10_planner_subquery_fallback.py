"""Acceptance check for issue #715 finding T1-35-K-13 (C9).

Desired post-fix behavior (RED on current master):

When the planner LLM's decision is exactly ``{"action": "retrieve_more"}``
(valid JSON, NO ``sub_query`` key), the follow-up retrieval must be called
with ``query == the current query`` (the original question), not ``''``.

Today ``AgenticPlanner._decide_next_action`` always returns a ``sub_query``
key (``parsed.get("sub_query", "")`` — agentic_planner.py), so
``decision.get("sub_query", current_query)`` at the plan_and_execute call
site finds the key present with value ``''`` and retrieval runs with an
empty query — a wasted round that can only return noise.

The mock-tool / registry / sequential-decision-LLM patterns are copied from
``TestMultiRound`` in tests/test_agentic_planner.py.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.agentic_planner import AgenticPlanner
from app.services.agentic_tools import AgenticTool, ToolRegistry, ToolResult


class _RecordingRetrievalTool(AgenticTool):
    """Retrieval mock (pattern of _SeqRetrievalTool in
    tests/test_agentic_planner.py::TestMultiRound) that records every query
    it is executed with."""

    def __init__(self):
        self.queries = []

    @property
    def name(self) -> str:
        return "retrieval"

    @property
    def description(self) -> str:
        return "Recording retrieval tool"

    async def execute(self, **kwargs):
        self.queries.append(kwargs.get("query"))
        return ToolResult(
            output=f"Retrieved round {len(self.queries)}",
            sources=[{"id": f"s{len(self.queries)}", "snippet": "text"}],
            success=True,
        )


class _MockSynthesisTool(AgenticTool):
    """Synthesis mock copied from tests/test_agentic_planner.py."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    @property
    def name(self) -> str:
        return "synthesis"

    @property
    def description(self) -> str:
        return "Mock synthesis tool"

    async def execute(self, **kwargs):
        return self._result


@pytest.mark.asyncio
async def test_missing_sub_query_falls_back_to_current_query():
    """C9: missing sub_query key must fall back to the current query.

    Module-level test function so the frozen acceptance-check argv
    ``tests/test_c10_planner_subquery_fallback.py::test_missing_sub_query_falls_back_to_current_query``
    resolves verbatim (class-qualified ids would not).
    """
    # Decision 1 (after round 1): retrieve_more with NO sub_query key —
    # valid JSON, exactly the finding's trigger. Decision 2: synthesize,
    # so the loop terminates after the second retrieval round.
    llm = MagicMock()
    llm.chat_completion = AsyncMock(
        side_effect=[
            '{"action": "retrieve_more"}',
            '{"action": "synthesize"}',
        ]
    )

    retrieval = _RecordingRetrievalTool()
    reg = ToolRegistry()
    reg.register(retrieval)
    reg.register(
        _MockSynthesisTool(ToolResult(output="[synthesized]", success=True))
    )

    planner = AgenticPlanner(tool_registry=reg, llm_client=llm, max_rounds=3)
    await planner.plan_and_execute(query="original question")

    # retrieval.queries[0] is the round-1 initial call — the planner
    # always retrieves with the original query first, before any LLM
    # decision. retrieval.queries[1] is the retrieve_more-triggered call
    # (the decision carried no sub_query key) — the record this
    # acceptance check targets: it must fall back to the current query.
    assert len(retrieval.queries) == 2
    assert retrieval.queries[1] == "original question"
