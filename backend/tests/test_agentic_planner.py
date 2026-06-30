"""Tests for agentic_planner.py — AgenticPlanner iterative loop."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agentic_planner import AgenticPlanner, AgenticResult
from app.services.agentic_tools import AgenticTool, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Helper: minimal concrete tools for testing
# ---------------------------------------------------------------------------


class _MockRetrievalTool(AgenticTool):
    """Retrieval tool with configurable execute() result."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result

    @property
    def name(self) -> str:
        return "retrieval"

    @property
    def description(self) -> str:
        return "Mock retrieval tool"

    async def execute(self, **kwargs):
        return self._result


class _MockSynthesisTool(AgenticTool):
    """Synthesis tool with configurable execute() result."""

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


def _make_registry(
    retrieval_result: ToolResult,
    synthesis_result: ToolResult | None = None,
) -> ToolRegistry:
    """Build a pre-populated ToolRegistry with mock tools."""
    reg = ToolRegistry()
    reg.register(_MockRetrievalTool(retrieval_result))
    if synthesis_result is not None:
        reg.register(_MockSynthesisTool(synthesis_result))
    return reg


# ---------------------------------------------------------------------------
# Test: single-round (no LLM → fallback)
# ---------------------------------------------------------------------------

class TestSingleRoundNoLLM:
    """Planner falls back to one retrieval + synthesis when LLM is absent."""

    @pytest.fixture
    def registry(self):
        return _make_registry(
            retrieval_result=ToolResult(
                output="Retrieved 2 sources for query: What is RAG?",
                sources=[{"id": "s1", "snippet": "RAG is retrieval."}, {"id": "s2", "snippet": "RAG uses embeddings."}],
                success=True,
            ),
            synthesis_result=ToolResult(
                output="[synthesized] What is RAG?",
                sources=[],
                success=True,
            ),
        )

    @pytest.mark.asyncio
    async def test_single_round_returns_result_in_one_round(self, registry):
        """Without an LLM the planner does exactly one retrieval round."""
        planner = AgenticPlanner(tool_registry=registry, llm_client=None, max_rounds=3)
        result = await planner.plan_and_execute(query="What is RAG?")

        assert result.rounds == 1
        assert len(result.all_sources) == 2
        assert result.sub_queries_used == []

    @pytest.mark.asyncio
    async def test_single_round_calls_synthesis(self, registry):
        """The synthesis tool is called and its output is returned."""
        planner = AgenticPlanner(tool_registry=registry, llm_client=None, max_rounds=3)
        result = await planner.plan_and_execute(query="What is RAG?")

        assert "[synthesized]" in result.output

    @pytest.mark.asyncio
    async def test_no_retrieval_tool_returns_empty_result(self):
        """When no retrieval tool is registered, an empty result is returned."""
        reg = ToolRegistry()
        reg.register(_MockSynthesisTool(ToolResult(output="never called", success=True)))
        planner = AgenticPlanner(tool_registry=reg, llm_client=None)
        result = await planner.plan_and_execute(query="test")

        assert result.output == ""
        assert result.rounds == 0
        assert result.all_sources == []

    @pytest.mark.asyncio
    async def test_no_synthesis_tool_returns_retrieval_output(self):
        """When synthesis tool is absent, retrieval output is returned as-is."""
        registry = _make_registry(
            retrieval_result=ToolResult(
                output="Retrieved 3 sources",
                sources=[{"id": "s1"}],
                success=True,
            ),
            synthesis_result=None,
        )
        planner = AgenticPlanner(tool_registry=registry, llm_client=None)
        result = await planner.plan_and_execute(query="test")

        assert result.output == "Retrieved 3 sources"
        assert result.rounds == 1


# ---------------------------------------------------------------------------
# Test: multi-round (mock LLM → retrieve_more then synthesize)
# ---------------------------------------------------------------------------

