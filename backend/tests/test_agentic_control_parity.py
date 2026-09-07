"""Agentic-path control parity (issue #510, reviewer Phase 4.5 finding).

The agentic early-return must honor the same per-query controls as the
standard pipeline: citation_mode reaches the SynthesisTool prompt, and the
agentic done payload carries citation_enforcement / currency_warnings.
"""

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conftest  # noqa: F401,E402
from app.services.agentic_tools import SynthesisTool, ToolResult


class _RecordingClient:
    def __init__(self, response="The answer is 42. [S1]"):
        self.response = response
        self.calls = []

    async def chat_completion(self, messages, temperature=0.7, max_tokens=None):
        self.calls.append({"messages": messages})
        return self.response


class TestSynthesisToolCitationMode(unittest.TestCase):
    def _system_prompt(self, tool, sources):
        result = asyncio.run(
            tool.execute(text="q", sources=sources)
        )
        self.assertTrue(result.success)
        return tool._llm.calls[-1]["messages"][0]["content"]

    def test_default_keeps_citation_instruction(self):
        client = _RecordingClient()
        tool = SynthesisTool(llm_client=client)
        prompt = self._system_prompt(tool, [{"snippet": "evidence text"}])
        self.assertIn("Cite sources using their [S#] label", prompt)

    def test_disabled_removes_citation_instruction(self):
        client = _RecordingClient()
        tool = SynthesisTool(llm_client=client, citation_mode="disabled")
        prompt = self._system_prompt(tool, [{"snippet": "evidence text"}])
        self.assertNotIn("Cite sources using their [S#] label", prompt)
        user_msg = client.calls[-1]["messages"][1]["content"]
        self.assertIn("Do not include bracketed citation labels", user_msg)

    def test_required_strengthens_instruction(self):
        client = _RecordingClient()
        tool = SynthesisTool(llm_client=client, citation_mode="required")
        prompt = self._system_prompt(tool, [{"snippet": "evidence text"}])
        user_msg = client.calls[-1]["messages"][1]["content"]
        self.assertIn("Citations are REQUIRED", user_msg)


