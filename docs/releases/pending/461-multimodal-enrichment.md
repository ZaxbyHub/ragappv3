# Multimodal RAG 2/3 — secure context-aware artifact enrichment + proxy indexing (Issue #461)

## What changed

### Backend

- **Generic provider policy core** (`app/services/model_provider_policy.py`):
  exact-origin allowlisting with lexical normalized-origin comparison **before**
  any DNS resolution, sensitive/ordinary tiers, and SSRF via
  `app/services/ssrf.py::assert_url_safe` as an **independent second gate** for
  allowlisted origins only. `draft_provider_policy.py` is now a thin wrapper
  delegating to this core, preserving Draft Room behavior and exception-message
  strings (existing Draft suite passes unchanged).
- **Global + per-vault authorization contract**: 16 new `multimodal_*` settings
  (default off/empty/zero), `SettingsUpdate`/`SettingsResponse`/`ALLOWED_FIELDS`
  wiring, redaction of URL/model for non-admins, PUT-time URL validation, hot
  rebind of the running multimodal client, and an idempotent migration for
  `vaults.multimodal_provider_enabled` (tri-state: NULL=inherit global,
  false=hard-off). New admin-gated
  `PUT /vaults/{id}/multimodal-provider-toggle`.
- **Bounded client/service** (`multimodal_enrichment.py` +
  `multimodal_prompts.py`): a dedicated `httpx.AsyncClient` with
  `follow_redirects=False` + SSRF-safe transport; policy re-checked immediately
  before every provider call; strict byte/pixel/asset-count caps; response
  parsed into a versioned derived record. Injection-hardened prompts wrap each
  context field in `<document>…</document>` boundaries and send image bytes as a
  separate content part with no user-controlled text.
- **Atom-scoped state machine** (`enrichment_state.py`) on
  `ingestion_stage_states` (`stage='enrich'`) with input fingerprints, stale-
  completion rejection, bounded retry (transient vs permanent), and startup
  recovery of stranded `running` rows.
- **Derived records + deterministic proxy indexing** (`document_atom_enrichments`
  table, metadata-only): proxies written through the existing LanceDB path with
  **add-then-delete** so a failed re-embed leaves base/raw proxies intact.
  Raw atoms/assets never overwritten; no physical LanceDB schema change.
- **Audit**: every attempted outbound transmission (including denials) logged as
  `attempted_external_transmission` with no content or credentials.

### Frontend

- Settings → Models **Multimodal artifact enrichment** card (global enable,
  provider URL/model, allowlisted origins, mode, concurrency, byte/pixel caps)
  with an **External data egress** warning.
- **Dedicated vault multimodal opt-in card** with an **egress warning** and a
  tri-state (Inherit global / On / Off), admin-gated.
- New component tests for the multimodal fields, allowlist parsing, and the
  vault opt-in tri-state.

### Docs & config surfaces

- `docs/multimodal-enrichment.md` — enablement, authorization gates, egress/
  privacy, limits, retry, re-enrichment, audit, disablement/rollback.
- `.env.example` + `docker-compose.yml` gain the `MULTIMODAL_*` keys.

## Security posture

Default-off; **zero outbound calls** unless global enablement AND per-vault
opt-in AND exact-origin allowlist membership AND SSRF safety all hold at call
time. No query-time VLM; raw evidence never rewritten; base/raw proxy survives
provider outage or policy revocation.