class TestMultiRound:
    """LLM-guided multi-round retrieval loop."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        # First call → ask for more; second call → synthesize
        llm.chat_completion = AsyncMock(
            side_effect=[
                '{"action": "retrieve_more", "sub_query": "deep retrieval: embeddings and vectors"}',
                '{"action": "synthesize"}',
            ]
        )
        return llm

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry()
        # Round 1 sources
        r1_sources = [{"id": "s1", "snippet": "RAG intro"}]
        # Round 2 sources (different)
        r2_sources = [{"id": "s2", "snippet": "vector embeddings"}]

        class _SeqRetrievalTool(AgenticTool):
            _calls: int = 0

            def __init__(self):
                self._calls = 0

            @property
            def name(self) -> str:
                return "retrieval"

            @property
            def description(self) -> str:
                return "Sequential retrieval tool"

            async def execute(self, **kwargs):
                if self._calls == 0:
                    self._calls += 1
                    return ToolResult(
                        output="Round 1 retrieved",
                        sources=r1_sources,
                        success=True,
                    )
                else:
                    return ToolResult(
                        output="Round 2 retrieved",
                        sources=r2_sources,
                        success=True,
                    )

        reg.register(_SeqRetrievalTool())
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized multi-round]", sources=[], success=True)
            )
        )
        return reg

    @pytest.mark.asyncio
    async def test_two_rounds_when_llm_asks_for_more(self, mock_llm, registry):
        """LLM returns retrieve_more → second retrieval → synthesize."""
        planner = AgenticPlanner(
            tool_registry=registry,
            llm_client=mock_llm,
            max_rounds=3,
        )
        result = await planner.plan_and_execute(query="What is RAG?")

        assert result.rounds == 2
        assert result.sub_queries_used == ["deep retrieval: embeddings and vectors"]
        # Sources merged from both rounds
        source_ids = {s["id"] for s in result.all_sources}
        assert source_ids == {"s1", "s2"}
        assert "[synthesized multi-round]" in result.output

    @pytest.mark.asyncio
    async def test_llm_called_twice(self, mock_llm, registry):
        """LLM is called once per round transition."""
        planner = AgenticPlanner(
            tool_registry=registry,
            llm_client=mock_llm,
            max_rounds=3,
        )
        await planner.plan_and_execute(query="What is RAG?")

        assert mock_llm.chat_completion.call_count == 2


# ---------------------------------------------------------------------------
# Test: max_rounds cap
# ---------------------------------------------------------------------------

class TestMaxRoundsCap:
    """Planner stops when max_rounds is reached even if LLM asks for more."""

    @pytest.fixture
    def always_retrieve_llm(self):
        llm = MagicMock()
        llm.chat_completion = AsyncMock(
            return_value='{"action": "retrieve_more", "sub_query": "another query"}'
        )
        return llm

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry()

        call_count = {"n": 0}

        class _CountingRetrievalTool(AgenticTool):
            @property
            def name(self) -> str:
                return "retrieval"

            @property
            def description(self) -> str:
                return "Counting retrieval"

            async def execute(self, **kwargs):
                call_count["n"] += 1
                return ToolResult(
                    output=f"Retrieval round {call_count['n']}",
                    sources=[{"id": f"source_{call_count['n']}"}],
                    success=True,
                )

        reg.register(_CountingRetrievalTool())
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized]", sources=[], success=True)
            )
        )
        # Expose call_count so test can verify
        reg._call_count = call_count
        return reg

    @pytest.mark.asyncio
    async def test_stops_at_max_rounds(self, always_retrieve_llm, registry):
        """When max_rounds=2, exactly 2 retrieval calls are made."""
        planner = AgenticPlanner(
            tool_registry=registry,
            llm_client=always_retrieve_llm,
            max_rounds=2,
        )
        result = await planner.plan_and_execute(query="test query")

        assert result.rounds == 2
        assert registry._call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_result_has_no_more_sub_queries_after_cap(self, always_retrieve_llm, registry):
        """Sub-queries list reflects only the rounds that actually executed."""
        planner = AgenticPlanner(
            tool_registry=registry,
            llm_client=always_retrieve_llm,
            max_rounds=3,
        )
        result = await planner.plan_and_execute(query="test query")

        # max_rounds=3, LLM always says retrieve_more → 3 retrieval rounds
        assert result.rounds == 3
        assert len(result.sub_queries_used) == 2  # first round has no sub_query


# ---------------------------------------------------------------------------
# Test: error tolerance
# ---------------------------------------------------------------------------

class TestErrorTolerance:
    """Planner handles tool failures gracefully."""

    @pytest.mark.asyncio
    async def test_retrieval_failure_continues_to_synthesis(self):
        """Retrieval failure → synthesis is still attempted with empty sources."""
        reg = ToolRegistry()
        reg.register(
            _MockRetrievalTool(
                ToolResult(output="", sources=[], success=False, error="vector store down")
            )
        )
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized from fallback]", sources=[], success=True)
            )
        )

        planner = AgenticPlanner(tool_registry=reg, llm_client=None)
        result = await planner.plan_and_execute(query="test")

        assert result.rounds == 1
        assert result.output == "[synthesized from fallback]"

    @pytest.mark.asyncio
    async def test_synthesis_failure_returns_retrieval_output(self):
        """Synthesis failure → retrieval output is returned as fallback."""
        reg = ToolRegistry()
        reg.register(
            _MockRetrievalTool(
                ToolResult(
                    output="Retrieved OK",
                    sources=[{"id": "s1"}],
                    success=True,
                )
            )
        )
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="", sources=[], success=False, error="LLM timeout")
            )
        )

        planner = AgenticPlanner(tool_registry=reg, llm_client=None)
        result = await planner.plan_and_execute(query="test")

        assert result.output == "Retrieved OK"
        assert result.rounds == 1

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_synthesis(self):
        """LLM unavailable/raises → planner falls back to synthesis without extra rounds."""
        reg = ToolRegistry()
        reg.register(
            _MockRetrievalTool(
                ToolResult(
                    output="First round",
                    sources=[{"id": "s1"}],
                    success=True,
                )
            )
        )
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized fallback]", sources=[], success=True)
            )
        )

        broken_llm = MagicMock()
        broken_llm.chat_completion = AsyncMock(side_effect=RuntimeError("LLM unreachable"))

        planner = AgenticPlanner(tool_registry=reg, llm_client=broken_llm, max_rounds=3)
        result = await planner.plan_and_execute(query="test")

        # Falls back immediately after round 1; no extra retrieval rounds
        assert result.rounds == 1
        assert result.output == "[synthesized fallback]"
        assert result.sub_queries_used == []

    @pytest.mark.asyncio
    async def test_llm_json_parse_error_falls_back(self):
        """LLM returns non-JSON → planner falls back to synthesis."""
        reg = ToolRegistry()
        reg.register(
            _MockRetrievalTool(
                ToolResult(output="First round", sources=[{"id": "s1"}], success=True)
            )
        )
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized after bad JSON]", sources=[], success=True)
            )
        )

        bad_llm = MagicMock()
        bad_llm.chat_completion = AsyncMock(return_value="This is not JSON {{{")

        planner = AgenticPlanner(tool_registry=reg, llm_client=bad_llm)
        result = await planner.plan_and_execute(query="test")

        assert result.rounds == 1
        assert "[synthesized after bad JSON]" in result.output


# ---------------------------------------------------------------------------
# Test: sources merge across rounds
# ---------------------------------------------------------------------------

class TestSourcesMerge:
    """Sources from multiple retrieval rounds are accumulated, not replaced."""

    @pytest.fixture
    def merge_registry(self):
        reg = ToolRegistry()
        round_num = {"n": 0}

        class _MergeRetrievalTool(AgenticTool):
            @property
            def name(self) -> str:
                return "retrieval"

            @property
            def description(self) -> str:
                return "Merge test retrieval"

            async def execute(self, **kwargs):
                round_num["n"] += 1
                return ToolResult(
                    output=f"Round {round_num['n']}",
                    sources=[
                        {"id": f"round{round_num['n']}_a", "snippet": f"sources from round {round_num['n']}"},
                        {"id": f"round{round_num['n']}_b", "snippet": f"more from round {round_num['n']}"},
                    ],
                    success=True,
                )

        reg.register(_MergeRetrievalTool())
        reg.register(
            _MockSynthesisTool(
                ToolResult(output="[synthesized merged]", sources=[], success=True)
            )
        )
        return reg

    @pytest.fixture
    def merge_llm(self):
        llm = MagicMock()
        llm.chat_completion = AsyncMock(
            side_effect=[
                '{"action": "retrieve_more", "sub_query": "more context"}',
                '{"action": "synthesize"}',
            ]
        )
        return llm

    @pytest.mark.asyncio
    async def test_sources_accumulated_across_rounds(self, merge_registry, merge_llm):
        """All sources from all rounds appear in final all_sources."""
        planner = AgenticPlanner(
            tool_registry=merge_registry,
            llm_client=merge_llm,
            max_rounds=3,
        )
        result = await planner.plan_and_execute(query="test")

        # Round 1: 2 sources, Round 2: 2 sources
        assert len(result.all_sources) == 4
        source_ids = {s["id"] for s in result.all_sources}
        assert source_ids == {
            "round1_a", "round1_b",
            "round2_a", "round2_b",
        }

    @pytest.mark.asyncio
    async def test_no_duplicates_in_merged_sources(self, merge_registry, merge_llm):
        """If the same source appears in multiple rounds it is still retained
        (de-duplication is the caller's responsibility; we accumulate)."""
        planner = AgenticPlanner(
            tool_registry=merge_registry,
            llm_client=merge_llm,
            max_rounds=3,
        )
        result = await planner.plan_and_execute(query="test")

        # All 4 source IDs are distinct in this test fixture
        assert len(result.all_sources) == len({s["id"] for s in result.all_sources})


# ---------------------------------------------------------------------------
# Test: AgenticResult dataclass shape
# ---------------------------------------------------------------------------

class TestAgenticResult:
    """Smoke tests for the AgenticResult dataclass."""

    def test_default_fields(self):
        r = AgenticResult(output="hello")
        assert r.output == "hello"
        assert r.all_sources == []
        assert r.rounds == 0
        assert r.sub_queries_used == []

    def test_all_fields(self):
        sources = [{"id": "s1", "snippet": "test"}]
        r = AgenticResult(
            output="final",
            all_sources=sources,
            rounds=3,
            sub_queries_used=["q1", "q2"],
        )
        assert r.output == "final"
        assert r.all_sources == sources
        assert r.rounds == 3
        assert r.sub_queries_used == ["q1", "q2"]