class TestAgenticDoneParity(unittest.TestCase):
    """Drive RAGEngine.query with agentic_rag_enabled to verify the done payload."""

    def test_done_carries_enforcement_and_currency(self):
        import app.services.rag_engine as rag_engine
        from app.config import settings
        from app.services.rag_engine import RAGEngine

        # settings is a process-wide singleton: every write here must be
        # restored or later tests in the same worker inherit the agentic
        # pipeline (CI round 2 on d55001a: order-dependent failures across
        # test_rag_engine / test_wiki_routes / vector-store suites).
        snapshot = {}

        def _set(attr, value):
            if hasattr(settings, attr):
                snapshot[attr] = getattr(settings, attr)
                setattr(settings, attr, value)

        for attr in (
            "hybrid_search_enabled",
            "context_distillation_enabled",
            "retrieval_evaluation_enabled",
            "query_transformation_enabled",
            "reranking_enabled",
            "wiki_enabled",
            "parent_retrieval_enabled",
            "anchor_best_chunk",
            "context_distillation_synthesis_enabled",
            "instant_skip_retrieval_evaluation",
            "instant_skip_query_transformation",
        ):
            _set(attr, False)
        _set("retrieval_top_k", 5)
        _set("agentic_rag_enabled", True)

        def _restore():
            for attr, value in snapshot.items():
                setattr(settings, attr, value)

        self.addCleanup(_restore)

        engine = RAGEngine(
            embedding_service=_FakeEmbeddingService(),
            vector_store=_FakeVectorStore(),
            memory_store=_FakeMemoryStore(),
            llm_client=_FakeLLMClient(),
        )
        engine.retrieval_window = 0

        # Supersession returns a warning for the agentic sources.
        async def _fake_check(file_ids):
            self.assertEqual(file_ids, ["42"])
            return "superseded warning"

        engine._check_supersession_file_ids = _fake_check

        chunks = [
            {
                "id": "42_ab12cd34_default_0",
                "text": "Deploy checklist steps for the night shift.",
                "file_id": "42",
                "_distance": 0.1,
                "metadata": {"chunk_index": 0, "chunk_scale": "default"},
            }
        ]

        async def _fake_plan_and_execute(self, query, vault_id=None):
            from app.services.agentic_planner import AgenticResult

            return AgenticResult(
                output="Plain answer citing a phantom label [S9].",
                all_sources=[
                    {
                        "id": "42_ab12cd34_default_0",
                        "file_id": "42",
                        "source_label": "S1",
                        "snippet": "Deploy checklist steps.",
                    }
                ],
                rounds=1,
            )

        with unittest.mock.patch.object(
            rag_engine.AgenticPlanner, "plan_and_execute", _fake_plan_and_execute
        ):
            messages = []
            async def _collect():
                async for chunk in engine.query(
                    "checklist",
                    [],
                    stream=False,
                    vault_id=1,
                    citation_mode="required",
                ):
                    messages.append(chunk)
            asyncio.run(_collect())

        done = [m for m in messages if m.get("type") == "done"]
        self.assertEqual(len(done), 1)
        payload = done[0]
        self.assertEqual(
            payload["citation_enforcement"]["status"], "missing_citations"
        )
        self.assertEqual(payload["currency_warnings"], ["superseded warning"])

    def test_done_satisfied_when_real_label_cited(self):
        """Regression (PR #523 review): a properly-cited agentic answer must
        report citation_enforcement status "satisfied", not missing_citations.

        _CITATION_RE has two capture groups, so findall() yields tuples like
        ("S", "1") rather than bare label strings. The agentic enforcement
        block must normalize each match back to its label ("S1") before the
        label-set membership check, or the intersection with available labels
        never matches and every answer is flagged missing_citations.
        """
        import app.services.rag_engine as rag_engine
        from app.config import settings
        from app.services.rag_engine import RAGEngine

        # settings is a process-wide singleton: snapshot + restore (CI history).
        snapshot = {}

        def _set(attr, value):
            if hasattr(settings, attr):
                snapshot[attr] = getattr(settings, attr)
                setattr(settings, attr, value)

        for attr in (
            "hybrid_search_enabled",
            "context_distillation_enabled",
            "retrieval_evaluation_enabled",
            "query_transformation_enabled",
            "reranking_enabled",
            "wiki_enabled",
            "parent_retrieval_enabled",
            "anchor_best_chunk",
            "context_distillation_synthesis_enabled",
            "instant_skip_retrieval_evaluation",
            "instant_skip_query_transformation",
        ):
            _set(attr, False)
        _set("agentic_rag_enabled", True)

        def _restore():
            for attr, value in snapshot.items():
                setattr(settings, attr, value)

        self.addCleanup(_restore)

        engine = RAGEngine(
            embedding_service=_FakeEmbeddingService(),
            vector_store=_FakeVectorStore(),
            memory_store=_FakeMemoryStore(),
            llm_client=_FakeLLMClient(),
        )
        engine.retrieval_window = 0

        async def _fake_check(file_ids):
            return None

        engine._check_supersession_file_ids = _fake_check

        async def _fake_plan_and_execute(self, query, vault_id=None):
            from app.services.agentic_planner import AgenticResult

            return AgenticResult(
                output="The checklist covers the deploy steps. [S1]",
                all_sources=[
                    {
                        "id": "42_ab12cd34_default_0",
                        "file_id": "42",
                        "source_label": "S1",
                        "snippet": "Deploy checklist steps.",
                    }
                ],
                rounds=1,
            )

        with unittest.mock.patch.object(
            rag_engine.AgenticPlanner, "plan_and_execute", _fake_plan_and_execute
        ):
            messages = []

            async def _collect():
                async for chunk in engine.query(
                    "checklist",
                    [],
                    stream=False,
                    vault_id=1,
                    citation_mode="required",
                ):
                    messages.append(chunk)

            asyncio.run(_collect())

        done = [m for m in messages if m.get("type") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["citation_enforcement"]["status"], "satisfied")

        # PRR-012: the agentic path must emit the same versioned
        # evidence_candidates event as the standard pipeline, before done.
        candidates_events = [
            m for m in messages if m.get("type") == "evidence_candidates"
        ]
        self.assertEqual(len(candidates_events), 1)
        self.assertEqual(candidates_events[0]["version"], 1)
        self.assertEqual(
            [c["source_label"] for c in candidates_events[0]["candidates"]],
            ["S1"],
        )
        self.assertLess(
            messages.index(candidates_events[0]), messages.index(done[0])
        )


class _FakeEmbeddingService:
    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    async def search(self, *args, **kwargs):
        return []

    async def get_fts_exceptions(self):
        return 0


class _FakeMemoryStore:
    async def search_memories(self, *args, **kwargs):
        return []


class _FakeLLMClient:
    async def chat_completion(self, messages, temperature=0.7, max_tokens=None):
        return "unused"


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()


