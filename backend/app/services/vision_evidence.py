"""Narrow post-retrieval vision-evidence service (issue #462).

Runs late in the standard RAG ``query()`` flow — AFTER final source
distillation/packing/parent expansion and BEFORE prompt construction. Given the
final retrieved sources, it selects authorized artifact winners whose modality and
asset are eligible, loads their bytes server-side from the confined per-vault
asset store, re-authorizes each against the current vault opt-in and provider
policy immediately before every call, and asks a narrow multimodal client for a
query-conditioned text observation keyed to the artifact.

Security invariants (locked by issue #462 / plan V1-V5):
- V1: never runs on excluded paths (agentic, memory-directive, retrieval-only).
- V3: authorization before any byte open; policy re-check before each provider call.
- V4: no paths / bytes / base64 / data-URLs / raw prompts / provider bodies ever
  leave this module to the wire, history, logs, trace, or audit.
- Per-source degradation: any failure affects only that artifact and falls back to
  the stored proxy, preserving rank and the [S#] label.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.services.artifact_store import (
    RASTER_MIME_ALLOWLIST,
    compute_asset_rel_path,
    resolve_confined,
    sniff_raster_mime,
)
from app.services.model_provider_policy import (
    ProviderPolicyError,
    assert_model_provider_allowed,
)
from app.services.multimodal_enrichment import (
    ERR_POLICY,
    MultimodalProviderClient,
    MultimodalProviderError,
    _read_bounded,
)
from app.services.multimodal_prompts import (
    MAX_OBSERVATION_CHARS,
    build_image_content_part,
    build_query_messages,
    build_query_user_text,
)
from app.services.security_audit import record_security_event

logger = logging.getLogger(__name__)

# Per-source vision_status vocabulary (locked by issue #462).
VISION_USED = "used"
VISION_PROXY_ONLY = "proxy_only"
VISION_POLICY_BLOCKED = "policy_blocked"
VISION_ASSET_MISSING = "asset_missing"
VISION_PROVIDER_UNAVAILABLE = "provider_unavailable"
# Issue #480 (B2): the provider answered 200 but produced empty/whitespace output.
# Distinct from VISION_PROVIDER_UNAVAILABLE (a true outage: network error, timeout,
# or HTTP failure) so observability counters / badges don't conflate "provider down"
# with "provider up but returned nothing useful".
VISION_EMPTY_RESPONSE = "empty_response"

_VISION_STATUSES = frozenset(
    {
        VISION_USED,
        VISION_PROXY_ONLY,
        VISION_POLICY_BLOCKED,
        VISION_ASSET_MISSING,
        VISION_PROVIDER_UNAVAILABLE,
        VISION_EMPTY_RESPONSE,
    }
)

# Modalities that may be sent to the VLM when they have a raster asset.
_ELIGIBLE_MODALITIES = frozenset({"image", "chart", "table", "equation"})

_VISION_QUERY_SELECT = """
SELECT a.atom_id AS atom_id, a.file_id AS file_id,
       a.asset_id AS asset_id, a.kind AS kind,
       a.generation_hash AS generation_hash, a.page_number AS page_number,
       a.raw_text AS raw_text, a.caption AS caption
FROM document_atoms a
JOIN files f ON f.id = a.file_id
WHERE a.atom_id = ? AND f.vault_id = ?
"""


@dataclass
class VisionEvidenceResult:
    """Safe aggregate result of a query-time vision run (all counts/bounded only)."""

    observations: Dict[str, str] = field(default_factory=dict)  # artifact_id -> observation
    statuses: Dict[str, str] = field(default_factory=dict)  # artifact_id -> vision_status
    eligible: int = 0
    selected: int = 0
    deduped: int = 0
    capped: int = 0
    vlm_used: int = 0
    proxy_only: int = 0
    policy_blocked: int = 0
    asset_missing: int = 0
    provider_unavailable: int = 0
    empty_response: int = 0  # issue #480 (B2): provider up but empty output
    latency_ms: Optional[float] = None
    payload_bytes: int = 0

    def status_for(self, artifact_id: Optional[str]) -> Optional[str]:
        return self.statuses.get(artifact_id) if artifact_id else None

    def observation_for(self, artifact_id: Optional[str]) -> Optional[str]:
        return self.observations.get(artifact_id) if artifact_id else None


class VisionEvidenceError(Exception):
    """Raised for unexpected whole-batch failures (safe, generic message only)."""


@dataclass
class VisionRunContext:
    """Context injected into ``RAGEngine.query()`` to enable query-time vision.

    Built by the stream/non-stream chat handlers (which hold the authenticated user,
    the policy evaluator, and a DB connection). Excluded callers (eval/retrieval-only)
    omit it entirely, so vision never runs on those paths (V1).
    """

    service: "VisionEvidenceService"
    user: Any = None
    evaluate: Optional[Callable] = None


def _pixel_dims(data: bytes) -> Optional[tuple[int, int]]:
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001 — Pillow optional
        return None
    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            return (img.width or 0, img.height or 0)
    except Exception:  # noqa: BLE001
        return None


def _validate_observation(raw: str) -> Optional[str]:
    """Bound/validate an untrusted model observation. Returns None if unusable."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())  # collapse control/newlines/whitespace
    if not text:
        return None
    return text[:MAX_OBSERVATION_CHARS]


