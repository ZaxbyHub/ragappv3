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
            if hasattr(settings, attr):
                setattr(settings, attr, False)
        settings.retrieval_top_k = 5
        settings.agentic_rag_enabled = True

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
