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
from app.services.circuit_breaker import (
    CircuitBreakerError,
    is_outage_status,
    reranking_cb,
)
from app.services.ssrf import assert_url_safe
from app.services.telemetry import (
    correlation_headers as _correlation_headers,
)
from app.services.telemetry import start_span

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


def _identity_activation(x):
    """Torch-free identity passed as ``activation_fn`` to CrossEncoder.predict.

    sentence-transformers >= 5.6 applies Sigmoid inside ``predict`` for
    single-label CrossEncoders; the identity keeps ``predict`` returning raw
    logits so the caller-side ``_safe_sigmoid`` remains the single sigmoid
    (#716 T1-35-K-01). Deliberately not ``torch.nn.Identity`` — the reranker
    local path must stay importable in CI environments without torch.
    """
    return x


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
            # #716 T1-35-K-09 (consumption-layer backstop): the producer
            # paths validate against their own bound (len(texts) for TEI,
            # enumerate positions for the local path), but this loop is the
            # last line of defense before chunks[idx] — anything outside
            # [0, len(chunks)) (positive OR negative) takes the documented
            # fallback instead of raising or mis-attaching.
            if not 0 <= idx < len(chunks):
                logger.warning(
                    "Rerank result index %r outside [0, %d) — returning "
                    "original order",
                    idx,
                    len(chunks),
                )
                return (chunks[:n], False)
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

        Issue #719 (TQ-differential-b-02): TEI enforces a per-request client
        batch cap (bundled default 32; ``--max-client-batch-size``). A larger
        candidate set is split into ``settings.reranker_max_batch``-sized
        requests; each batch is scored in full, so the union of per-batch
        results contains the global top-n and the merged ordering is
        equivalent to a single un-split request. At or below the cap the
        request is byte-identical to the historical single-shot call.
        """
        url = f"{self.reranker_url}/rerank"
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

        max_batch = settings.reranker_max_batch
        if len(texts) <= max_batch:
            batches = [(0, texts, top_n)]
        else:
            batches = [
                (offset, texts[offset : offset + max_batch], len(texts[offset : offset + max_batch]))
                for offset in range(0, len(texts), max_batch)
            ]

        merged: List[Tuple[int, float]] = []
        for offset, batch_texts, request_top_n in batches:
            payload = {
                "query": query,
                "texts": batch_texts,
                "top_n": request_top_n,
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
            data = await self._post_rerank_batch(url, payload)

            # data is list of {"index": int, "score": float}
            # Sort by score descending before slicing to ensure correct results
            sorted_data = sorted(data, key=lambda x: x.get("score", 0), reverse=True)
            # #716 T1-35-K-09: a malformed response may carry indices outside
            # [0, len(batch_texts)) — validate here (this method runs inside
            # the caller's guarded region) so the documented (chunks[:n],
            # False) fallback absorbs it, instead of an IndexError escaping
            # rerank() or a negative index silently mis-attaching a score.
            for item in sorted_data[:request_top_n]:
                idx = item["index"]
                if not isinstance(idx, int) or not 0 <= idx < len(batch_texts):
                    raise ValueError(
                        f"rerank response index out of range [0, {len(batch_texts)}): {idx!r}"
                    )
            # Map per-batch indices to global candidate indices; scores stay
            # raw logits until the single sigmoid below (monotonic, so
            # selecting on raw scores equals selecting on normalized scores).
            merged.extend(
                (offset + item["index"], item["score"])
                for item in sorted_data[:request_top_n]
            )

        # Global ordering across batches, then the SAME single-conversion
        # sigmoid semantics as the historical single-request path.
        merged.sort(key=lambda pair: pair[1], reverse=True)
        selected = merged[:top_n]
        return [
            (global_idx, _safe_sigmoid(raw)) for global_idx, raw in selected
        ]

    async def _post_rerank_batch(self, url: str, payload: Dict[str, Any]) -> Any:
        """POST one /rerank payload and return the parsed JSON body.

        # OPS-002 (issue #494): run the POST and the HTTP status check as
        # ONE breaker-wrapped operation so HTTPStatusError trips the
        # reranking breaker. Previously raise_for_status() ran outside the
        # wrap, so error statuses recorded a SUCCESS on the breaker.
        # JSON decoding stays outside — a malformed body is not an outage.
        """
        # E3 closure (#518): gen_ai client span for the rerank call.
        with start_span(
            "gen_ai rerank",
            attributes={
                "gen_ai.operation.name": "rerank",
                # #716 T1-35-K-10: the TEI payload never carries a
                # "model" key — advertise the CONFIGURED reranker.
                "gen_ai.request.model": str(self.reranker_model),
            },
        ):

            async def _checked_post() -> httpx.Response:
                # E3 telemetry (issue #518): correlation headers only when bound;
                # kwarg omitted when empty (strict test fakes keep working).
                _corr = _correlation_headers()
                if _corr:
                    response = await self._http_client.post(
                        url, json=payload, headers=_corr
                    )
                else:
                    response = await self._http_client.post(url, json=payload)
                # Outage statuses only inside the wrap (review RP-002, PR #576):
                # ordinary 4xx are input errors, not provider outages.
                if is_outage_status(response.status_code):
                    response.raise_for_status()
                return response

            try:
                response = await reranking_cb(_checked_post)()
            except CircuitBreakerError:
                raise
        response.raise_for_status()
        return response.json()

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
            # #716 T1-35-K-01: sentence-transformers >= 5.6 applies Sigmoid
            # inside CrossEncoder.predict for single-label models; pass an
            # identity activation so predict returns raw logits and the
            # single _safe_sigmoid below is the ONLY sigmoid. Torch-free on
            # purpose — the installed callable protocol just calls it.
            scores = model.predict(pairs, activation_fn=_identity_activation)
            indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

            # Unconditional sigmoid with overflow protection for BGE-M3 logits
            raw_scores = [score for _, score in indexed[:top_n]]
            normalized_scores = [_safe_sigmoid(score) for score in raw_scores]

            return list(zip([idx for idx, _ in indexed[:top_n]], normalized_scores))

        return await asyncio.to_thread(_score)
