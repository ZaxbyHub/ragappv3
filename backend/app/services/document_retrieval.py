"""Document retrieval service for RAG pipeline.

Handles document retrieval, filtering by relevance thresholds, and window expansion.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

# Matches the 8-hex-char hash produced by reupload_safe_order ID scheme
_RE_HASH8 = re.compile(r"^[0-9a-f]{8}$")

from app.config import settings  # noqa: E402

logger = logging.getLogger(__name__)


def _extract_reupload_hash(chunk_id: str, file_id: str) -> Optional[str]:
    """Return the 8-hex hash from a reupload-safe chunk ID, or None for legacy IDs.

    New format: {file_id}_{hash8}_{scale}_{idx}
    Legacy format: {file_id}_{scale}_{idx} or {file_id}_{idx}
    """
    prefix = f"{file_id}_"
    if not chunk_id.startswith(prefix):
        return None
    rest = chunk_id[len(prefix):]
    candidate = rest.split("_", 1)[0]
    return candidate if _RE_HASH8.fullmatch(candidate) else None


def _normalize_uid_for_dedup(uid: str) -> str:
    """Strip scale suffix from multi-scale chunk UIDs for deduplication.

    Multi-scale UIDs have format: {file_id}_{scale}_{index}
    Default UIDs have format: {file_id}_{index}

    This function strips the scale component so that "doc1_512_3" and "doc1_3"
    are treated as the same chunk for deduplication purposes.
    """
    # Try to parse as multi-scale: {file_id}_{scale}_{index}
    # The last segment should be a number (chunk_index)
    parts = uid.rsplit("_", 2)
    if len(parts) == 3:
        # Check if last part is a number (chunk_index)
        try:
            int(parts[2])
            # If last part is numeric, this could be multi-scale OR default with 3-part file_id
            # Check if middle part is a known scale pattern (numeric like "512", "1024", etc.)
            try:
                int(parts[1])
                # Middle part is also numeric → likely multi-scale: file_id_scale_index
                return f"{parts[0]}_{parts[2]}"
            except ValueError:
                # Middle part is not numeric → default UID with 3-part file_id
                return uid
        except ValueError:
            # Last part is not numeric → not a standard chunk UID
            return uid
    return uid


def _group_aware_dedup(
    sources: List["RAGSource"],
    per_doc_chunk_cap: int,
    unique_docs_in_top_k: int,
) -> List["RAGSource"]:
    """Group-aware dedup for final result list (Issue #12).

    Replaces UID-strip dedup that collapsed the best document's multiple strong
    chunks to a single entry.  This policy:
    - Preserves up to *per_doc_chunk_cap* chunks per document (default 5).
    - Caps breadth at *unique_docs_in_top_k* distinct documents (default 5).

    Sources are assumed to be in descending relevance order; iteration order is
    preserved so the final list is still ranked.

    Args:
        sources: Candidate RAGSource list, ranked by relevance.
        per_doc_chunk_cap: Maximum chunks allowed from the same document.
        unique_docs_in_top_k: Maximum distinct documents in the output.

    Returns:
        Filtered list respecting both caps.
    """
    selected: List["RAGSource"] = []
    count_per_doc: Dict[str, int] = {}
    selected_docs: Set[str] = set()

    for source in sources:
        file_id = source.file_id
        doc_count = count_per_doc.get(file_id, 0)

        # Cap: too many chunks already from this document
        if doc_count >= per_doc_chunk_cap:
            continue
        # Cap: already at max distinct docs and this is a new one
        if len(selected_docs) >= unique_docs_in_top_k and file_id not in selected_docs:
            continue

        selected.append(source)
        count_per_doc[file_id] = doc_count + 1
        selected_docs.add(file_id)

    return selected


@dataclass
class RAGSource:
    """Represents a retrieved document source."""

    text: str
    file_id: str
    score: float
    metadata: Dict[str, Any]
    # Parent-document retrieval field (Issue #12) — set by rag_engine when
    # parent_retrieval_enabled=True and the chunk has a stored parent window.
    parent_window_text: Optional[str] = None


class DocumentRetrievalService:
    """Service for retrieving and filtering relevant documents."""

    def __init__(
        self,
        vector_store: Optional[Any] = None,
        max_distance_threshold: Optional[float] = None,
        retrieval_top_k: Optional[int] = None,
        retrieval_window: Optional[int] = None,
    ) -> None:
        """Initialize the document retrieval service.

        Args:
            vector_store: Vector store instance for fetching adjacent chunks
            max_distance_threshold: Maximum distance threshold for filtering
            retrieval_top_k: Maximum number of results to return
            retrieval_window: Window size for expanding results with adjacent chunks
        """
        self.vector_store = vector_store
        self.max_distance_threshold = (
            max_distance_threshold or settings.max_distance_threshold
        )
        self.retrieval_top_k = retrieval_top_k or settings.retrieval_top_k
        self.retrieval_window = retrieval_window or settings.retrieval_window
        self.relevance_threshold = settings.rag_relevance_threshold  # Legacy support
        self.no_match: bool = (
            False  # Flag set when all results exceed distance threshold
        )

    async def filter_relevant(
        self,
        results: List[Dict[str, Any]],
        top_k: Optional[int] = None,
        reranked: bool = False,
        indexed_file_ids: Optional[Set[str]] = None,
    ) -> List[RAGSource]:
        """Filter retrieved documents by relevance threshold.

        Uses _distance from LanceDB (lower is better) or score (higher is better
        for backward compatibility). Falls back to returning top results if
        all results are filtered out.

        Args:
            results: List of raw search results from vector store
            top_k: Maximum number of results to return (defaults to retrieval_top_k)
            reranked: If True, skip distance filtering (reranker score is the signal)
            indexed_file_ids: If provided, filter out chunks whose file_id is not in
                this set. Used to hide chunks belonging to files still being ingested
                (status != 'indexed') — Issue #13 atomic visibility.

        Returns:
            List of filtered RAGSource objects

        Sets self.no_match = True when all results exceed the distance threshold
        and an empty list is returned.
        """
        if top_k is None:
            top_k = self.retrieval_top_k

        # Reset no_match flag at start of filtering
        self.no_match = False

        sources: List[RAGSource] = []
        distances: List[float] = []

        input_count = len(results)
        logger.debug(
            "Filtering: input_results=%d, max_distance_threshold=%s",
            input_count,
            self.max_distance_threshold,
        )
        if results:
            first_distances = [r.get("_distance", r.get("score")) for r in results[:5]]
            logger.debug("First few _distance values: %s", first_distances)

        # When reranking has been applied, skip distance threshold — the reranker
        # score is the quality signal; _distance is stale.
        skip_distance_filter = reranked

        for record in results:
            # Issue #13: Atomic visibility — skip chunks from non-indexed files
            if indexed_file_ids is not None:
                chunk_file_id = record.get("file_id", "")
                if chunk_file_id and chunk_file_id not in indexed_file_ids:
                    logger.debug(
                        "Skipping chunk from non-indexed file_id=%s (status pending or processing)",
                        chunk_file_id,
                    )
                    continue

            has_distance = "_distance" in record
            distance = record.get("_distance")
            if distance is None:
                score = record.get("score")
                if score is None:
                    score = 1.0
                distance = score

            distances.append(distance)

            threshold = self.max_distance_threshold
            if threshold is None:
                threshold = self.relevance_threshold

            should_skip = False
            if not skip_distance_filter and threshold is not None:
                if has_distance:
                    should_skip = distance > threshold
                else:
                    should_skip = distance < threshold

            if should_skip:
                continue

            # Use sigmoid-normalized _rerank_score when reranked, otherwise fall back to distance
            source_score = distance  # default
            if reranked and "_rerank_score" in record:
                raw_score = record["_rerank_score"]
                if isinstance(raw_score, (int, float)) and 0.0 <= float(raw_score) <= 1.0:
                    source_score = float(raw_score)
                else:
                    logger.warning(
                        "Invalid _rerank_score value %r for chunk %s, falling back to distance",
                        raw_score,
                        record.get("id", "unknown"),
                    )

            raw_meta = self._normalize_metadata(record.get("metadata"))
            # Carry the actual stored chunk ID so expand_window can handle
            # reupload-safe hash-prefixed IDs (e.g. "42_abc12345_default_0")
            raw_meta["_chunk_id"] = record.get("id", "")
            sources.append(
                RAGSource(
                    text=record.get("text", ""),
                    file_id=record.get("file_id", ""),
                    score=source_score,
                    metadata=raw_meta,
                )
            )

        if distances:
            initial_count = len(distances)
            filtered_count = len(sources)
            min_dist = min(distances)
            max_dist = max(distances)
            mean_dist = sum(distances) / len(distances)

            logger.info(
                "Vector search: initial=%d, filtered=%d, min=%.3f, max=%.3f, mean=%.3f, threshold=%.3f",
                initial_count,
                filtered_count,
                min_dist,
                max_dist,
                mean_dist,
                self.max_distance_threshold,
            )

        # Apply group-aware dedup before window expansion (Issue #12)
        if settings.new_dedup_policy and sources:
            before_dedup = len(sources)
            sources = _group_aware_dedup(
                sources,
                per_doc_chunk_cap=settings.per_doc_chunk_cap,
                unique_docs_in_top_k=settings.unique_docs_in_top_k,
            )
            if len(sources) != before_dedup:
                logger.debug(
                    "Group-aware dedup: %d → %d sources (cap=%d per-doc, %d unique-docs)",
                    before_dedup,
                    len(sources),
                    settings.per_doc_chunk_cap,
                    settings.unique_docs_in_top_k,
                )

        # Apply window expansion if enabled
        if self.retrieval_window > 0:
            sources = await self.expand_window(sources)

        # When all results exceed threshold, signal no_match instead of returning top-k
        if input_count > 0 and len(sources) == 0:
            logger.info(
                "All %d results exceeded max_distance_threshold=%.3f. "
                "Returning empty list with no_match flag.",
                input_count,
                self.max_distance_threshold,
            )
            self.no_match = True

        logger.debug("Filtering complete: %d results returned", len(sources))
        return sources

    async def expand_window(self, sources: List[RAGSource]) -> List[RAGSource]:
        """Expand search results by fetching adjacent chunks (N±window).

        Args:
            sources: Initial list of RAGSource chunks from vector search

        Returns:
            Expanded list of RAGSource chunks with adjacent context
        """
        if not sources or not self.vector_store:
            return sources

        window = self.retrieval_window

        # Group sources by (file_id, chunk_scale) to avoid cross-scale window mixing
        file_chunks: Dict[str, List[RAGSource]] = {}
        for source in sources:
            chunk_scale = source.metadata.get("chunk_scale", "default")
            if chunk_scale == "default" or chunk_scale is None:
                group_key = source.file_id
            else:
                group_key = f"{source.file_id}_{chunk_scale}"

            if group_key not in file_chunks:
                file_chunks[group_key] = []
            file_chunks[group_key].append(source)

        chunk_uids_to_fetch: List[str] = []

        for group_key, file_sources in file_chunks.items():
            if "_" in group_key:
                parts = group_key.rsplit("_", 1)
                if len(parts) == 2:
                    file_id, chunk_scale = parts
                else:
                    file_id = group_key
                    chunk_scale = "default"
            else:
                file_id = group_key
                chunk_scale = "default"

            def _parse_chunk_index(raw) -> int:
                """Parse chunk_index that may be in 'scale_idx' format."""
                s = str(raw)
                return int(s.rsplit("_", 1)[-1]) if "_" in s else int(s)

            indices = [
                _parse_chunk_index(s.metadata.get("chunk_index", 0))
                for s in file_sources
            ]

            # Detect reupload-safe hash prefix from any source in this group.
            # New-format IDs: {file_id}_{hash8}_{scale}_{idx}
            # Legacy IDs: {file_id}_{scale}_{idx} or {file_id}_{idx}
            hash_prefix: Optional[str] = None
            for s in file_sources:
                h = _extract_reupload_hash(s.metadata.get("_chunk_id", ""), file_id)
                if h:
                    hash_prefix = h
                    break

            for chunk_index in indices:
                start_idx = max(0, chunk_index - window)
                end_idx = chunk_index + window

                for idx in range(start_idx, end_idx + 1):
                    if hash_prefix:
                        chunk_uid = f"{file_id}_{hash_prefix}_{chunk_scale}_{idx}"
                    elif chunk_scale != "default":
                        chunk_uid = f"{file_id}_{chunk_scale}_{idx}"
                    else:
                        chunk_uid = f"{file_id}_{idx}"
                    chunk_uids_to_fetch.append(chunk_uid)

        # Fetch adjacent chunks from vector store
        if chunk_uids_to_fetch:
            adjacent_chunks = await self.vector_store.get_chunks_by_uid(
                chunk_uids_to_fetch
            )

            adjacent_lookup: Dict[str, Dict[str, Any]] = {}
            for chunk in adjacent_chunks:
                chunk_id = chunk.get("id", "")
                if chunk_id:
                    adjacent_lookup[chunk_id] = chunk

            expanded_sources: List[RAGSource] = []
            seen_uids: set = set()

            # First, add the original sources
            for source in sources:
                chunk_index = source.metadata.get("chunk_index", 0)
                chunk_scale = source.metadata.get("chunk_scale", "default")
                if chunk_scale and chunk_scale != "default":
                    uid = f"{source.file_id}_{chunk_scale}_{chunk_index}"
                else:
                    uid = f"{source.file_id}_{chunk_index}"
                if _normalize_uid_for_dedup(uid) not in seen_uids:
                    expanded_sources.append(source)
                    seen_uids.add(_normalize_uid_for_dedup(uid))

            # Then, add adjacent chunks that aren't already in the results
            for chunk_uid in chunk_uids_to_fetch:
                if _normalize_uid_for_dedup(chunk_uid) in seen_uids:
                    continue

                parts = chunk_uid.rsplit("_", 2)

                if len(parts) == 3:
                    file_id, chunk_scale, chunk_index_str = parts
                elif len(parts) == 2:
                    file_id, chunk_index_str = parts
                    chunk_scale = None
                else:
                    continue

                try:
                    chunk_index = int(chunk_index_str)
                except ValueError:
                    continue

                if chunk_uid in adjacent_lookup:
                    chunk = adjacent_lookup[chunk_uid]

                    distance = chunk.get("_distance")
                    if distance is None:
                        score = chunk.get("score")
                        if score is None:
                            score = 1.0
                        distance = score

                    metadata = self._normalize_metadata(chunk.get("metadata"))
                    if chunk_scale:
                        metadata["chunk_scale"] = chunk_scale

                    # Use file_id from the record, not parsed from the UID string
                    # (UID parsing breaks with hash-prefixed new-format IDs)
                    record_file_id = chunk.get("file_id") or file_id
                    expanded_source = RAGSource(
                        text=chunk.get("text", ""),
                        file_id=record_file_id,
                        score=distance,
                        metadata=metadata,
                    )
                    expanded_sources.append(expanded_source)
                    seen_uids.add(_normalize_uid_for_dedup(chunk_uid))

            # Two-tier ordering: preserve cross-document relevance ranking
            # while assembling chunks within each document in reading order.
            # Step 1: Find the best (lowest) position of any source per file_id.
            #         First occurrence = best rank for that document.
            file_rank: Dict[str, int] = {}
            for pos, s in enumerate(expanded_sources):
                fid = s.file_id
                if fid not in file_rank:
                    file_rank[fid] = pos

            # Step 2: Sort by (file_rank, file_id, chunk_index) — groups by document
            # in relevance order, then reading order within each document.
            def sort_key(source: RAGSource) -> tuple:
                idx = source.metadata.get("chunk_index", 0)
                try:
                    idx = int(idx)
                except (ValueError, TypeError):
                    idx = 0
                return (file_rank.get(source.file_id, 999), source.file_id, idx)

            expanded_sources.sort(key=sort_key)

            # Cap to retrieval_top_k total
            if len(expanded_sources) > self.retrieval_top_k:
                expanded_sources = expanded_sources[: self.retrieval_top_k]

            return expanded_sources

        return sources

    def _normalize_metadata(self, metadata: Any) -> Dict[str, Any]:
        """Ensure metadata is a dict, parsing JSON string if needed.

        Args:
            metadata: Raw metadata (dict, JSON string, or other)

        Returns:
            Normalized dictionary
        """
        if isinstance(metadata, dict):
            return metadata
        if isinstance(metadata, str):
            try:
                parsed = json.loads(metadata)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Failed to parse metadata JSON: %s", exc)
        return {}

    def to_source_metadata(
        self, chunk: RAGSource, source_index: int = 0
    ) -> Dict[str, Any]:
        """Convert RAGSource to source metadata dictionary.

        Args:
            chunk: RAGSource to convert
            source_index: 1-based index for stable source label (0 = not assigned)

        Returns:
            Dictionary with source metadata
        """
        filename = (
            chunk.metadata.get("source_file")
            or chunk.metadata.get("filename")
            or chunk.metadata.get("section_title")
            or "Unknown document"
        )
        section = (
            chunk.metadata.get("section_title")
            or chunk.metadata.get("heading")
            or ""
        )
        # Prefer the persisted chunk UID so frontend lazy preview can fetch the
        # exact indexed row, including reupload-safe hash-prefixed IDs.
        chunk_index = chunk.metadata.get("chunk_index", "")
        chunk_scale = chunk.metadata.get("chunk_scale", "")
        stored_chunk_id = chunk.metadata.get("_chunk_id") or chunk.metadata.get("chunk_uid")
        if stored_chunk_id:
            unique_id = str(stored_chunk_id)
        elif chunk_scale:
            unique_id = f"{chunk.file_id}_{chunk_scale}_{chunk_index}"
        else:
            unique_id = f"{chunk.file_id}_{chunk_index}"

        # Stable source label for citation resolution
        source_label = f"S{source_index}" if source_index > 0 else ""

        # Use raw_text for snippet when available (contextual chunking may have
        # prepended synthetic context to chunk.text — we want the original for display)
        raw_text = chunk.metadata.get("raw_text")
        snippet_source = raw_text if raw_text else chunk.text
        snippet = snippet_source[:300] if snippet_source else ""

        result: Dict[str, Any] = {
            "id": unique_id,
            "file_id": chunk.file_id,
            "filename": filename,
            "section": section,
            "source_label": source_label,
            "page_number": chunk.metadata.get("page_number"),
            "chunk_bbox": chunk.metadata.get("chunk_bbox"),
            "snippet": snippet,
            "score": chunk.score,
            "metadata": chunk.metadata,
        }
        # Synthesized sources are LLM-condensed summaries, not concrete retrieved
        # documents. Drop the (placeholder) score so no relevance label renders
        # for them on any frontend surface (all guard on ``score !== undefined``).
        if chunk.metadata.get("synthesized"):
            result.pop("score", None)
        return result

    def format_chunk(self, chunk: RAGSource) -> str:
        """Format a chunk for inclusion in the prompt context.

        Args:
            chunk: RAGSource to format

        Returns:
            Formatted string with source and text
        """
        source_title = (
            chunk.metadata.get("source_file")
            or chunk.metadata.get("section_title")
            or "document"
        )
        return f"Source {source_title} (score: {chunk.score:.2f}):\n{chunk.text}"
