"""
Cross-encoder reranking service for KnowledgeVault.

Supports two backends:
  1. TEI endpoint (if reranker_url is set): POST {url}/rerank
     Expected request:  {"query": str, "texts": [str], "top_n": int,
                         "truncate": true, "raw_scores": true}
     Expected response: [{"index": int, "score": float}, ...] (raw logits;
                         the client sigmoid-normalizes them exactly once)
  2. Local sentence-transformers CrossEncoder (if reranker_url is empty).
     Models are loaded lazily on first use and cached per model identity
     (LRU, capped) so distinct reranker models never share a scorer.
"""

import asyncio
import logging
import math
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.services.circuit_breaker import CircuitBreakerError, reranking_cb
from app.services.ssrf import assert_url_safe

logger = logging.getLogger(__name__)

# Model-identity-keyed cache of loaded CrossEncoder instances (RERANK-001,
# issue #511). Keyed by model_id so two configured reranker identities never
# share a scorer, LRU-ordered, and capped so at most _LOCAL_MODEL_CACHE_MAX
# models stay resident (bounded memory; eviction just drops the reference).
_LOCAL_MODEL_CACHE_MAX = 2
_local_models: "OrderedDict[str, Any]" = OrderedDict()
_model_lock = threading.Lock()  # lock for thread-safe lazy initialization


def _reset_local_model_cache() -> None:
    """Drop every cached local reranker model (test/support helper)."""
    with _model_lock:
        _local_models.clear()


def _loaded_local_model_count() -> int:
    """Return how many local reranker models are currently resident."""
    with _model_lock:
        return len(_local_models)


def _iter_local_model_ids():
    """Return a snapshot of the cached reranker model identities, LRU order."""
    with _model_lock:
        return list(_local_models.keys())


def _safe_sigmoid(logit: float) -> float:
    """Unconditional sigmoid with overflow protection for BGE-M3 logits.

    A NaN logit (malformed/hostile TEI response) maps to 0.0 — the lowest
    relevance — instead of poisoning the score with NaN, which would break
    every downstream numeric comparison silently (PRR-010, PR #528 review).
    """
    if logit > 709:
        return 1.0
    if logit < -709:
        return 0.0
    if math.isnan(logit):
        return 0.0
    return 1.0 / (1.0 + math.exp(-logit))


def _get_local_model(model_id: str):
    """Lazy-load and cache a sentence-transformers CrossEncoder per model identity.

    The cache is keyed by ``model_id`` (LRU, capped at
    ``_LOCAL_MODEL_CACHE_MAX`` resident models): a second configured identity
    loads its own instance instead of silently reusing the first one, and a
    repeat call for an already-loaded identity never reloads. Loads happen
    under ``_model_lock`` with a double-check so concurrent first loads of
    one identity construct exactly one instance.
    """
    with _model_lock:
        model = _local_models.get(model_id)
        if model is not None:
            # Touch recency so the LRU eviction order reflects use.
            _local_models.move_to_end(model_id)
            return model

        # Construct while holding the lock so racing threads cannot build
        # duplicate instances for the same identity. (Single check under the
        # lock — there is no lock-free fast path, so this is not classic
        # double-checked locking.)
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"Loading local CrossEncoder reranker: {model_id}")
            model = CrossEncoder(model_id)
            logger.info("Local reranker loaded successfully")
        except ImportError:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "Either set RERANKER_URL to a TEI endpoint, or add "
                "'sentence-transformers>=2.7.0' to requirements.txt."
            )

        _local_models[model_id] = model
        # Evict least-recently-used identities beyond the cap. Unloading is
        # just dropping the reference — the GC reclaims the weights.
        while len(_local_models) > _LOCAL_MODEL_CACHE_MAX:
            evicted_id, _ = _local_models.popitem(last=False)
            logger.info(
                "Evicted least-recently-used local reranker model from cache: %s",
                evicted_id,
            )
        return model