class TestQueryLeavesNoPerRequestEngineState(unittest.TestCase):
    """PRR-001 regression pin: query() must not persist per-request controls.

    The engine is a process-wide singleton; any per-request state left on
    ``self`` races across concurrent requests. Controls now live in locals
    threaded as explicit parameters, so after a full query() the engine
    instance must carry none of the former ``_active_*`` attributes.
    """

    def test_query_leaves_no_active_state_on_engine(self):
        import asyncio
        import unittest.mock

        import app.services.rag_engine as rag_engine
        from app.config import settings
        from app.services.rag_engine import RAGEngine

        snapshot = {}

        def _set(attr, value):
            if hasattr(settings, attr):
                snapshot[attr] = getattr(settings, attr)
                setattr(settings, attr, value)

        def _restore():
            for attr, value in snapshot.items():
                setattr(settings, attr, value)

        self.addCleanup(_restore)
        for attr in (
            "hybrid_search_enabled",
            "context_distillation_enabled",
            "retrieval_evaluation_enabled",
            "query_transformation_enabled",
            "reranking_enabled",
            "wiki_enabled",
            "parent_retrieval_enabled",
            "anchor_best_chunk",
            "context_distillation_synthesis_enabled",
            "instant_skip_retrieval_evaluation",
            "instant_skip_query_transformation",
            "agentic_rag_enabled",
        ):
            _set(attr, False)
        _set("retrieval_top_k", 5)

        engine = RAGEngine(
            embedding_service=_FakeEmbeddingService(),
            vector_store=_FakeVectorStore(),
            memory_store=_FakeMemoryStore(),
            llm_client=_FakeLLMClient(),
        )
        engine.retrieval_window = 0

        async def _fake_plan_and_execute(self, query, vault_id=None):
            from app.services.agentic_planner import AgenticResult

            return AgenticResult(
                output="answer [S1]",
                all_sources=[
                    {
                        "id": "42_ab12cd34_default_0",
                        "file_id": "42",
                        "source_label": "S1",
                        "snippet": "evidence",
                    }
                ],
                rounds=1,
            )

        # Exercise the agentic path (citation_mode consumed) AND assert the
        # engine carries no per-request attributes afterwards.
        with unittest.mock.patch.object(
            rag_engine.AgenticPlanner, "plan_and_execute", _fake_plan_and_execute
        ):
            async def _collect():
                messages = []
                async for chunk in engine.query(
                    "checklist",
                    [],
                    stream=False,
                    vault_id=1,
                    citation_mode="required",
                    metadata_filter=None,
                ):
                    messages.append(chunk)
                return messages

            messages = asyncio.run(_collect())

        self.assertTrue(any(m.get("type") == "done" for m in messages))
        for attr in (
            "_active_retrieval_mode",
            "_active_citation_mode",
            "_active_filter_expr",
        ):
            self.assertNotIn(
                attr,
                engine.__dict__,
                f"per-request state '{attr}' leaked onto the shared engine singleton",
            )


class TestConcurrentQueriesDoNotCrossControls(unittest.TestCase):
    """PRR-001 regression: two concurrently-executed query() calls on the SAME
    engine singleton must each apply their own citation_mode / filter.

    Pre-fix, the controls lived on engine instance state and concurrent
    interleaving could leak one request's mode into the other's done payload.
    """

    def test_interleaved_queries_keep_own_citation_modes(self):
        import asyncio
        import unittest.mock

        import app.services.rag_engine as rag_engine
        from app.config import settings
        from app.services.rag_engine import RAGEngine

        snapshot = {}

        def _set(attr, value):
            if hasattr(settings, attr):
                snapshot[attr] = getattr(settings, attr)
                setattr(settings, attr, value)

        def _restore():
            for attr, value in snapshot.items():
                setattr(settings, attr, value)

        self.addCleanup(_restore)
        for attr in (
            "hybrid_search_enabled",
            "context_distillation_enabled",
            "retrieval_evaluation_enabled",
            "query_transformation_enabled",
            "reranking_enabled",
            "wiki_enabled",
            "parent_retrieval_enabled",
            "anchor_best_chunk",
            "context_distillation_synthesis_enabled",
            "instant_skip_retrieval_evaluation",
            "instant_skip_query_transformation",
            "agentic_rag_enabled",
        ):
            _set(attr, False)
        _set("retrieval_top_k", 5)
        _set("agentic_rag_enabled", True)

        engine = RAGEngine(
            embedding_service=_FakeEmbeddingService(),
            vector_store=_FakeVectorStore(),
            memory_store=_FakeMemoryStore(),
            llm_client=_FakeLLMClient(),
        )
        engine.retrieval_window = 0

        async def _fake_plan_and_execute(self, query, vault_id=None):
            from app.services.agentic_planner import AgenticResult

            # Yield control so the two requests genuinely interleave.
            await asyncio.sleep(0)
            return AgenticResult(
                output="answer citing [S1]",
                all_sources=[
                    {
                        "id": "42_ab12cd34_default_0",
                        "file_id": "42",
                        "source_label": "S1",
                        "snippet": "evidence",
                    }
                ],
                rounds=1,
            )

        async def _run(citation_mode):
            chunks = []
            async for chunk in engine.query(
                "checklist",
                [],
                stream=False,
                vault_id=1,
                citation_mode=citation_mode,
            ):
                chunks.append(chunk)
            return [m for m in chunks if m.get("type") == "done"][0]

        with unittest.mock.patch.object(
            rag_engine.AgenticPlanner, "plan_and_execute", _fake_plan_and_execute
        ):
            async def _gather():
                return await asyncio.gather(
                    _run("required"),
                    _run(None),
                )

            done_required, done_none = asyncio.run(_gather())

        self.assertEqual(
            done_required["citation_enforcement"]["status"], "satisfied"
        )
        self.assertNotIn("citation_enforcement", done_none)