def _vault_opted_in(conn: Any, vault_id: int) -> bool:
    """Explicit per-vault multimodal provider opt-in (fail-closed: NULL/0 = off)."""
    if conn is None or vault_id is None:
        return False
    try:
        row = conn.execute(
            "SELECT multimodal_provider_enabled FROM vaults WHERE id = ?",
            (vault_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    if row is None:
        return False
    return bool(row["multimodal_provider_enabled"])


@asynccontextmanager
async def _conn_ctx():
    """Yield a short-lived pooled DB connection (closed on exit).

    The stream path deliberately releases its request-scoped connection before
    generation, so the vision service opens its own short-lived pooled connection
    per task instead of sharing one across concurrent artifact workers.
    """
    from app.models.database import get_pool

    pool = get_pool(str(settings.sqlite_path))
    with pool.connection() as conn:
        yield conn


async def _can_read(user: Any, evaluate: Optional[Callable], conn: Any, vault_id: int) -> bool:
    """Vault read authorization (self-checked when no DI evaluate is available).

    Fails closed: a missing principal (``None``) or evaluation error denies.

    Issue #480 (D3): when no DI ``evaluate`` is available (the streaming chat
    path, which releases its request-scoped connection BEFORE vision runs per
    the S-003 no-double-connection invariant), this fallback opens a FRESH
    short-lived pooled connection per check and evaluates via the shared
    service-layer policy (``app.services.authz_policy``) — NOT ``app.api.deps``,
    which would invert the services→api dependency direction. Do NOT "optimize"
    this by reusing a captured stream-path connection: that conn is already
    released.
    """
    if user is None:
        return False
    if evaluate is not None:
        try:
            return bool(await evaluate(user, "vault", vault_id, "read"))
        except Exception:  # noqa: BLE001
            return False
    try:
        from app.services.authz_policy import evaluate_policy

        return bool(await evaluate_policy(conn, user, "vault", vault_id, "read"))
    except Exception:  # noqa: BLE001
        return False



class VisionEvidenceService:
    """Query-time retrieval-first vision orchestrator (narrow, cancellable)."""

    def __init__(self, client_factory: Optional[Callable[..., MultimodalProviderClient]] = None) -> None:
        self._client_factory = client_factory or MultimodalProviderClient

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------
    def _whole_batch_allowed(self, conn: Any, vault_id: int) -> Optional[str]:
        """Return a policy-block reason if NO artifact may be sent, else None."""
        if not settings.multimodal_query_vision_enabled:
            return "query_vision_disabled"
        if not _vault_opted_in(conn, vault_id):
            return "vault_not_opted_in"
        base_url = (settings.multimodal_chat_url or "").strip()
        if not base_url:
            return "no_provider_configured"
        allow = list(settings.multimodal_allowed_model_origins or [])
        try:
            assert_model_provider_allowed(
                base_url, sensitive=True, ordinary_allowlist=allow, sensitive_allowlist=allow
            )
        except ProviderPolicyError:
            return "provider_origin_not_allowed"
        return None

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def _select(self, sources: List) -> List:
        """Determine deterministic rank-ordered eligible artifact candidates.

        Eligibility = artifact identity + eligible modality + an asset id. Dedup by
        (artifact_id, asset_id). Byte/pixel/asset-count caps are applied later before
        byte load so no file is opened for an over-cap candidate.
        """
        seen: set = set()
        selected: List = []
        for src in sources:
            if not getattr(src, "artifact_id", None):
                continue
            if getattr(src, "modality", None) not in _ELIGIBLE_MODALITIES:
                continue
            if not getattr(src, "asset_id", None):
                continue
            key = (src.artifact_id, src.asset_id)
            if key in seen:
                continue
            seen.add(key)
            selected.append(src)
        return selected

    # ------------------------------------------------------------------
    # Per-asset load + call (V3 normative ordering)
    # ------------------------------------------------------------------
    async def _process_one(
        self,
        *,
        query: str,
        src: Any,
        vault_id: int,
        user: Any,
        evaluate: Callable,
        semaphore: asyncio.Semaphore,
        client: Optional[MultimodalProviderClient] = None,
    ) -> tuple[str, str, Optional[str]]:
        """Load/authorize/call for a single artifact.

        Returns ``(artifact_id, vision_status, observation_or_None)``. Every failure
        degrades to a safe status — never raises to the caller loop. Opens its own
        short-lived connection so concurrent workers never share a sqlite conn.

        ``client`` is a SHARED provider client amortized across the whole batch by
        ``run()`` (issue #480 D1) — reuses one TCP/TLS connection pool instead of
        creating+closing a fresh client per artifact. The per-call policy re-check
        (``_assert_policy`` inside ``chat_multimodal``) is preserved, so a mid-batch
        kill-switch flip still blocks the next call regardless of client reuse.
        """
        artifact_id = src.artifact_id

        async def _run() -> tuple[str, str, Optional[str]]:
            async with _conn_ctx() as conn:
                # (1) Vault read re-check (authz).
                if not await _can_read(user, evaluate, conn, vault_id):
                    return artifact_id, VISION_POLICY_BLOCKED, None

                # (2) Joined artifact->file->vault membership + authoritative metadata.
                # Issue #480 (D2): offload blocking sqlite to a worker thread,
                # aligned with the documents.py convention. The pool opens
                # connections with check_same_thread=False so cross-thread use is safe.
                try:
                    row = await asyncio.to_thread(
                        lambda: conn.execute(
                            _VISION_QUERY_SELECT, (artifact_id, vault_id)
                        ).fetchone()
                    )
                except Exception:  # noqa: BLE001
                    row = None
                if row is None:
                    return artifact_id, VISION_POLICY_BLOCKED, None
                file_id = int(row["file_id"])
                asset_id = row["asset_id"]
                generation_hash = row["generation_hash"]
                if not asset_id:
                    return artifact_id, VISION_ASSET_MISSING, None

                # (3) Confined relative asset path (no caller path accepted).
                rel = compute_asset_rel_path(file_id, generation_hash, asset_id)
                path = resolve_confined(rel, vault_id)
                if path is None:
                    return artifact_id, VISION_ASSET_MISSING, None

                # (4) Policy re-check immediately before any byte open (and again before
                # the call via the client). Global switch + vault opt-in read live.
                blocked = await asyncio.to_thread(
                    self._whole_batch_allowed, conn, vault_id
                )
                if blocked is not None:
                    await self._audit(conn, vault_id, file_id, artifact_id, asset_id, "denied:policy")
                    return artifact_id, VISION_POLICY_BLOCKED, None

                # (5) Open + read bounded bytes NOW (after the above gates).
                data = await asyncio.to_thread(
                    _read_bounded, path, int(settings.multimodal_max_asset_bytes)
                )
                if data is None or not data:
                    return artifact_id, VISION_ASSET_MISSING, None

                # (6) Header-derived MIME + pixel cap (allowlist; no path inference).
                mime = sniff_raster_mime(data)
                if mime not in RASTER_MIME_ALLOWLIST:
                    return artifact_id, VISION_ASSET_MISSING, None
                dims = _pixel_dims(data)
                if dims is not None:
                    if dims[0] * dims[1] > int(settings.multimodal_max_pixels):
                        return artifact_id, VISION_ASSET_MISSING, None

                # (7) Compose image part + query-conditioned user text; call provider.
                try:
                    image_part = build_image_content_part(mime, data, None)
                except Exception:  # noqa: BLE001 — unsupported/oversize asset
                    return artifact_id, VISION_ASSET_MISSING, None
                user_text = build_query_user_text(
                    query=query,
                    kind=row["kind"] or src.modality or "",
                    offline_description=getattr(src, "description", None) or None,
                    raw_evidence=row["raw_text"],
                    caption=row["caption"],
                    page_number=row["page_number"],
                )
                messages = build_query_messages(user_text, image_part)

                raw_bytes = len(data) + len(user_text.encode("utf-8"))
                started = time.perf_counter()
                try:
                    output = await client.chat_multimodal(messages, max_tokens=512)
                    await self._audit(conn, vault_id, file_id, artifact_id, asset_id, "used")
                except MultimodalProviderError as exc:
                    if exc.code in (ERR_POLICY,):
                        await self._audit(conn, vault_id, file_id, artifact_id, asset_id, "denied:policy")
                        return artifact_id, VISION_POLICY_BLOCKED, None
                    await self._audit(conn, vault_id, file_id, artifact_id, asset_id, "degraded:provider")
                    return artifact_id, VISION_PROVIDER_UNAVAILABLE, None
                except Exception:  # noqa: BLE001 — cancellation propagates naturally here
                    await self._audit(conn, vault_id, file_id, artifact_id, asset_id, "degraded:provider")
                    return artifact_id, VISION_PROVIDER_UNAVAILABLE, None
                finally:
                    # Issue #480 (D1): the client is owned + closed by run(); do NOT
                    # close it per-task. Latency/byte accounting stays per-call.
                    self._record_latency_bytes(started, raw_bytes)

                observation = _validate_observation(output)
                if observation is None:
                    # Issue #480 (B2): the provider call SUCCEEDED (no exception
                    # reached here) but produced empty/whitespace output. This is
                    # distinct from VISION_PROVIDER_UNAVAILABLE (a true outage:
                    # error/timeout) — report it as empty_response so counters and
                    # the proxy badge don't conflate "provider up but empty" with
                    # "provider down".
                    return artifact_id, VISION_EMPTY_RESPONSE, None
                return artifact_id, VISION_USED, observation

        async with semaphore:
            try:
                return await asyncio.wait_for(_run(), timeout=float(settings.multimodal_timeout_seconds))
            except asyncio.TimeoutError:
                return artifact_id, VISION_PROVIDER_UNAVAILABLE, None
            except Exception:  # noqa: BLE001 — never raise to the caller loop (F-003)
                # Any unexpected failure for THIS artifact (e.g. _conn_ctx pool
                # exhaustion) degrades only that source; the sibling gather tasks
                # still report their own statuses and the batch is not discarded.
                # Ordering matters: `asyncio.TimeoutError is builtin TimeoutError`
                # (an Exception) on 3.11, so this handler MUST come after the
                # TimeoutError branch above. CancelledError is BaseException and
                # still propagates, preserving cancellation.
                return artifact_id, VISION_PROVIDER_UNAVAILABLE, None

    def _record_latency_bytes(self, started: float, payload_bytes: int) -> None:
        # Safe per-source aggregate accumulator (latency + byte counts only).
        try:
            self._latency_ms = max(0.0, (time.perf_counter() - started) * 1000.0) + getattr(
                self, "_latency_ms", 0.0
            )
            self._payload_bytes = payload_bytes + getattr(self, "_payload_bytes", 0)
        except Exception:  # noqa: BLE001  # nosec B110 - best-effort accumulator
            pass

    async def _audit(
        self,
        conn: Any,
        vault_id: int,
        file_id: int,
        artifact_id: str,
        asset_id: Optional[str],
        outcome: str,
    ) -> None:
        """Record a query-vision external-transmission attempt/denial (ids only).

        Issue #480 (D2): offloads the blocking sqlite insert to a worker thread,
        aligned with the documents.py convention (pool uses check_same_thread=False).
        Best-effort: an audit failure must never break the degradation path.
        """
        if conn is None:
            return
        try:
            await asyncio.to_thread(
                lambda: record_security_event(
                    conn,
                    event_type="attempted_external_transmission",
                    metadata={
                        "vault_id": vault_id,
                        "file_id": file_id,
                        "artifact_id": artifact_id,
                        "asset_id": asset_id,
                        "purpose": "query_vision",
                        "outcome": outcome,
                    },
                )
            )
        except Exception:  # noqa: BLE001  # nosec B110 - audit must never break degradation path
            pass

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        query: str,
        sources: List,
        vault_id: int,
        user: Any = None,
        evaluate: Callable = None,
    ) -> VisionEvidenceResult:
        """Run the query-time vision step over eligible final-winner sources."""
        result = VisionEvidenceResult()
        result.eligible = sum(
            1
            for s in sources
            if getattr(s, "artifact_id", None)
            and getattr(s, "modality", None) in _ELIGIBLE_MODALITIES
            and getattr(s, "asset_id", None)
        )
        selected = self._select(sources)
        result.selected = len(selected)

        # V5 feature-off non-regression (F-03): when query-time vision is disabled
        # (the default), return an empty result BEFORE the `not selected` branch so
        # ineligible-modality artifact sources (e.g. code) do not leak
        # VISION_PROXY_ONLY onto the wire. vision_status must be omitted entirely.
        if not settings.multimodal_query_vision_enabled:
            return result

        # Whole-batch gate: if no artifact may be sent, degrade all to proxy_only
        # without any byte open / provider call.
        if not selected:
            result.proxy_only = 0
            for s in sources:
                if getattr(s, "artifact_id", None):
                    result.statuses[s.artifact_id] = VISION_PROXY_ONLY
                    result.proxy_only += 1
            return result

        # Whole-batch policy gate (global switch + vault opt-in + origin/SSRF) on a
        # short-lived connection. Issue #480 (D2): offload the blocking sqlite read
        # to a worker thread (pool uses check_same_thread=False).
        async with _conn_ctx() as conn:
            blocked = await asyncio.to_thread(self._whole_batch_allowed, conn, vault_id)
        if blocked is not None:
            if blocked == "query_vision_disabled":
                # V5 feature-off non-regression: vision is never attempted and
                # artifact vision_status is NOT set (omitted from wire), so a
                # deployment where the feature was never enabled does not render a
                # misleading "policy_blocked" badge on artifact sources.
                return result
            for s in selected:
                result.statuses[s.artifact_id] = VISION_POLICY_BLOCKED
                result.policy_blocked += 1
            return result

        # Cap to max assets per batch (rank order, deterministic).
        cap = int(settings.multimodal_max_assets_per_batch)
        if len(selected) > cap:
            result.capped = len(selected) - cap
            selected = selected[:cap]

        self._latency_ms = 0.0
        self._payload_bytes = 0
        semaphore = asyncio.Semaphore(int(settings.multimodal_concurrency))
        # Issue #480 (D1): amortize ONE provider client across the whole batch —
        # reuses a single TCP/TLS connection pool instead of creating+closing a
        # fresh client per artifact. The per-call policy re-check (_assert_policy
        # inside chat_multimodal) is preserved, so a mid-batch kill-switch flip
        # still blocks the next call. Closed once in the finally below.
        client = self._client_factory(purpose="query")
        await client.start()
        try:
            tasks = [
                self._process_one(
                    query=query,
                    src=s,
                    vault_id=vault_id,
                    user=user,
                    evaluate=evaluate,
                    semaphore=semaphore,
                    client=client,
                )
                for s in selected
            ]
            outcomes = await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            await client.close()
        for artifact_id, status, observation in outcomes:
            result.statuses[artifact_id] = status
            if status == VISION_USED and observation:
                result.observations[artifact_id] = observation
            if status == VISION_USED:
                result.vlm_used += 1
            elif status == VISION_PROXY_ONLY:
                result.proxy_only += 1
            elif status == VISION_POLICY_BLOCKED:
                result.policy_blocked += 1
            elif status == VISION_ASSET_MISSING:
                result.asset_missing += 1
            elif status == VISION_PROVIDER_UNAVAILABLE:
                result.provider_unavailable += 1
            elif status == VISION_EMPTY_RESPONSE:
                result.empty_response += 1

        result.latency_ms = self._latency_ms
        result.payload_bytes = self._payload_bytes
        return result


def apply_vision_to_sources(result: VisionEvidenceResult, sources: List) -> None:
    """Attach vision_status + internal observation to artifact sources in place.

    Mutates the authoritative ``relevant_chunks`` list so BOTH prompt construction
    and done/reource serialization reflect the status; the observation feeds the
    prompt and support-text registry but is never serialized (V2/V4).
    """
    for src in sources:
        art = getattr(src, "artifact_id", None)
        if not art:
            continue
        status = result.statuses.get(art)
        if status:
            src.vision_status = status
        obs = result.observation_for(art)
        if obs:
            src.vision_observation = obs
