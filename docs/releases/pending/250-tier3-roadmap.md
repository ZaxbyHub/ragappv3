# Tier 3 Roadmap (Issue #250)

## What changed

### Auth Hardening (Phase 1)
- **Argon2id password migration** (FR-013): bcrypt → Argon2id with transparent verifier upgrade on next login (no forced reset). OWASP-recommended parameters.
- **Revocable access tokens** (FR-011): unique `jti` claim + denylist; tokens revocable before their 15-min TTL; logout denies the current access token.
- **Client fingerprint binding** (FR-011): tokens bound to User-Agent hash; replay from a different client rejected (fail-closed).
- **Service-account API keys** (FR-014): scoped, rotatable keys with sha256 hash storage; admin-gated issue/rotate/revoke endpoints.
- **Organization invite flow** (FR-012): token-based invites (create/resend/revoke/list/accept); identity-match enforcement prevents token-theft acceptance.
- **TEST_SCHEMA centralization**: 17 duplicated test schemas consolidated into one shared `schema_constants.py` module.

### Retrieval Foundation (Phase 2)
- **Sentence-level provenance** (FR-003): per-sentence source document + character span tracking in distilled context.
- **Shared Redis embedding cache** (FR-005): L1 LRU → L2 Redis → provider; cluster-wide reuse; graceful fallback.
- **Citation confidence + unverifiable claims** (FR-004): per-citation Jaccard lexical-overlap confidence score; uncited low-overlap claims surfaced.
- **Live-pipeline eval adapter** (FR-001): offline MRR/nDCG/recall harness consumes actual live retrieval results; runs persisted with timestamp + release ID.

### Query Intelligence (Phase 3)
- **LLM query planner** (FR-002): decomposes complex questions into distinct sub-queries; simple-query guard avoids LLM call.
- **RRF orchestration** (FR-002): per-sub-query independent retrieval + reciprocal-rank fusion (k=60) before distillation.
- **Per-vault + per-file enrichment toggle** (FR-006): file > vault > global resolution; API-toggleable.
- **Prompt versioning + per-org overrides + A/B variants** (FR-007): version registry, transactional exactly-one-active, org-scoped overrides, deterministic per-user A/B assignment with exposure tracking.

### Chat & UX (Phase 4)
- **Per-pane error boundaries** (FR-017): each chat pane isolated; crash in one doesn't take down others; state-reset retry.
- **KaTeX + Mermaid rendering** (FR-016): inline/block LaTeX + Mermaid diagrams in chat; WCAG-compliant (MathML preserved, role="img").
- **Inline composer controls** (FR-018): temperature, retrieval-mode, citation-mode selects; per-session persistence; temperature wired to LLM.
- **Reconnecting banner** (FR-019): prominent banner on connection loss; severity-colored; accessible; prop-based (no double-poll).
- **SSE staged progress** (FR-015): Searching/Reading/Drafting stage events + frontend indicator.
- **Citation inspection UI** (FR-003/FR-004 frontend): confidence dots on citation chips; source-span popover; unverifiable-claims warning.

### Advanced (Phase 5)
- **Feedback-driven re-ranking** (FR-010): bounded (±0.10) ranking signal from user feedback; score_type-guarded (prevents distance-score inversion).
- **Agentic RAG** (FR-008): tool registry + iterative planner; feature-flagged (`agentic_rag_enabled`, default OFF); real LLM synthesis.
- **Image ingestion** (FR-009): OCR via pytesseract + PIL; searchable text representation; wired into document_processor dispatch.

## Why
Implements all 19 functional requirements from issue #250 (Tier-3 roadmap split from #242).

## Migration steps
- Install new dependencies: `pip install -r backend/requirements.txt` (adds argon2-cffi, Pillow, pytesseract).
- The database migrations run automatically on startup (idempotent `CREATE TABLE IF NOT EXISTS` for all new tables).
- `agentic_rag_enabled` defaults to `False` — opt-in for agentic multi-step retrieval.
- `chunk_enrichment_enabled` still defaults to `False` — per-vault/per-file overrides are additive.
- Argon2id migration is transparent: existing bcrypt hashes verify and are upgraded on next login.

## Breaking changes
None — all changes are additive (new tables, new endpoints, new fields with defaults). The standard RAG pipeline behavior is unchanged when feature flags are off.

## Known caveats
- Frontend typecheck/lint/tests pass locally; 7 pre-existing test failures in `Composer.draft.test.tsx` and `Composer.indexing.test.tsx` (from PRs #58/#81, unrelated to this PR).
- Agentic RAG path emits a simplified done payload (omits some standard fields); acceptable for opt-in feature.
- `retrieval_mode` and `citation_mode` are accepted by the backend as v1 forward-compatible (logged, not yet implemented in retrieval behavior).
- Image OCR requires Pillow + pytesseract (graceful degradation without them).
