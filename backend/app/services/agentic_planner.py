"""Agentic planner loop — iterative retrieval + synthesis driven by an LLM.

This module provides the :class:`AgenticPlanner` which wraps a
:class:`~app.services.agentic_tools.ToolRegistry` and alternates between
retrieval rounds and LLM-guided reasoning, bounded by ``max_rounds``.
It is wired into ``RAGEngine.query`` behind ``settings.agentic_rag_enabled``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.agentic_tools import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AgenticResult:
    """Result returned by :meth:`AgenticPlanner.plan_and_execute`."""

    output: str
    all_sources: List[Dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    sub_queries_used: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class AgenticPlanner:
    """Iterative retrieval + synthesis planner.

    The planner holds a :class:`~app.services.agentic_tools.ToolRegistry` and
    an optional LLM client.  On each round it either retrieves more evidence
    (guided by the LLM) or synthesises a final answer from everything gathered
    so far.

    Parameters
    ----------
    tool_registry
        Registry that provides ``RetrievalTool`` and ``SynthesisTool`` instances.
    llm_client
        Optional OpenAI-compatible LLM client.  When ``None`` or when the LLM
        call / parsing fails the planner falls back to single-round retrieval +
        synthesis.
    max_rounds
        Upper bound on retrieval rounds (default 3).  The first retrieval with
        the user's query always runs; subsequent rounds are LLM-guided.
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        llm_client: Optional[Any] = None,
        max_rounds: int = 3,
    ) -> None:
        self._registry = tool_registry
        self._llm = llm_client
        self._max_rounds = max_rounds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan_and_execute(
        self,
        query: str,
        vault_id: Optional[int] = None,
    ) -> AgenticResult:
        """Run the iterative retrieval + synthesis loop.

        Parameters
        ----------
        query
            The original user question.
        vault_id
            Optional vault scope for retrieval.

        Returns
        -------
        AgenticResult
            ``output`` holds the synthesized answer; ``all_sources`` accumulates
            evidence from every retrieval round; ``rounds`` is the number of
            retrieval iterations performed; ``sub_queries_used`` lists every
            sub-query the LLM generated.
        """
        retrieval_tool = self._registry.get("retrieval")
        synthesis_tool = self._registry.get("synthesis")

        if retrieval_tool is None:
            return AgenticResult(
                output="",
                all_sources=[],
                rounds=0,
                sub_queries_used=[],
            )

        all_sources: List[Dict[str, Any]] = []
        sub_queries_used: List[str] = []
        rounds = 0
        current_query = query

        # --- Round 1: always retrieve with the original query ---------------
        first_result = await retrieval_tool.execute(query=current_query, vault_id=vault_id)
        rounds = 1
        if first_result.success:
            all_sources.extend(first_result.sources)

        if first_result.success and synthesis_tool is None:
            # No synthesis tool registered — return retrieval output as-is
            return AgenticResult(
                output=first_result.output,
                all_sources=list(all_sources),
                rounds=rounds,
                sub_queries_used=[],
            )

        # --- Subsequent rounds: LLM decides whether to retrieve more -------
        if self._llm is not None and rounds < self._max_rounds:
            decision = await self._decide_next_action(query, all_sources)
            while (
                decision["action"] == "retrieve_more"
                and rounds < self._max_rounds
            ):
                sub_query = decision.get("sub_query", current_query)
                sub_queries_used.append(sub_query)

                retrieval_result = await retrieval_tool.execute(
                    query=sub_query,
                    vault_id=vault_id,
                )
                rounds += 1
                if retrieval_result.success:
                    all_sources.extend(retrieval_result.sources)

                if rounds >= self._max_rounds:
                    break

                decision = await self._decide_next_action(query, all_sources)

        # --- Final synthesis -----------------------------------------------
        if synthesis_tool is not None:
            synthesis_result = await synthesis_tool.execute(
                text=query,
                sources=list(all_sources),
            )
            if synthesis_result.success:
                return AgenticResult(
                    output=synthesis_result.output,
                    all_sources=list(all_sources),
                    rounds=rounds,
                    sub_queries_used=list(sub_queries_used),
                )
            # Synthesis failed — fall back to retrieval output
            return AgenticResult(
                output=first_result.output,
                all_sources=list(all_sources),
                rounds=rounds,
                sub_queries_used=[],
            )

        # Fallback: return retrieval output
        return AgenticResult(
            output=first_result.output,
            all_sources=list(all_sources),
            rounds=rounds,
            sub_queries_used=[],
        )

    # ------------------------------------------------------------------
    # LLM decision helper
    # ------------------------------------------------------------------

    async def _decide_next_action(
        self,
        original_query: str,
        gathered_sources: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Ask the LLM whether another retrieval round is needed.

        The prompt contains the original query and a summary of sources
        retrieved so far.  The expected response is a JSON object::

            {{"action": "retrieve_more", "sub_query": "<refined query>"}}
            # or
            {{"action": "synthesize"}}

        Parameters
        ----------
        original_query
            The user's original question.
        gathered_sources
            Sources accumulated from previous retrieval rounds.

        Returns
        -------
        dict
            ``{"action": "retrieve_more"|"synthesize", "sub_query": ...}``
        """
        sources_summary = self._summarise_sources(gathered_sources)
        prompt = (
            "You are a reasoning assistant helping a RAG system decide whether "
            "to fetch more evidence before answering.\n\n"
            f"Original query: {original_query}\n\n"
            f"Evidence gathered so far:\n{sources_summary}\n\n"
            "Based on the above, decide the next action. "
            "Return ONLY a JSON object with no extra text: "
            '{"action": "retrieve_more", "sub_query": "<refined sub-query>"}'
            " or "
            '{"action": "synthesize"}'
        )

        try:
            response: str = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            parsed = json.loads(response.strip())
            action = parsed.get("action", "synthesize")
            if action not in ("retrieve_more", "synthesize"):
                action = "synthesize"
            return {"action": action, "sub_query": parsed.get("sub_query", "")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgenticPlanner] LLM decision failed: %s", exc)
            return {"action": "synthesize", "sub_query": ""}

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _summarise_sources(sources: List[Dict[str, Any]]) -> str:
        """Render a compact string summary of gathered sources."""
        if not sources:
            return "(no sources retrieved yet)"
        lines = []
        for i, src in enumerate(sources[:10], start=1):
            snippet = str(src.get("snippet", ""))[:120]
            score = src.get("score")
            score_str = f" (score={score:.2f})" if score is not None else ""
            lines.append(f"  [{i}] {snippet!r}{score_str}")
        if len(sources) > 10:
            lines.append(f"  ... and {len(sources) - 10} more sources")
        return "\n".join(lines) if lines else "(no sources retrieved yet)"
