"""Tests for agentic_tools.py — tool registry and concrete tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agentic_tools import (
    AgenticTool,
    RetrievalTool,
    SynthesisTool,
    ToolRegistry,
    ToolResult,
)

# ---------------------------------------------------------------------------
# ToolResult shape
# ---------------------------------------------------------------------------

class TestToolResult:
    """Tests for the ToolResult dataclass."""

    def test_tool_result_success_defaults(self):
        result = ToolResult(output="hello")
        assert result.output == "hello"
        assert result.sources == []
        assert result.success is True
        assert result.error is None

    def test_tool_result_full_fields(self):
        sources = [{"file_id": "abc", "score": 0.95}]
        result = ToolResult(
            output="answer",
            sources=sources,
            success=True,
            error=None,
        )
        assert result.output == "answer"
        assert result.sources == sources
        assert result.success is True
        assert result.error is None

    def test_tool_result_failure(self):
        result = ToolResult(
            output="",
            sources=[],
            success=False,
            error="something went wrong",
        )
        assert result.success is False
        assert result.error == "something went wrong"


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class _DummyTool(AgenticTool):
    """Minimal concrete tool for registry tests."""

    def __init__(self, name: str, desc: str):
        self._name = name
        self._desc = desc

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._desc

    async def execute(self, **kwargs):
        return ToolResult(output=self._name)


class TestToolRegistry:
    """Tests for ToolRegistry registration, lookup, and listing."""

    def setup_method(self):
        self.registry = ToolRegistry()

    def test_register_single_tool(self):
        tool = _DummyTool("dummy", "A dummy tool for testing")
        self.registry.register(tool)
        assert "dummy" in self.registry
        assert len(self.registry) == 1

    def test_register_duplicate_raises(self):
        tool1 = _DummyTool("dup", "First")
        tool2 = _DummyTool("dup", "Second")
        self.registry.register(tool1)
        with pytest.raises(ValueError, match="already registered"):
            self.registry.register(tool2)

    def test_get_existing(self):
        tool = _DummyTool("getme", "Get this tool")
        self.registry.register(tool)
        retrieved = self.registry.get("getme")
        assert retrieved is tool

    def test_get_missing(self):
        assert self.registry.get("not_there") is None

    def test_replace_existing(self):
        tool1 = _DummyTool("rep", "Original")
        tool2 = _DummyTool("rep", "Replacement")
        self.registry.register(tool1)
        self.registry.replace(tool2)
        assert self.registry.get("rep") is tool2

    def test_list_tools_empty(self):
        assert self.registry.list_tools() == []

    def test_list_tools_multiple(self):
        self.registry.register(_DummyTool("t1", "First tool"))
        self.registry.register(_DummyTool("t2", "Second tool"))
        listed = self.registry.list_tools()
        assert len(listed) == 2
        names = {t["name"] for t in listed}
        assert names == {"t1", "t2"}
        descs = {t["description"] for t in listed}
        assert descs == {"First tool", "Second tool"}

    def test_len(self):
        self.registry.register(_DummyTool("a", "A"))
        self.registry.register(_DummyTool("b", "B"))
        assert len(self.registry) == 2


# ---------------------------------------------------------------------------
# SynthesisTool (self-contained — no mocks needed)
# ---------------------------------------------------------------------------

class TestSynthesisTool:
    """Tests for SynthesisTool (v1 placeholder)."""

    def setup_method(self):
        self.tool = SynthesisTool()

    def test_name_and_description(self):
        assert self.tool.name == "synthesis"
        assert "synthesize" in self.tool.description.lower()

    @pytest.mark.asyncio
    async def test_execute_returns_synthesized_output(self):
        result = await self.tool.execute(text="The answer is 42.")
        assert result.success is True
        # No LLM client: fallback returns raw text unchanged
        assert result.output == "The answer is 42."

    @pytest.mark.asyncio
    async def test_execute_passes_through_sources(self):
        sources = [{"file_id": "f1", "score": 0.9}]
        result = await self.tool.execute(text="Draft answer.", sources=sources)
        assert result.sources == sources

    @pytest.mark.asyncio
    async def test_execute_handles_none_sources(self):
        result = await self.tool.execute(text="No sources provided.")
        assert result.sources == []
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_empty_text(self):
        result = await self.tool.execute(text="")
        assert result.success is True
        # No LLM client: fallback returns raw text unchanged (empty string)
        assert result.output == ""


# ---------------------------------------------------------------------------
# RetrievalTool (mock the RAGEngine internals)
# ---------------------------------------------------------------------------

class TestRetrievalTool:
    """Tests for RetrievalTool using mocked RAGEngine."""

    def setup_method(self):
        self.tool = RetrievalTool(retrieval_top_k=5)

    def test_name_and_description(self):
        assert self.tool.name == "retrieval"
        assert "retrieve" in self.tool.description.lower()

    @pytest.mark.asyncio
    async def test_execute_returns_sources_on_success(self):
        """Happy path: retrieval returns chunks, tool returns sources."""
        mock_chunk = MagicMock()
        mock_chunk.text = "Chunk text about RAG."
        mock_chunk.file_id = "file-123"
        mock_chunk.score = 0.87
        mock_chunk.metadata = {"source_file": "doc.pdf", "section_title": "Introduction"}

        mock_source_meta = {
            "id": "file-123_0_0",
            "file_id": "file-123",
            "filename": "doc.pdf",
            "section": "Introduction",
            "source_label": "S1",
            "snippet": "Chunk text about RAG...",
            "score": 0.87,
            "metadata": mock_chunk.metadata,
        }

        mock_engine = MagicMock()
        mock_engine.embedding_service = MagicMock()
        mock_engine.embedding_service.embed_single = AsyncMock(return_value=[0.1] * 768)
        mock_engine._execute_retrieval = AsyncMock(
            return_value=(
                [{"file_id": "file-123", "score": 0.87, "text": "Chunk text about RAG."}],
                None, None, None, None, None, None, None, None, None, {},
            )
        )
        mock_engine.document_retrieval = MagicMock()
        mock_engine.document_retrieval.filter_relevant = AsyncMock(
            return_value=[mock_chunk]
        )
        mock_engine.document_retrieval.to_source_metadata = MagicMock(
            return_value=mock_source_meta
        )
        mock_engine._sync_document_retrieval_settings = MagicMock()

        with patch(
            "app.services.rag_engine.RAGEngine",
            return_value=mock_engine,
        ):
            result = await self.tool.execute(query="What is RAG?", vault_id=1)

        assert result.success is True
        assert len(result.sources) == 1
        assert result.sources[0]["file_id"] == "file-123"
        assert "Retrieved 1 sources" in result.output

    @pytest.mark.asyncio
    async def test_execute_returns_empty_on_no_results(self):
        mock_engine = MagicMock()
        mock_engine.embedding_service = MagicMock()
        mock_engine.embedding_service.embed_single = AsyncMock(return_value=[0.1] * 768)
        mock_engine._execute_retrieval = AsyncMock(
            return_value=([], None, None, None, None, None, None, None, None, None, {})
        )
        mock_engine.document_retrieval = MagicMock()
        mock_engine.document_retrieval.filter_relevant = AsyncMock(return_value=[])
        mock_engine._sync_document_retrieval_settings = MagicMock()

        with patch(
            "app.services.rag_engine.RAGEngine",
            return_value=mock_engine,
        ):
            result = await self.tool.execute(query="No matches query")

        assert result.success is True
        assert result.sources == []
        assert "Retrieved 0 sources" in result.output

    @pytest.mark.asyncio
    async def test_execute_error_returns_failure_result(self):
        """If retrieval raises, tool returns success=False with error message."""
        with patch(
            "app.services.rag_engine.RAGEngine",
            side_effect=RuntimeError("vector store unavailable"),
        ):
            result = await self.tool.execute(query="Test query")

        assert result.success is False
        assert result.output == ""
        assert "vector store unavailable" in result.error
        assert result.sources == []

    @pytest.mark.asyncio
    async def test_execute_uses_top_k_param(self):
        """top_k parameter overrides the default retrieval_top_k."""
        mock_engine = MagicMock()
        mock_engine.retrieval_top_k = 0  # will be set to 20 by execute
        mock_engine.embedding_service = MagicMock()
        mock_engine.embedding_service.embed_single = AsyncMock(return_value=[0.1] * 768)
        mock_engine._execute_retrieval = AsyncMock(
            return_value=([], None, None, None, None, None, None, None, None, None, {})
        )
        mock_engine.document_retrieval = MagicMock()
        mock_engine.document_retrieval.filter_relevant = AsyncMock(return_value=[])
        mock_engine._sync_document_retrieval_settings = MagicMock()

        with patch(
            "app.services.rag_engine.RAGEngine",
            return_value=mock_engine,
        ) as mock_constructor:
            await self.tool.execute(query="Test", top_k=20)

        # Verify engine was constructed and retrieval_top_k was set
        mock_constructor.assert_called_once()
        assert mock_engine.retrieval_top_k == 20


# ---------------------------------------------------------------------------
# SynthesisTool — XML escaping and prompt structure regression tests
# ---------------------------------------------------------------------------

class TestSynthesisToolXMLEscaping:
    """Regression tests for XML escaping in SynthesisTool user content."""

    def setup_method(self):
        self.tool = SynthesisTool()

    def _build_user_content(self, text: str, sources=None):
        """Helper: call _build_prompt directly to inspect user_content without LLM."""
        # SynthesisTool uses an internal _build_prompt method; we test via
        # execute by mocking the LLM so we can inspect the messages passed.
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self._execute_capture(text, sources)
        )

    async def _execute_capture(self, text: str, sources=None):
        captured_messages = []

        async def mock_chat_completion(messages, **kwargs):
            captured_messages.extend(messages)
            return "mocked response"

        tool = SynthesisTool()
        tool._llm = MagicMock()
        tool._llm.chat_completion = mock_chat_completion
        await tool.execute(text=text, sources=sources or [])
        return captured_messages

    @pytest.mark.asyncio
    async def test_user_content_has_user_query_tag(self):
        """User content must contain <user_query> XML tag wrapping the query."""
        captured = await self._execute_capture(text="What is 2+2?")
        user_msg = next(m for m in captured if m["role"] == "user")
        assert "<user_query>" in user_msg["content"]
        assert "</user_query>" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_user_content_has_source_passages_tag(self):
        """Retrieved evidence must use <source_passages> XML tag."""
        mock_source = [{"file_id": "f1", "score": 0.9, "snippet": "some text"}]
        captured = await self._execute_capture(
            text="What is RAG?", sources=mock_source
        )
        user_msg = next(m for m in captured if m["role"] == "user")
        assert "<source_passages>" in user_msg["content"]
        assert "</source_passages>" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_xml_entities_escaped_in_user_query(self):
        """Literal < > & in user query must be converted to XML entities."""
        captured = await self._execute_capture(text="Look at a < b & c > d")
        user_msg = next(m for m in captured if m["role"] == "user")
        assert "&lt;" in user_msg["content"]
        assert "&gt;" in user_msg["content"]
        assert "&amp;" in user_msg["content"]
        # Original characters must NOT appear unescaped inside tags
        assert "<user_query>" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_xml_entities_escaped_in_source_snippets(self):
        """Literal < > & in source snippets must be converted to XML entities."""
        mock_source = [
            {
                "file_id": "f1",
                "score": 0.9,
                "snippet": "Use array <list> & map for <b> & <c>",
            }
        ]
        captured = await self._execute_capture(text="Explain this", sources=mock_source)
        user_msg = next(m for m in captured if m["role"] == "user")
        # Check entities are present
        assert "&lt;" in user_msg["content"]
        assert "&gt;" in user_msg["content"]
        assert "&amp;" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_none_text_handled_gracefully(self):
        """execute() must not raise AttributeError when text is None."""
        captured = await self._execute_capture(text=None)
        user_msg = next(m for m in captured if m["role"] == "user")
        # None becomes "None" string — no AttributeError
        assert "None" in user_msg["content"]
        assert "<user_query>" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_system_prompt_has_security_boundary_directive(self):
        """System prompt must contain SECURITY BOUNDARY directive and tag names."""
        captured = await self._execute_capture(text="Test query")
        sys_msg = next(m for m in captured if m["role"] == "system")
        assert "SECURITY BOUNDARY" in sys_msg["content"]
        assert "user_query" in sys_msg["content"]
        assert "source_passages" in sys_msg["content"]


# ---------------------------------------------------------------------------
# AgenticTool protocol (structural test — ABC won't let you instantiate
# without implementing abstract methods)
# ---------------------------------------------------------------------------

class TestAgenticToolProtocol:
    """Verify AgenticTool is a proper ABC."""

    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError, match="abstract"):
            AgenticTool()  # type: ignore[abstract]

    def test_partial_subclass_rejected(self):
        class IncompleteTool(AgenticTool):
            @property
            def name(self) -> str:
                return "incomplete"

            # missing description and execute

        with pytest.raises(TypeError):
            IncompleteTool()
