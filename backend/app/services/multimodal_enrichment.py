"""Bounded multimodal enrichment client/service for typed artifacts (issue #461).

Provides:

- :class:`MultimodalProviderClient`: a narrow OpenAI-compatible multimodal client
  (separate from the string-only :class:`app.services.llm_client.LLMClient`) with
  ``follow_redirects=False``, ``SSRFSafeTransport``, and an exact-origin policy check
  immediately before every call.
- :class:`ArtifactEnrichmentService`: orchestrates atom-scoped enrichment — validates
  typed asset membership/root containment, enforces byte/pixel caps, builds bounded
  injection-hardened context, calls the provider, validates the untrusted response into
  a versioned derived record, persists the derived row + atom stage state, records a safe
  audit event, and returns the deterministic proxy record for vector write.

Effective authorization (checked before enqueue AND immediately before every call):
``global multimodal_enrichment_enabled AND vault opt-in AND exact-origin allowlisted AND
SSRF-safe``. Provider output is untrusted; raw evidence is never rewritten.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import enrichment_state as st
from app.services.artifact_store import compute_asset_rel_path, resolve_confined
from app.services.model_provider_policy import (
    ProviderPolicyError,
    assert_model_provider_allowed,
    model_provider_snapshot,
)
from app.services.multimodal_prompts import (
    ACCEPTED_RASTER_MIMES,
    DerivedError,
    build_artifact_context,
    build_image_content_part,
    build_messages,
    build_user_prompt_text,
    parse_derived_response,
)
from app.services.security_audit import record_security_event
from app.services.ssrf_transport import SSRFSafeTransport

logger = logging.getLogger(__name__)

# Stable, bounded, content-free error codes (never raw provider errors/content).
ERR_POLICY = "provider_policy_denied"
ERR_NETWORK = "provider_network_failure"
ERR_TIMEOUT = "provider_timeout"
ERR_PROVIDER_HTTP = "provider_http_error"
ERR_RATE = "provider_rate_limited"
ERR_SCHEMA = "provider_invalid_schema"
ERR_INVALID_ASSET = "invalid_asset"
ERR_ASSET_NO_BYTES = "asset_bytes_unavailable"
ERR_ASSET_OVERSIZE = "asset_oversize_pixels"

# Cap for the persisted/embedded proxy text. Must stay at or below the embedding
# service's per-text maximum (embeddings.MAX_TEXT_LENGTH=8192) so embed_batch never
# raises on a schema-compliant provider response after the stage is committed
# (F-1). Kept as a module constant so the service and its tests share one value.
_EMBED_TEXT_CAP = 8000


def _parse_bounded_bbox(bbox_json: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse a stored ``bbox_json`` into a bounded canonical ``{x0,y0,x1,y1}``.

    Only finite numbers, nonnegative width/height (x1>=x0, y1>=y0), absolute coords
    <= 1e6, and EXACTLY the canonical keys are accepted (P4 contract, issue #462).
    Malformed / NaN / inf / negative / oversized / extra-key inputs => None.
    """
    if not bbox_json:
        return None
    try:
        raw = json.loads(bbox_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    if set(raw.keys()) != {"x0", "y0", "x1", "y1"}:
        return None
    parts: dict[str, float] = {}
    for key in ("x0", "y0", "x1", "y1"):
        val = raw.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        fv = float(val)
        if not math.isfinite(fv):
            return None
        parts[key] = fv
    if parts["x1"] < parts["x0"] or parts["y1"] < parts["y0"]:
        return None
    if any(abs(v) > 1e6 for v in parts.values()):
        return None
    return parts


class MultimodalProviderError(Exception):
    """Classified provider error with a stable code and retryability."""

    def __init__(self, code: str, *, retryable: bool, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MultimodalProviderClient:
    """Narrow OpenAI-compatible multimodal chat client (dedicated transport)."""

    def __init__(
        self,
        *,
        base_url: str = "",
        model: str = "",
        timeout: Optional[float] = None,
        concurrency: Optional[int] = None,
        purpose: str = "enrich",
    ) -> None:
        self.base_url = base_url or settings.multimodal_chat_url
        self.model = model or settings.multimodal_model
        self.logical_mode = settings.multimodal_mode or "thinking"
        self.timeout = timeout if timeout is not None else settings.multimodal_timeout_seconds
        # None default so settings.multimodal_concurrency is actually honored when
        # the client is constructed without an explicit value (lifespan does this).
        self.concurrency = concurrency if concurrency is not None else settings.multimodal_concurrency
        # "enrich" = offline artifact enrichment (#461); "query" = query-time
        # retrieval-first VLM (#462). Selects the master switch checked in
        # _assert_policy (they share the same origin allowlist + SSRF gate).
        self.purpose = purpose
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def reconfigure(self, *, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        if base_url is not None:
            self.base_url = base_url
        if model is not None:
            self.model = model

    def _assert_policy(self) -> None:
        """Re-verify the authorizing gates immediately before a provider call.

        Enforces the purpose-specific global kill switch (enrichment xor query-time
        vision) and the exact-origin allowlist + SSRF gate (sensitive tier). Checking
        the global switch here closes the race where a mid-run revocation would
        otherwise let an already-authorized asset egress after the operator disabled
        the feature — the documented invariant is that revocation stops further
        outbound calls.
        """
        # Global kill switch: re-read live settings so a flip lands even for assets
        # that cleared the service-level gate before the revocation.
        if self.purpose == "query":
            if not settings.multimodal_query_vision_enabled:
                raise MultimodalProviderError(
                    ERR_POLICY, retryable=False, message="query vision disabled"
                )
        elif not settings.multimodal_enrichment_enabled:
            raise MultimodalProviderError(
                ERR_POLICY, retryable=False, message="multimodal enrichment disabled"
            )
        allow = list(settings.multimodal_allowed_model_origins or [])
        try:
            assert_model_provider_allowed(
                self.base_url,
                sensitive=True,
                ordinary_allowlist=allow,
                sensitive_allowlist=allow,
            )
        except ProviderPolicyError as exc:
            raise MultimodalProviderError(ERR_POLICY, retryable=False, message=str(exc)) from exc

    async def start(self) -> None:
        if self._client is not None:
            return
        limits = httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency)
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=SSRFSafeTransport(transport=httpx.AsyncHTTPTransport(limits=limits)),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat_multimodal(self, messages: list[dict[str, Any]], max_tokens: int = 1024) -> str:
        """Send a multimodal chat payload; enforce policy immediately before the call.

        Raises :class:`MultimodalProviderError` with retryable classification.
        """
        self._assert_policy()
        if self._client is None:
            await self.start()
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        # Enforce the per-request asset-count cap (multimodal_max_assets_per_batch).
        asset_parts = _count_image_parts(messages)
        if asset_parts > settings.multimodal_max_assets_per_batch:
            raise MultimodalProviderError(
                ERR_ASSET_OVERSIZE, retryable=False, message="too many assets per batch"
            )
        # Enforce the total payload byte cap (multimodal_max_total_payload_bytes).
        payload_bytes = json.dumps(payload).encode("utf-8")
        if len(payload_bytes) > settings.multimodal_max_total_payload_bytes:
            raise MultimodalProviderError(
                ERR_ASSET_OVERSIZE, retryable=False, message="payload exceeds total byte cap"
            )
        async with self._semaphore:
            try:
                resp = await self._client.post(url, json=payload)
            except httpx.TimeoutException as exc:
                raise MultimodalProviderError(ERR_TIMEOUT, retryable=True) from exc
            except httpx.RequestError as exc:
                raise MultimodalProviderError(ERR_NETWORK, retryable=True) from exc

        # Provider must not return 30x (follow_redirects=False): accept a direct
        # response only, so the payload can never hop to an un-allowlisted origin.
        if 300 <= resp.status_code < 400:
            raise MultimodalProviderError(ERR_PROVIDER_HTTP, retryable=False)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise MultimodalProviderError(ERR_RATE if resp.status_code == 429 else ERR_PROVIDER_HTTP, retryable=True)
        if resp.status_code >= 400:
            raise MultimodalProviderError(ERR_PROVIDER_HTTP, retryable=False)

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MultimodalProviderError(ERR_SCHEMA, retryable=False) from exc
        if not isinstance(content, str):
            raise MultimodalProviderError(ERR_SCHEMA, retryable=False)
        return content


def build_proxy_text(*, raw_evidence: str, kind: str, description: str, retrieval_aids: list[str]) -> str:
    """Deterministic proxy text: raw typed evidence + validated derived fields.

    Field order is fixed and documented (raw kind label, raw evidence, description,
    aids) so the proxy is stable and reconstructible.
    """
    parts: list[str] = []
    if kind:
        parts.append(f"[{kind}]")
    if raw_evidence and raw_evidence.strip():
        parts.append(raw_evidence.strip())
    if description and description.strip():
        parts.append(description.strip())
    for aid in retrieval_aids:
        if aid.strip():
            parts.append(aid.strip())
    return "\n".join(parts)


class ArtifactEnrichmentService:
    """Atom-scoped multimodal enrichment orchestration."""

    def __init__(
        self,
        *,
        pool=None,
        client: Optional[MultimodalProviderClient] = None,
    ) -> None:
        self.pool = pool
        self._client = client

    @property
    def client(self) -> MultimodalProviderClient:
        if self._client is None:
            self._client = MultimodalProviderClient()
        return self._client

    def _authorized(self, *, vault_id: int, file_id: int) -> tuple[bool, str]:
        """Return (authorized, reason) combining global + vault + allowlist + SSRF."""
        if not settings.multimodal_enrichment_enabled:
            return False, "global_disabled"
        if not _vault_multimodal_enabled(self.pool, vault_id):
            return False, "vault_not_opted_in"
        allow = list(settings.multimodal_allowed_model_origins or [])
        if not allow or not settings.multimodal_chat_url:
            return False, "no_allowlisted_origin"
        try:
            self.client._assert_policy()
        except MultimodalProviderError:
            return False, "policy_denied"
        return True, ""

    async def enrich_atoms(
        self,
        *,
        vault_id: int,
        file_id: int,
        generation_hash: str,
        document_title: str,
    ) -> list[dict[str, Any]]:
        """Enrich all actionable atoms for a file/generation; return proxy records.

        Never holds a DB connection across a provider/filesystem call (short pooled
        claims only, matching the #460/no-connection-over-long-ops contract).

        Returns a list of proxy records to be written through the LanceDB path
        (add-then-delete) by the caller. On permanent/policy failures the atom stage is
        marked accordingly and the base/raw proxy remains untouched.
        """
        proxy_records: list[dict[str, Any]] = []

        # Read the ordered atom list (short claim) for neighbor context.
        atoms = self._load_ordered_atoms(file_id, generation_hash)
        if not atoms:
            return proxy_records
        # Only atoms whose stage is actionable are enriched. This skips
        # already-succeeded atoms (fingerprint/status-based skip), so a
        # retry or re-run does not re-transmit unchanged atoms to the
        # external provider.
        actionable = self._actionable_atom_pks(file_id, generation_hash)
        candidates = [a for a in atoms if a["atom_pk"] in actionable]

        tasks = [
            self._enrich_atom(
                atom=atom, vault_id=vault_id, file_id=file_id,
                generation_hash=generation_hash,
                neighbors=self._neighbors(atoms, atom),
                document_title=document_title,
            )
            for atom in candidates
            if str(atom.get("kind", "")).lower()
            in ("image", "chart", "table", "equation")
        ]
        if not tasks:
            return proxy_records
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, dict) and result.get("proxy_record"):
                proxy_records.append(result["proxy_record"])
        return proxy_records

    def _actionable_atom_pks(self, file_id: int, generation_hash: str) -> set[int]:
        """Return atom PKs whose enrich stage is pending / retryable / re-runnable."""
        if self.pool is None:
            return set()
        with self.pool.connection() as conn:
            rows = st.list_enrichable_atoms(
                conn, file_id=file_id, generation_hash=generation_hash,
                stage=st.ENRICH_STAGE,
            )
        return {int(r["atom_pk"]) for r in rows}

    def _load_ordered_atoms(self, file_id: int, generation_hash: str) -> list[dict]:
        if self.pool is None:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id AS atom_pk, atom_id, ordinal, kind, raw_text, page_number, "
                "bbox_json, caption, asset_id, section_path_json "
                "FROM document_atoms WHERE file_id = ? AND generation_hash = ? "
                "ORDER BY ordinal",
                (file_id, generation_hash),
            ).fetchall()
            return [dict(r) for r in rows]

    def _neighbors(self, atoms: list[dict], atom: dict, window: int = 2) -> tuple[list[dict], list[dict]]:
        idx = next((i for i, a in enumerate(atoms) if a["atom_pk"] == atom["atom_pk"]), 0)
        pre = atoms[max(0, idx - window): idx]
        post = atoms[idx + 1: idx + 1 + window]
        return pre, post

    async def _enrich_atom(
        self,
        *,
        atom: dict,
        vault_id: int,
        file_id: int,
        generation_hash: str,
        neighbors: tuple[list[dict], list[dict]],
        document_title: str,
    ) -> dict[str, Any]:
        atom_pk: int = atom["atom_pk"]
        atom_id: str = atom["atom_id"]
        kind = str(atom.get("kind", "")).lower()
        snapshot: dict[str, str] = {}

        authorized, reason = self._authorized(vault_id=vault_id, file_id=file_id)
        if not authorized:
            status = (
                st.SKIPPED_POLICY
                if reason
                in ("global_disabled", "vault_not_opted_in", "no_allowlisted_origin", "policy_denied")
                else st.SKIPPED_NOT_APPLICABLE
            )
            self._skip(atom_pk, file_id, generation_hash, status, "policy_denied")
            # Audit the DENIAL too (no outbound call was made; outcome records why).
            self._audit(
                vault_id, file_id, atom_id, atom.get("asset_id"),
                "attempted_external_transmission", {}, f"denied:{reason}",
            )
            return {}

        # Compute fingerprint (before claim; no provider call).
        input_fingerprint = st.compute_input_fingerprint(
            generation_hash=generation_hash,
            asset_sha=atom.get("asset_id"),
            neighbor_hashes=self._neighbor_hashes(neighbors),
            atom_schema_version=1,
            impl_version=settings.multimodal_impl_version,
            prompt_version=settings.multimodal_prompt_version,
            model=settings.multimodal_model,
            logical_mode=settings.multimodal_mode,
            response_schema_version=settings.multimodal_schema_version,
            max_pixels=settings.multimodal_max_pixels,
            max_asset_bytes=settings.multimodal_max_asset_bytes,
        )

        # Claim atom stage running (short claim; release before provider call).
        with self.pool.connection() as conn:
            st.claim_atom_stage(
                conn, file_id=file_id, generation_hash=generation_hash, atom_pk=atom_pk,
                stage=st.ENRICH_STAGE, input_fingerprint=input_fingerprint,
                implementation_version=settings.multimodal_impl_version,
                model_id=settings.multimodal_model or None,
                prompt_id=settings.multimodal_prompt_version,
                config_id=settings.multimodal_schema_version,
            )
            conn.commit()

        try:
            raw_evidence = atom.get("raw_text") or ""
            caption = atom.get("caption")
            pre, post = neighbors
            context = build_artifact_context(
                title=document_title,
                section_path=tuple(),
                kind=kind,
                caption=caption,
                preceding_prose=tuple(p.get("raw_text", "") for p in pre if p.get("raw_text")),
                following_prose=tuple(p.get("raw_text", "") for p in post if p.get("raw_text")),
                raw_evidence=raw_evidence,
                page_number=atom.get("page_number"),
            )
            user_text = build_user_prompt_text(context)

            # Asset: validate/contain + load bytes; build separate image content part.
            image_part = await self._load_image_part(atom, vault_id, file_id, generation_hash)
            messages = build_messages(user_text, image_part)

            # Policy re-checked inside chat_multimodal immediately before the call.
            output = await self.client.chat_multimodal(messages, max_tokens=1024)
            snapshot = self._snapshot()
            derived = parse_derived_response(output, settings.multimodal_schema_version)
            description = derived["description"]
            aids = derived["retrieval_aids"]
        except MultimodalProviderError as exc:
            self._fail(atom_pk, file_id, generation_hash, input_fingerprint, exc.code, exc.retryable)
            self._audit(vault_id, file_id, atom_id, atom.get("asset_id"), "attempted_external_transmission", snapshot, "failed")
            return {"atom_id": atom_id, "outcome": "failed", "code": exc.code}
        except DerivedError:
            self._fail(atom_pk, file_id, generation_hash, input_fingerprint, ERR_SCHEMA, False)
            self._audit(vault_id, file_id, atom_id, atom.get("asset_id"), "attempted_external_transmission", snapshot, "failed")
            return {"atom_id": atom_id, "outcome": "failed", "code": ERR_SCHEMA}
        except Exception as exc:  # noqa: BLE001 — bounded classification
            logger.warning("Multimodal enrichment unexpected error atom=%s: %s", atom_id, type(exc).__name__)
            self._fail(atom_pk, file_id, generation_hash, input_fingerprint, ERR_NETWORK, True)
            return {"atom_id": atom_id, "outcome": "failed", "code": ERR_NETWORK}

        # Persist derived + succeed (short claim). Reject stale fingerprint.
        with self.pool.connection() as conn:
            ok = st.complete_atom_stage(
                conn, file_id=file_id, generation_hash=generation_hash, atom_pk=atom_pk,
                stage=st.ENRICH_STAGE, input_fingerprint=input_fingerprint,
                status=st.SUCCEEDED, attempts=1,
            )
            if ok:
                st.upsert_derived(
                    conn, file_id=file_id, generation_hash=generation_hash, atom_id=atom_id,
                    input_fingerprint=input_fingerprint, description=description,
                    retrieval_aids=aids, prompt_version=settings.multimodal_prompt_version,
                    schema_version=settings.multimodal_schema_version,
                    impl_version=settings.multimodal_impl_version,
                    provider_snapshot=snapshot,
                )
            conn.commit()

        self._audit(vault_id, file_id, atom_id, atom.get("asset_id"), "attempted_external_transmission", snapshot, "succeeded")
        raw_proxy = build_proxy_text(raw_evidence=raw_evidence, kind=kind, description=description, retrieval_aids=aids)
        # Bound the proxy text so it can never exceed the embedding service's
        # per-text cap (embeddings.MAX_TEXT_LENGTH=8192): a schema-compliant
        # provider response (20000 chars allowed) could otherwise raise
        # EmbeddingError inside _write_atom_proxies AFTER this stage is already
        # committed SUCCEEDED, leaving a permanently unrecoverable proxy-less atom
        # (F-1). Truncate to the embedding limit so the durable proxy always fits.
        proxy = raw_proxy[: _EMBED_TEXT_CAP]
        proxy_record = {
            "atom_id": atom_id,
            "atom_kind": kind,
            "asset_id": atom.get("asset_id"),
            "file_id": file_id,
            "vault_id": vault_id,
            "generation_hash": generation_hash,
            "proxy_text": proxy,
            "description": description,
            "retrieval_aids": aids,
            "status": st.SUCCEEDED,
            "fingerprint": input_fingerprint,
            # Persistent artifact presentation metadata (issue #462) so retrieval
            # can carry page_number/bbox through to the source DTO. bbox is
            # bounded/validated; never raw provider or file-path content.
            "page_number": atom.get("page_number"),
            "bbox": _parse_bounded_bbox(atom.get("bbox_json")),
        }
        return {"proxy_record": proxy_record, "atom_id": atom_id}

    async def _load_image_part(self, atom: dict, vault_id: int, file_id: int, generation_hash: str) -> dict[str, Any]:
        asset_id = atom.get("asset_id")
        if not asset_id:
            raise MultimodalProviderError(ERR_ASSET_NO_BYTES, retryable=False)
        rel = compute_asset_rel_path(file_id, generation_hash, asset_id)
        path = resolve_confined(rel, vault_id)
        if path is None or not Path(path).exists():
            raise MultimodalProviderError(ERR_INVALID_ASSET, retryable=False)
        data = await asyncio.to_thread(_read_bounded, path, settings.multimodal_max_asset_bytes)
        if data is None:
            raise MultimodalProviderError(ERR_ASSET_NO_BYTES, retryable=False)
        # MIME is derived from the image header (authoritative, and independent of
        # the asset path which carries no file extension — asset_id is an opaque
        # SHA-256 hexdigest). Non-raster is rejected before any outbound call.
        mime = _image_mime_from_peek(data)
        if mime not in ACCEPTED_RASTER_MIMES:
            raise MultimodalProviderError(ERR_INVALID_ASSET, retryable=False)
        # Enforce the decoded pixel cap (multimodal_max_pixels) so a small,
        # highly compressed file with a huge pixel surface cannot force excessive
        # provider-side decode. Dimensions are read from the header only (lazy).
        peek = _peek_image(data)
        if peek is None:
            raise MultimodalProviderError(ERR_INVALID_ASSET, retryable=False)
        dims = peek[0]
        _check_pixel_cap(dims, settings.multimodal_max_pixels)
        return build_image_content_part(mime, data, atom.get("caption"))

    def _neighbor_hashes(self, neighbors: tuple[list[dict], list[dict]]) -> tuple[str, ...]:
        h = []
        for group in neighbors:
            for a in group:
                ident = a.get("atom_id") or a.get("atom_pk")
                if ident is not None:
                    h.append(str(ident))
        return tuple(sorted(h))

    def _snapshot(self) -> dict[str, str]:
        return model_provider_snapshot(self.client)

    def _skip(self, atom_pk, file_id, generation_hash, status, reason_code) -> None:
        if self.pool is None:
            return
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ingestion_stage_states "
                "(file_id, atom_id, generation_hash, stage, status, error_code, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (file_id, atom_pk, generation_hash, st.ENRICH_STAGE, status, reason_code),
            )
            conn.commit()

    def _fail(self, atom_pk, file_id, generation_hash, input_fingerprint, code, retryable) -> None:
        if self.pool is None:
            return
        status = st.FAILED_RETRYABLE if retryable else st.FAILED_PERMANENT
        with self.pool.connection() as conn:
            st.complete_atom_stage(
                conn, file_id=file_id, generation_hash=generation_hash, atom_pk=atom_pk,
                stage=st.ENRICH_STAGE, input_fingerprint=input_fingerprint,
                status=status, error_code=code, error_message=None, attempts=1,
            )
            conn.commit()

    def _audit(self, vault_id, file_id, atom_id, asset_id, event_type, snapshot, outcome) -> None:
        metadata = {
            "vault_id": vault_id,
            "file_id": file_id,
            "atom_id": atom_id,
            "asset_id": asset_id,
            "purpose": "artifact_enrichment",
            "prompt_version": settings.multimodal_prompt_version,
            "provider_snapshot": snapshot,
            "outcome": outcome,
        }
        if self.pool is None:
            return
        try:
            with self.pool.connection() as conn:
                record_security_event(conn, event_type=event_type, metadata=metadata)
        except Exception as exc:  # noqa: BLE001 — audit must never break enrichment
            logger.warning("Could not record multimodal audit event: %s", type(exc).__name__)


