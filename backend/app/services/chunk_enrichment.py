"""Chunk enrichment service for curator-style auxiliary metadata generation.

Generates auxiliary retrieval artifacts for chunks without mutating primary chunk text:
- chunk summary
- hypothetical questions answered by the chunk
- entities / acronyms / aliases
- section breadcrumb / heading anchors

Enrichment is stored as separate metadata fields tied to canonical chunk IDs,
never replacing the raw evidence text used for user-visible snippets.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.llm_client import LLMClient, LLMError

logger = logging.getLogger(__name__)


@dataclass
class ChunkEnrichment:
    """Auxiliary metadata generated for a chunk by the curator."""

    chunk_id: str
    summary: str = ""
    questions: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    section_breadcrumb: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "summary": self.summary,
            "questions": self.questions,
            "entities": self.entities,
            "aliases": self.aliases,
            "section_breadcrumb": self.section_breadcrumb,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkEnrichment":
        return cls(
            chunk_id=data.get("chunk_id", ""),
            summary=data.get("summary", ""),
            questions=data.get("questions", []),
            entities=data.get("entities", []),
            aliases=data.get("aliases", []),
            section_breadcrumb=data.get("section_breadcrumb", ""),
        )


class ChunkEnrichmentService:
    """Generates auxiliary retrieval metadata for document chunks.

    This service enriches chunks with curator-generated metadata that aids retrieval
    without mutating the primary chunk text. Enrichment artifacts are stored
    in the chunk's metadata dictionary under an 'enrichment' key.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        concurrency: int = 5,
        enrichment_fields: Optional[List[str]] = None,
    ):
        self._llm_client = llm_client
        self._concurrency = concurrency
        self._fields = enrichment_fields or ["summary", "questions", "entities"]
        self._semaphore = asyncio.Semaphore(concurrency)

    async def enrich_chunks(
        self,
        chunks: List[Dict[str, Any]],
        document_title: str = "",
    ) -> List[ChunkEnrichment]:
        """Enrich a batch of chunks with auxiliary metadata.

        Args:
            chunks: List of chunk dicts with at minimum 'text' and 'chunk_uid' keys
            document_title: Title of the source document for context

        Returns:
            List of ChunkEnrichment objects (same order as input chunks)
        """
        tasks = [self._enrich_single(chunk, document_title) for chunk in chunks]
        return await asyncio.gather(*tasks)

    async def _enrich_single(
        self,
        chunk: Dict[str, Any],
        document_title: str,
    ) -> ChunkEnrichment:
        """Enrich a single chunk."""
        chunk_id = chunk.get("chunk_uid") or chunk.get("id", "unknown")
        text = chunk.get("text", "")
        section = chunk.get("metadata", {}).get("section_title", "")

        if not text.strip():
            return ChunkEnrichment(chunk_id=chunk_id)

        async with self._semaphore:
            try:
                return await self._generate_enrichment(
                    chunk_id, text, document_title, section
                )
            except (LLMError, Exception) as e:
                logger.warning("Enrichment failed for chunk %s: %s", chunk_id, e)
                return ChunkEnrichment(chunk_id=chunk_id)

    async def _generate_enrichment(
        self,
        chunk_id: str,
        text: str,
        document_title: str,
        section: str,
    ) -> ChunkEnrichment:
        """Call LLM to generate enrichment metadata."""
        fields_instruction = ", ".join(self._fields)
        breadcrumb = " > ".join(filter(None, [document_title, section]))

        system_prompt = (
            "You are a document curator. Given a text passage, generate structured "
            "metadata to help with retrieval. Return ONLY valid JSON with the requested fields."
        )
        user_prompt = (
            f"Document: {document_title or 'Unknown'}\n"
            f"Section: {section or 'Unknown'}\n\n"
            f"Text:\n{text[:2000]}\n\n"
            f"Generate the following fields as JSON: {fields_instruction}\n\n"
            f"Format:\n"
            f'{{"summary": "1-2 sentence summary", '
            f'"questions": ["question1", "question2", "question3"], '
            f'"entities": ["entity1", "entity2"], '
            f'"aliases": ["alias1", "alias2"]}}'
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = await self._llm_client.chat_completion(
            messages, max_tokens=512, temperature=0.2
        )

        enrichment = ChunkEnrichment(
            chunk_id=chunk_id,
            section_breadcrumb=breadcrumb,
        )

        try:
            # Try to parse JSON from response (handle markdown code blocks)
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(cleaned)

            if "summary" in self._fields:
                enrichment.summary = data.get("summary", "")
            if "questions" in self._fields:
                enrichment.questions = data.get("questions", [])[:5]
            if "entities" in self._fields:
                enrichment.entities = data.get("entities", [])[:10]
            if "aliases" in self._fields:
                enrichment.aliases = data.get("aliases", [])[:10]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(
                "Failed to parse enrichment JSON for chunk %s: %s", chunk_id, e
            )

        return enrichment