class RerankingService:
    """
    Reranks a list of (chunk_dict) results given a query string.

    chunk_dict must have at minimum a 'text' key.
    Returns the top_n highest-scoring chunks, ordered by relevance descending.
    """

    # Sentinel marking "no explicit override given at construction".
    # Distinct from ``None``/``""`` so callers can intentionally pass an empty
    # string to mean "always local mode" if they ever want to.
    _UNSET = object()

    def __init__(
        self,
        reranker_url: Any = _UNSET,
        reranker_model: Any = _UNSET,
        top_n: Any = _UNSET,
    ):
        """Initialize the reranking service.

        When no arguments are passed, URL/model/top_n are read live from
        ``settings`` on every call so admins can change them via the Settings
        UI without restarting. When arguments are passed (e.g. from tests),
        those values become per-instance overrides that shadow the live read.
        """
        self._reranker_url_override = (
            reranker_url.rstrip("/") if isinstance(reranker_url, str) and reranker_url
            else "" if reranker_url == ""
            else None if reranker_url is self._UNSET
            else reranker_url
        )
        self._reranker_model_override = (
            None if reranker_model is self._UNSET else reranker_model
        )
        self._top_n_override = None if top_n is self._UNSET else top_n
        self._http_client: Optional[httpx.AsyncClient] = None
        # NOTE: startup URL validation is intentionally NOT performed here, even
        # though EmbeddingService/LLMClient validate at construction. Unlike
        # those, RerankingService resolves its URL lazily (the _UNSET sentinel
        # reads live from settings on every call so admins can change it without
        # restarting), and per-call assert_url_safe in _rerank_via_endpoint is
        # the correct validation point. Validating at construction would both
        # break the deferred-resolution design and reject test fixtures that use
        # non-resolvable hostnames (e.g. 'reranker.local').

    @property
    def reranker_url(self) -> str:
        """Live read of the reranker URL (empty string → local mode)."""
        if self._reranker_url_override is not None:
            return self._reranker_url_override
        url = settings.reranker_url
        return url.rstrip("/") if url else ""

    @property
    def reranker_model(self) -> str:
        """Live read of the reranker model name."""
        if self._reranker_model_override is not None:
            return self._reranker_model_override
        return settings.reranker_model

    @property
    def top_n(self) -> int:
        """Live read of the default top_n. Caller can still override per-call."""
        if self._top_n_override is not None:
            return self._top_n_override
        return settings.reranker_top_n

    async def rerank(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Rerank chunks given a query. Returns top_n chunks sorted by score desc.

        Args:
            query: The user's query string.
            chunks: List of chunk dicts (must contain 'text' key).
            top_n: Override instance top_n for this call.

        Returns:
            Tuple of (reranked and trimmed list of chunk dicts with '_rerank_score', success).
        """
        n = top_n or self.top_n
        if not chunks:
            return ([], True)
        if len(chunks) <= 1:
            # Scoring is bypassed for a single chunk, so report success=False:
            # a lone chunk has no relative ordering to compute and must not be
            # labeled with rerank-score semantics downstream (RERANK-003,
            # issue #511). The chunk is returned unchanged, no _rerank_score.
            return (chunks, False)

        texts = [c.get("text", "") for c in chunks]

        try:
            if self.reranker_url:
                scored = await self._rerank_via_endpoint(query, texts, n)
            else:
                scored = await self._rerank_local(query, texts, n)
        except CircuitBreakerError as e:
            # Expected self-healing condition (reranker circuit open) — log at
            # WARNING with backend context so it's distinguishable from a
            # code-level parse bug or a network timeout.
            logger.warning(
                "Reranker circuit open (backend=%s), returning original order: %s",
                self.reranker_url or "local",
                e,
            )
            return (chunks[:n], False)
        except httpx.HTTPError as e:
            # Network/timeout/HTTP errors — WARNING with backend context.
            logger.warning(
                "Reranker HTTP error (backend=%s), returning original order: %s",
                self.reranker_url or "local",
                e,
            )
            return (chunks[:n], False)
        except Exception as e:
            # Genuinely unexpected (e.g. a response-parsing bug) — ERROR with
            # a full stack trace for triage.
            logger.error(
                "Reranking failed unexpectedly, returning original order: %s",
                e,
                exc_info=True,
            )
            return (chunks[:n], False)

        # Attach score and return top_n
        result = []
        for idx, score in scored:
            chunk = dict(chunks[idx])
            chunk["_rerank_score"] = score
            result.append(chunk)
        return (result, True)

    async def _rerank_via_endpoint(
        self, query: str, texts: List[str], top_n: int
    ) -> List[Tuple[int, float]]:
        """
        Call a TEI-compatible rerank endpoint.

        TEI format:
          POST /rerank
          Body: {"query": str, "texts": [str], "top_n": int, "truncate": true,
                 "raw_scores": true}
          Response: [{"index": int, "score": float}, ...]
        """
        url = f"{self.reranker_url}/rerank"
        payload = {
            "query": query,
            "texts": texts,
            "top_n": top_n,
            "truncate": True,
            # Score protocol (RERANK-004, issue #511): TEI's default config
            # returns sigmoid-normalized 0..1 scores, but servers can be
            # configured to return raw logits. Requesting raw_scores=true
            # makes the response explicit logits, so the single client-side
            # _safe_sigmoid below converts exactly once regardless of the
            # server's default normalization — the same single-conversion
            # semantics as the local CrossEncoder path.
            "raw_scores": True,
        }
        await asyncio.to_thread(assert_url_safe, self.reranker_url)
        if self._http_client is None:
            # SSRFSafeTransport re-validates the resolved IP at request time to
            # close the DNS-rebinding TOCTOU gap the per-call guard leaves open.
            from app.services.ssrf_transport import SSRFSafeTransport

            self._http_client = httpx.AsyncClient(
                timeout=settings.reranker_timeout_seconds,
                follow_redirects=False,
                transport=SSRFSafeTransport(),
            )
        try:
            response = await reranking_cb(self._http_client.post)(url, json=payload)
            response.raise_for_status()
            data = response.json()
        except CircuitBreakerError:
            raise

        # data is list of {"index": int, "score": float}
        # Sort by score descending before slicing to ensure correct results
        sorted_data = sorted(data, key=lambda x: x.get("score", 0), reverse=True)
        raw_scores = [item["score"] for item in sorted_data[:top_n]]

        # Unconditional sigmoid with overflow protection for BGE-M3 logits
        normalized_scores = [_safe_sigmoid(score) for score in raw_scores]

        return [(item["index"], score) for item, score in zip(sorted_data[:top_n], normalized_scores)]

    async def close(self):
        """Close the persistent HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _rerank_local(
        self, query: str, texts: List[str], top_n: int
    ) -> List[Tuple[int, float]]:
        """
        Rerank using a locally loaded sentence-transformers CrossEncoder.
        Runs in a thread to avoid blocking the event loop.
        """
        def _score():
            model = _get_local_model(self.reranker_model)
            pairs = [(query, text) for text in texts]
            scores = model.predict(pairs)
            indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

            # Unconditional sigmoid with overflow protection for BGE-M3 logits
            raw_scores = [score for _, score in indexed[:top_n]]
            normalized_scores = [_safe_sigmoid(score) for score in raw_scores]

            return list(zip([idx for idx, _ in indexed[:top_n]], normalized_scores))

        return await asyncio.to_thread(_score)
