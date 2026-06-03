"""Retrieval evaluation service for CRAG (Corrective RAG) self-evaluation."""

import logging
from typing import Any, Dict, List

from .llm_client import LLMClient

logger = logging.getLogger(__name__)


class RetrievalEvaluator:
    """Evaluates retrieval quality using LLM-based self-assessment."""

    def __init__(self, llm_client: LLMClient):
        self._llm_client = llm_client

    async def evaluate(self, query: str, chunks: List[Dict[str, Any]]) -> str:
        """
        Evaluate whether retrieved chunks are relevant to the query.

        Args:
            query: The original user query
            chunks: List of retrieved chunks (dicts with 'text' key)

        Returns:
            One of: "CONFIDENT" | "AMBIGUOUS" | "NO_MATCH"
            On any error, returns "CONFIDENT" (fail-open).
        """
        try:
            # Take top 3 chunks
            top_chunks = chunks[:3]
            if not top_chunks:
                return "CONFIDENT"  # No chunks to evaluate

            # Extract and truncate text from each chunk
            chunk_texts = []
            for i, chunk in enumerate(top_chunks, 1):
                text = chunk.get("text", "")
                # Truncate to 500 chars
                if len(text) > 500:
                    text = text[:500] + "..."
                chunk_texts.append(f"{i}. {text}")

            chunks_str = "\n".join(chunk_texts)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a retrieval evaluator. Assess if the retrieved documents "
                        "answer the user's query. Respond with exactly ONE word: "
                        "CONFIDENT (documents clearly answer), AMBIGUOUS (partially relevant), "
                        "or NO_MATCH (not relevant)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Query: <user_query>{query}</user_query>\n\nDocuments:\n{chunks_str}\n\nClassification:"
                    ),
                },
            ]

            # One-word classification. Keep max_tokens small but with enough
            # slack for a reasoning model that may emit a few tokens before the
            # word (8 was too tight and truncated to empty → silent CONFIDENT).
            response = await self._llm_client.chat_completion(
                messages=messages, max_tokens=64, temperature=0.1
            )

            if not response:
                logger.warning("Retrieval evaluator returned empty response")
                return "CONFIDENT"

            # Parse response - look for one of the three keywords
            response_clean = response.strip().upper()

            if "NO_MATCH" in response_clean or "NO MATCH" in response_clean:
                return "NO_MATCH"
            elif "AMBIGUOUS" in response_clean:
                return "AMBIGUOUS"
            elif "CONFIDENT" in response_clean:
                return "CONFIDENT"
            else:
                # Unexpected response, log and default to CONFIDENT
                logger.warning(
                    "Retrieval evaluator returned unexpected response: '%s', defaulting to CONFIDENT",
                    response,
                )
                return "CONFIDENT"

        except Exception as e:
            # Fail-open to CONFIDENT, matching the documented contract and every
            # other fallback in this method (empty chunks / empty / unexpected
            # response). CONFIDENT is the no-op verdict: it injects no relevance
            # hint into the prompt and does not trigger distillation synthesis, so
            # an evaluator outage cannot silently degrade an otherwise-good answer.
            logger.warning(
                "Retrieval evaluation failed: %s, defaulting to CONFIDENT", e
            )
            return "CONFIDENT"
