# Multimodal RAG 3/3 — retrieval-first query-time vision + follow-up hardening (Issues #462 / #480)

## What changed

This note covers **#462** (the query-time vision feature, shipped via PR #479)
and the **#480** follow-up hardening items landed in this change.

### #462 — retrieval-first query-time vision (PR #479)

### Backend

- **Retrieval-first vision seam** (`app/services/vision_evidence.py`): a narrow
  `VisionEvidenceService` runs strictly AFTER retrieval/rerank/distill/pack and
  BEFORE prompt construction, gated by `multimodal_query_vision_enabled`
  (default OFF). Per-source degraded `vision_status`
  (`used` / `proxy_only` / `policy_blocked` / `asset_missing` /
  `provider_unavailable` / `empty_response`) preserves rank and the stable
  `[S#]` label. Feature-off omits `vision_status` from the wire entirely.
- **Artifact identity survives retrieval** — `source_dedup_key` +
  `RAGSource.artifact_identity_key` fix the identity-collapse bug (previously
  byte-identical `(file_id, text)` proxy texts collapsed) at fusion and
  token-pack rebuild sites. The key is intentionally always-on (not flag-gated)
  so identity is preserved even with the feature off.
- **Presentation** — opaque authorized asset endpoint
  `GET /documents/artifacts/{id}/raw` (403 on missing vault read vs
  nondisclosing 404 for missing/unavailable ids; allowlist MIME;
  `private, no-store`; sha256 ETag).
- **Evaluated** — artifact-level metrics (recall/MRR/nDCG/citation-validity/
  modality-match), `RetrievalOutcome` `ok|partial|unavailable` with
  denominators excluding `unavailable`.

### #480 — follow-up hardening (this change)

### Backend

- **A1 (security)** — `to_source_metadata` now emits a **whitelist**
  (`synthesized`, `page_number`) of metadata keys instead of forwarding
  `chunk.metadata` wholesale. Server-absolute paths (`file_path`/`source_file`)
  and internal ids no longer leak to the wire; `filename` is reduced to a
  basename. Defense-in-depth: any future metadata key is dropped by default.
- **B1** — `source_dedup_key`/`artifact_identity_key` documented as an
  intentional, always-on invariant (strictly finer than the legacy key; only
  splits, never merges; downstream consumers are bounded by top_k/context
  budget). Regression test added.
- **B2** — new `empty_response` vision status distinguishes "provider answered
  200 but empty" from a true `provider_unavailable` outage. Separate counter +
  trace field so observability no longer conflates the two.
- **B3** — `_retrieve_eval_outcome` now wraps `_build_query_embeddings` in the
  outage try, so an embedding failure returns `RetrievalOutcome(status=
  "unavailable")` (matching the docstring) instead of raising and aborting the
  eval run.
- **D1** — `VisionEvidenceService.run()` amortizes ONE shared
  `MultimodalProviderClient` across the whole batch (reusing the TCP/TLS pool)
  instead of creating+closing one per artifact. The per-call `_assert_policy`
  re-check inside `chat_multimodal` is preserved, so a mid-batch kill-switch
  flip still blocks the next call.
- **D2** — blocking sqlite calls in `vision_evidence.py` are now wrapped in
  `asyncio.to_thread`, aligning with the `documents.py` convention (the pool
  uses `check_same_thread=False`).
- **D3** — the core vault-policy evaluator + permission helpers + level maps
  moved to a shared service module (`app/services/authz_policy.py`);
  `app/api/deps` re-exports them. `vision_evidence._can_read` no longer imports
  from `app.api.deps`, eliminating the services→api inversion (no authz change
  — still fail-closed).

### Ops / config / docs

- **C1** — `MULTIMODAL_QUERY_VISION_ENABLED` enrolled in `.env.example` and
  `docker-compose.yml`.
- **C2** — the full multimodal family (16 vars) enrolled in the
  `scripts/check_config_contract.py` CI gate (bool/int/float/str/list readers),
  so drift between `config.py`, `.env.example`, and `docker-compose.yml` is
  CI-detected.
- **C3** — this release note + a new "Query-time vision (#462)" section in
  `docs/multimodal-enrichment.md`.

### Frontend

- The `Source.vision_status` type union widens to include `"empty_response"`
  (B2).

### Test coverage

- **E1** — real-impl branch tests for `_read_bounded` and `_can_read`.
- **E2** — a real cross-vault disallowed-read IDOR test for
  `GET /documents/artifacts/{id}/raw`.

## Security posture

Query-time vision is default-off and reuses the #461 authorization gates
(global switch + per-vault opt-in + exact-origin allowlist + SSRF), re-checked
per call. No paths, bytes, base64, or raw observations appear on any wire
property (A1 closes the metadata leak across all source-emitting surfaces —
chat/stream/history/agentic/eval AND the /search + chunk-context endpoints,
all routed through one shared `whitelist_metadata_for_wire` whitelist). Authz
is enforced before any byte open; the stream path's fallback evaluator uses a
fresh short-lived connection (S-003 no-double-connection invariant preserved).
