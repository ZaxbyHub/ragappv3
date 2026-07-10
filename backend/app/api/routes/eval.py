"""
Evaluation API routes for RAG pipeline metrics.

NOTE: the ``/eval/ragas`` route computes **lexical-overlap approximation**
metrics (bigram/trigram overlap, keyword overlap, query/ground-truth word-set
overlap, optional embedding cosine), NOT metrics from the ``ragas`` library.
The route and model names retain the ``RAGAS`` prefix for API stability, but
the values are approximations intended for quick local sanity-checks of RAG
output, not the rigorous reference-based metrics the upstream ``ragas``
package provides. ``ragas`` is intentionally NOT a runtime dependency (it is
absent from all requirements files); the endpoint is gated solely on
``settings.eval_enabled``.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_embedding_service, get_rag_engine, require_admin_role
from app.services.embeddings import EmbeddingService
from app.services.rag_engine import RAGEngine

router = APIRouter()
logger = logging.getLogger(__name__)


class RAGASEvaluationRequest(BaseModel):
    """Request model for the lexical-overlap evaluation endpoint.

    Despite the legacy ``RAGAS`` prefix (kept for API stability), the metrics
    computed here are lexical-overlap approximations, not upstream-ragas values.
    """

    query: str = Field(..., min_length=1, description="User query to evaluate")
    answer: str = Field(..., description="Generated answer to evaluate")
    contexts: List[str] = Field(
        ..., min_length=1, description="Retrieved context chunks"
    )
    ground_truth: Optional[str] = Field(
        None, description="Ground truth answer for comparison"
    )


class RAGASMetrics(BaseModel):
    """Lexical-overlap approximation metrics (NOT upstream ragas values).

    The ``RAGAS`` name is retained for API stability; each field below is a
    hand-rolled heuristic (n-gram/keyword/word-set overlap, or embedding
    cosine for ``answer_similarity``). See ``eval.py`` module docstring.
    """

    faithfulness: float = Field(
        0.0, ge=0.0, le=1.0, description="Answer grounded in context"
    )
    answer_relevancy: float = Field(
        0.0, ge=0.0, le=1.0, description="Answer relevant to query"
    )
    context_precision: float = Field(
        0.0, ge=0.0, le=1.0, description="Retrieval precision"
    )
    context_recall: float = Field(0.0, ge=0.0, le=1.0, description="Retrieval recall")
    context_relevancy: float = Field(
        0.0, ge=0.0, le=1.0, description="Context relevance to query"
    )
    answer_similarity: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Similarity to ground truth"
    )


class RAGASEvaluationResponse(BaseModel):
    """Response model for the lexical-overlap evaluation endpoint.

    Note: ``metrics`` are lexical-overlap approximations, not upstream ragas.
    """

    metrics: RAGASMetrics
    evaluation_time_ms: int
    details: Dict[str, Any] = Field(default_factory=dict)


def _calculate_faithfulness(answer: str, contexts: List[str]) -> float:
    """
    Calculate faithfulness score (answer grounded in context).

    Simple heuristic: check what proportion of answer sentences
    have n-gram overlap with context.
    """
    import re

    if not answer or not contexts:
        return 0.0

    # Combine all contexts
    combined_context = " ".join(contexts).lower()

    # Split answer into sentences
    sentences = re.split(r"(?<=[.!?])\s+", answer)
    if not sentences:
        return 0.0

    supported_count = 0
    for sentence in sentences:
        sentence = sentence.strip().lower()
        if len(sentence) < 5:  # Skip very short sentences
            continue

        # Extract key phrases (2-3 word n-grams)
        words = sentence.split()
        if len(words) < 2:
            continue

        # Check for n-gram overlap
        found_overlap = False
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i + 1]}"
            if bigram in combined_context:
                found_overlap = True
                break

            if i < len(words) - 2:
                trigram = f"{words[i]} {words[i + 1]} {words[i + 2]}"
                if trigram in combined_context:
                    found_overlap = True
                    break

        if found_overlap:
            supported_count += 1

    total_sentences = len([s for s in sentences if len(s.strip()) >= 5])
    return supported_count / total_sentences if total_sentences > 0 else 0.0


def _calculate_answer_relevancy(query: str, answer: str) -> float:
    """
    Calculate answer relevancy score.

    Simple heuristic: check keyword overlap between query and answer.
    """
    if not query or not answer:
        return 0.0

    query_words = set(query.lower().split())
    answer_words = set(answer.lower().split())

    # Remove common stop words
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "and",
        "but",
        "or",
        "yet",
        "so",
        "if",
        "because",
        "although",
        "though",
        "while",
        "where",
        "when",
        "that",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "this",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "mine",
        "yours",
        "hers",
        "ours",
        "theirs",
        "myself",
        "yourself",
        "himself",
        "herself",
        "itself",
        "ourselves",
        "yourselves",
        "themselves",
    }

    query_words = query_words - stop_words
    answer_words = answer_words - stop_words

    if not query_words:
        return 1.0  # Query has no meaningful words

    overlap = query_words & answer_words
    return len(overlap) / len(query_words)


def _calculate_context_precision(contexts: List[str], query: str) -> float:
    """
    Calculate context precision score.

    Measures how many retrieved chunks are actually relevant to the query.
    """
    if not contexts or not query:
        return 0.0

    query_words = set(query.lower().split()) - {
        "the",
        "a",
        "an",
        "is",
        "are",
        "in",
        "of",
        "and",
        "to",
    }

    if not query_words:
        return 1.0

    relevant_count = 0
    for context in contexts:
        context_words = set(context.lower().split())
        overlap = query_words & context_words
        # Consider context relevant if it shares at least 20% of query terms
        if len(overlap) >= max(1, len(query_words) * 0.2):
            relevant_count += 1

    return relevant_count / len(contexts)


def _calculate_context_recall(
    contexts: List[str], ground_truth: Optional[str]
) -> float:
    """
    Calculate context recall score.

    Measures how much of the ground truth is covered by the retrieved contexts.
    """
    if not contexts or not ground_truth:
        return 1.0  # No ground truth to compare, assume perfect

    combined_context = " ".join(contexts).lower()
    ground_truth_words = set(ground_truth.lower().split())

    # Remove stop words
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "in",
        "of",
        "and",
        "to",
        "for",
        "on",
        "with",
        "at",
        "by",
    }
    ground_truth_words = ground_truth_words - stop_words

    if not ground_truth_words:
        return 1.0

    found_in_context = sum(1 for word in ground_truth_words if word in combined_context)
    return found_in_context / len(ground_truth_words)


def _calculate_context_relevancy(contexts: List[str], query: str) -> float:
    """
    Calculate average context relevancy to query.

    Measures semantic relevance of each context chunk to the query.
    """
    if not contexts or not query:
        return 0.0

    query_words = set(query.lower().split())
    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "in",
        "of",
        "and",
        "to",
        "for",
        "on",
        "with",
        "at",
        "by",
    }
    query_words = query_words - stop_words

    if not query_words:
        return 1.0

    total_relevancy = 0.0
    for context in contexts:
        context_words = set(context.lower().split())
        overlap = query_words & context_words
        relevancy = len(overlap) / len(query_words) if query_words else 0.0
        total_relevancy += min(1.0, relevancy * 2)  # Scale up but cap at 1.0

    return total_relevancy / len(contexts)


async def _calculate_answer_similarity(
    answer: str, ground_truth: Optional[str], embedding_service: EmbeddingService
) -> Optional[float]:
    """
    Calculate semantic similarity between answer and ground truth.

    Uses embeddings to compare semantic meaning.
    """
    if not ground_truth or not answer:
        return None

    try:
        answer_embedding = await embedding_service.embed_single(answer)
        truth_embedding = await embedding_service.embed_single(ground_truth)

        # Calculate cosine similarity
        import math

        dot_product = sum(a * b for a, b in zip(answer_embedding, truth_embedding))
        norm1 = math.sqrt(sum(a * a for a in answer_embedding))
        norm2 = math.sqrt(sum(b * b for b in truth_embedding))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)
    except Exception as e:
        logger.warning("Failed to calculate answer similarity: %s", e)
        return None


@router.post("/eval/ragas", response_model=RAGASEvaluationResponse)
async def ragas_evaluation(
    request: RAGASEvaluationRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    user: dict = Depends(require_admin_role),
):
    """
    Evaluate RAG pipeline output with lexical-overlap approximation metrics.

    NOTE: despite the legacy route path (``/eval/ragas``) and the ``RAGAS``
    model names, these are **hand-rolled lexical heuristics**, NOT metrics
    computed by the upstream ``ragas`` library (which is not a dependency).
    They are intended for quick local sanity-checks, not rigorous evaluation.

    Calculates (all lexical-overlap approximations unless noted):
    - Faithfulness: answer-sentence n-gram overlap with retrieved contexts
    - Answer Relevancy: keyword overlap between query and answer
    - Context Precision: query/contexts word-set overlap
    - Context Recall: ground-truth/contexts word-set overlap
    - Context Relevancy: average query/contexts word-set overlap
    - Answer Similarity: embedding cosine to ground truth (if provided)

    Args:
        request: RAGASEvaluationRequest containing query, answer, contexts

    Returns:
        RAGASEvaluationResponse with computed metrics and evaluation details

    Raises:
        HTTPException: 400 if request validation fails, 500 on evaluation error
    """
    from app.config import settings

    if not settings.eval_enabled:
        raise HTTPException(
            status_code=501,
            detail="Evaluation endpoint is disabled. Set EVAL_ENABLED=true to enable.",
        )

    # NOTE: the `ragas` library is intentionally NOT imported here. The route
    # previously gated on `import ragas` purely as an install-presence check,
    # but ragas was never called and is absent from all requirements files.
    # The metrics above are lexical-overlap heuristics; the gate is solely
    # `eval_enabled` (see module docstring).

    import time

    start_time = time.time()

    try:
        # Calculate all metrics
        faithfulness = _calculate_faithfulness(request.answer, request.contexts)
        answer_relevancy = _calculate_answer_relevancy(request.query, request.answer)
        context_precision = _calculate_context_precision(
            request.contexts, request.query
        )
        context_recall = _calculate_context_recall(
            request.contexts, request.ground_truth
        )
        context_relevancy = _calculate_context_relevancy(
            request.contexts, request.query
        )
        answer_similarity = await _calculate_answer_similarity(
            request.answer, request.ground_truth, embedding_service
        )

        evaluation_time_ms = int((time.time() - start_time) * 1000)

        metrics = RAGASMetrics(
            faithfulness=faithfulness,
            answer_relevancy=answer_relevancy,
            context_precision=context_precision,
            context_recall=context_recall,
            context_relevancy=context_relevancy,
            answer_similarity=answer_similarity,
        )

        details = {
            "query_length": len(request.query),
            "answer_length": len(request.answer),
            "context_count": len(request.contexts),
            "ground_truth_provided": request.ground_truth is not None,
        }

        logger.info(
            "RAGAS evaluation completed: faithfulness=%.3f, relevancy=%.3f, precision=%.3f, "
            "recall=%.3f, time_ms=%d",
            faithfulness,
            answer_relevancy,
            context_precision,
            context_recall,
            evaluation_time_ms,
        )

        return RAGASEvaluationResponse(
            metrics=metrics, evaluation_time_ms=evaluation_time_ms, details=details
        )

    except Exception:
        logger.exception("RAGAS evaluation failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Live retrieval benchmark endpoint (FR-001)
# ---------------------------------------------------------------------------


class LiveBenchmarkItem(BaseModel):
    """A single benchmark query with ground-truth relevant document identifiers.

    Mirrors ``eval_adapter.BenchmarkItem``.
    """

    id: str = Field(..., min_length=1, description="Stable benchmark item id")
    query: str = Field(..., min_length=1, description="Natural-language query")
    relevant_ids: List[str] = Field(
        default_factory=list,
        description="Ground-truth file_ids relevant to the query",
    )


class LiveEvalRequest(BaseModel):
    """Request model for live retrieval benchmark endpoint."""

    benchmark: List[LiveBenchmarkItem] = Field(
        ...,
        min_length=1,
        description="Benchmark set: queries with ground-truth relevant_ids",
    )
    vault_id: Optional[int] = Field(
        None, description="Vault scope for all queries (optional)"
    )
    top_k: Optional[int] = Field(
        None, ge=1, description="Override recall@k / nDCG@k k value"
    )


class LiveEvalResponse(BaseModel):
    """Response model for live retrieval benchmark endpoint."""

    run_id: str = Field(description="Unique run identifier")
    timestamp: str = Field(description="ISO 8601 UTC run timestamp")
    release_id: str = Field(description="Git short commit hash or fallback")
    top_k: int = Field(description="k used for recall@k and nDCG@k")
    query_metrics: List[Dict[str, Any]] = Field(
        description="Per-query metric breakdown"
    )
    mrr_mean: Optional[float] = Field(None, description="Mean Reciprocal Rank")
    ndcg_mean: Optional[float] = Field(None, description="Mean nDCG@k")
    recall_mean: Optional[float] = Field(None, description="Mean recall@k")


@router.post("/eval/live", response_model=LiveEvalResponse)
async def live_eval(
    request: LiveEvalRequest,
    rag_engine: RAGEngine = Depends(get_rag_engine),
    user: dict = Depends(require_admin_role),
):
    """Run a live retrieval benchmark against the RAG pipeline (FR-001).

    For each item in the supplied ``benchmark``, this endpoint:
    1. Calls ``RAGEngine.query_retrieve_only`` to obtain the ranked list of
       retrieved file_ids from the live vector store + reranker pipeline.
    2. Computes MRR, nDCG@k, and recall@k against the ground-truth ``relevant_ids``.
    3. Persists the run record (timestamp, release_id, per-query and aggregate
       metrics) as JSONL to ``data/eval-runs/runs.jsonl``.

    This bridges the offline eval harness (which consumes pre-supplied JSONL
    contexts) with the live retrieval pipeline, enabling production retrieval
    quality measurement without curated benchmark files.

    **Access**: Admin-gated via ``require_admin_role``.
    **Feature flag**: Requires ``settings.eval_enabled=True`` (defaults to False).

    Args:
        request: LiveEvalRequest containing the benchmark set and optional overrides.

    Returns:
        LiveEvalResponse with run metadata and computed metrics.

    Raises:
        HTTPException: 501 if eval is disabled, 500 on internal failure.
    """
    from app.config import settings
    from app.services.eval_adapter import BenchmarkItem, LiveEvalAdapter

    if not settings.eval_enabled:
        raise HTTPException(
            status_code=501,
            detail=(
                "Live evaluation endpoint is disabled. "
                "Set EVAL_ENABLED=true to enable."
            ),
        )

    try:
        # Build the adapter
        adapter = LiveEvalAdapter(top_k=request.top_k or 5)

        # Convert request model to adapter types
        benchmark_items = [
            BenchmarkItem(id=item.id, query=item.query, relevant_ids=item.relevant_ids)
            for item in request.benchmark
        ]

        # Run live evaluation
        result = await adapter.run_live(
            benchmark=benchmark_items,
            rag_engine=rag_engine,
            vault_id=request.vault_id,
            top_k=request.top_k,
        )

        logger.info(
            "Live eval run %s completed: MRR=%.3f, nDCG=%.3f, recall=%.3f",
            result.run_id,
            result.mrr_mean or 0.0,
            result.ndcg_mean or 0.0,
            result.recall_mean or 0.0,
        )

        return LiveEvalResponse(
            run_id=result.run_id,
            timestamp=result.timestamp,
            release_id=result.release_id,
            top_k=result.top_k,
            query_metrics=[qm.__dict__ for qm in result.query_metrics],
            mrr_mean=result.mrr_mean,
            ndcg_mean=result.ndcg_mean,
            recall_mean=result.recall_mean,
        )

    except Exception:
        logger.exception("Live eval failed")
        raise HTTPException(status_code=500, detail="Internal server error")