def _vault_multimodal_enabled(pool, vault_id: int) -> bool:
    if pool is None or vault_id is None:
        return False
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT multimodal_provider_enabled FROM vaults WHERE id = ?", (vault_id,)
            ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    if row is None:
        return False
    raw = row["multimodal_provider_enabled"]
    # Multimodal enrichment sends vault artifacts to an external provider, so it
    # requires an EXPLICIT per-vault opt-in. The column is SQLite INTEGER (1/0) —
    # normalize via bool() so a stored 1 is accepted and NULL/0 are fail-closed.
    return bool(raw)


def _check_pixel_cap(dims: tuple[int, int], max_pixels: int) -> None:
    """Raise ERR_ASSET_OVERSIZE if an image's pixel surface exceeds the cap."""
    if dims[0] * dims[1] > max_pixels:
        raise MultimodalProviderError(ERR_ASSET_OVERSIZE, retryable=False)


def _count_image_parts(messages: list[dict[str, Any]]) -> int:
    """Count image content parts across the message list (per-request assets)."""
    n = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    n += 1
    return n


def _read_bounded(path: Path, cap: int) -> Optional[bytes]:
    """Read up to ``cap`` bytes; return None if the file exceeds the cap."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > cap:
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read(cap)
    except OSError:
        return None


def _peek_image(data: bytes) -> Optional[tuple[tuple[int, int], str]]:
    """Return ((width, height), pil_format) by decoding only the image header (lazy).

    Pillow's ``Image.open`` reads the header without fully decoding pixel data,
    so this is cheap and does not allocate a large decompressed surface. Returns
    None if the bytes are not a decodable raster or PIL is unavailable. The PIL
    format (e.g. ``JPEG``, ``MPO``) is returned so the raster MIME can be derived
    authoritatively from the bytes rather than from an asset path that carries no
    extension.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - PIL is a hard dep but guard anyway
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size, str(img.format or "").upper()
    except Exception:  # noqa: BLE001 - truncated/corrupt/unsupported
        return None


# Map Pillow's format identifiers to accepted raster MIME types. MPO (multi-picture
# JPEG) is treated as JPEG: it is a JPEG container and is a standalone-image output
# of the #460 image path. Unmapped formats yield "" so the caller rejects them before
# any outbound call (ERR_INVALID_ASSET).
_PIL_FORMAT_TO_MIME: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "MPO": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


def _image_mime_from_peek(data: bytes) -> str:
    peek = _peek_image(data)
    if peek is None:
        return ""
    return _PIL_FORMAT_TO_MIME.get(peek[1], "")
