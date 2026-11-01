# Workstream B PR 9: settings-to-ingestion contract and the embedding cache stop drifting from what actually serves (Issue #698)

## What changed

### Backend

- **`backend/app/config.py`** — new `EMBEDDING_MAX_TEXT_CHARS = 8192` constant
  (mirrored by `EmbeddingService.MAX_TEXT_LENGTH`, equality pinned by a
  guardrail test — config cannot import the service). `chunk_size_chars`
  gains a `mode="after"` validator rejecting values `<= 0` or above the cap
  (the legacy `chunk_size x4` migrator runs first, so both direct and
  legacy-derived values are checked at construction). `multi_scale_chunk_sizes`
  rejects entries above the cap — under the shipped default
  (multi-scale on), an oversized scale would otherwise silently drop every
  chunk it produces at ingest. A cross-field model validator rejects
  `chunk_overlap_chars >= chunk_size_chars`, and
  `multi_scale_overlap_ratio` is now strictly `< 1.0` (implementation-review
  findings: both configurations previously passed every validation check and
  then failed EVERY ingest in the chunker with "'overlap' argument must be
  less than max_characters").
- **`backend/app/api/routes/settings.py`** — the PUT path enforces the same
  both-bounds rule at two seams: the `SettingsUpdate` field validator, and a
  post-`apply_legacy_settings_conversion` re-check inside
  `_validate_settings_update` (a legacy `{"chunk_size": 3000}` PUT derives
  `chunk_size_chars=12000` AFTER the field validators run — it now 422s
  instead of poisoning the settings singleton mid-ingest).
- **`backend/app/services/embeddings.py`** — `embed_batch` classifies input
  texts (None / whitespace / oversized) before batching: `fail_fast=True`
  (default) still raises `EmbeddingError` on the first invalid text with the
  unchanged message shapes; `fail_fast=False` now degrades per-text — the
  invalid text never reaches the provider, gets a `None` placeholder at its
  ORIGINAL position, and is named in one WARNING. Valid texts keep their
  order and char-bounded batch semantics; `len(embeddings) == len(texts)`
  always holds. A single oversized chunk therefore no longer fails the whole
  document (the ingest paths' [W10] per-text skip machinery drops exactly
  that chunk). The stale `MAX_TEXT_LENGTH` comment ("derived from
  chunk_size_chars=8192" — the real default is 2000) is corrected.
- **`backend/app/services/contextual_chunking.py`** —
  - The context-prefix budget charges the EFFECTIVE doc prefix (including
    the auto-applied 82-char Qwen prefix) via #696's
    `resolve_effective_prefixes` — the same single source the embedder uses —
    so a qwen-model deployment with an unset prefix can no longer produce a
    chunk that parses fine but fails at embed time.
  - The fixed `_MAX_DOCUMENT_LENGTH=100_000`/`_TRUNCATE_CHARS=50_000`
    constants are gone. The whole per-chunk prompt — chunk view, document
    view, filename view — is bounded by `4 * model_context_tokens`
    characters, measured on the post-escape views (escape inflation cannot
    break the bound); the chunk view gets at most half the remaining budget,
    the document view gets the rest with the `[...truncated...]` marker
    inside the budget; a sub-~236-token context degrades to the fixed
    scaffolding floor. Views are prompt-only: canonical chunk text is never
    truncated.
  - `contextualize_chunks` materializes real task handles and, on any
    failure, cancels still-pending siblings before re-raising — a surviving
    sibling can no longer keep mutating `chunk.text` after the ingest caller
    has caught the raise (embedded and stored text can no longer diverge).
    Outer `CancelledError` passes through unchanged. The document is
    XML-escaped once per call (not once per chunk) and prompt-budget
    truncation logs one WARNING per document with repeats at DEBUG.
- **`backend/app/services/document_processor.py`** — the persistent
  embedding-cache identity includes the serving endpoint
  (`model_revision` now carries `settings.ollama_embedding_url`), so
  re-pointing the embedding URL under the same model id no longer reuses
  stale cached vectors. `embedding_cache_store` runs via `asyncio.to_thread`
  (the file's established off-load idiom) — the store's SQLite write plus
  entry-cap prune no longer blocks the event loop (measured 400-600 ms at
  the default 50k cap). Both `_embed_with_cache` failure raises now name the
  failed TEXT positions. The pre-ingest oversized-chunk warning states the
  post-fix truth (skipped under `fail_fast=False`, raises under `True`).
- **`backend/app/services/embedding_cache.py`** — connection init creates
  `idx_embedding_cache_created_at`, so the prune's `ORDER BY created_at`
  stops full-scanning the table at the entry cap.

- **`backend/app/lifespan.py`** — persisted-settings replay validates the
  merged candidate state once (batch) before applying, falling back to the
  historical per-key warn-and-skip only when the set as a whole is
  rejected. Per-key replay order could silently revert one field of a
  jointly-valid persisted pair on restart (e.g. a PUT-legal
  `chunk_size_chars=150` + `chunk_overlap_chars=100` pair replayed over
  boot defaults `{2000, 200}` landed as `{2000, 100}`); the same fix
  covers the pre-existing jobs-lease heartbeat/reclaim instance of that
  class (issue #559).

### Operations

- **`start-services.ps1`** — the TEI embedding container is started with
  `--max-client-batch-size 128` (matching `docker-compose.yml`): TEI's own
  default cap of 32 silently rejected every app batch above 32 texts on the
  documented Windows launch path while the app default is 64.

### Frontend

- **`frontend/src/components/settings/DocumentProcessingSettings.tsx`** — the
  Chunk Size / Chunk Overlap help text now states that those fields apply
  only when multi-scale indexing is disabled (multi-scale is the shipped
  default and uses `MULTI_SCALE_CHUNK_SIZES`), instead of implying they
  govern the next ingest. Same disclosure added to `docs/admin-guide.md`.

### Tests

- Frozen acceptance suite `backend/tests/test_b09_settings_ingest_contract.py`
  (10 checks, authored at arm's length, RED at base / GREEN at fix).
- `test_b09_embed_batch_splice_contract.py` — the fail_fast=False return
  contract (positions, count, failed content, provider-failure shape) and
  the AC5 PUT-path bounds (direct + legacy-derived 422s).
- `test_b09_contextual_budget_clamps.py` — end-to-end prompt-budget clamps
  (escape inflation both sides, two different-length chunks, fixed floor,
  canonical-text preservation).
- `test_b09_contextual_cancellation.py` — re-raise preservation, outer
  CancelledError pass-through, all-success path.
- `test_contextual_chunking.py` — the truncation tests are rewritten for the
  settings-derived budget math (`TestBoundedDocumentView`,
  `TestBoundedPromptBudget`).

## Upgrade notes

- Configurations with `CHUNK_SIZE_CHARS > 8192` (or legacy `CHUNK_SIZE >=
  2049`) now fail at startup with a named validation error instead of
  failing per-chunk mid-ingest. The same bound applies to each
  `MULTI_SCALE_CHUNK_SIZES` entry. `CHUNK_OVERLAP_CHARS >= CHUNK_SIZE_CHARS`
  (direct or legacy-derived) and `MULTI_SCALE_OVERLAP_RATIO = 1.0` are
  likewise rejected at configuration time — both previously failed every
  ingest in the chunker. The PUT `/settings` API enforces the same rules
  and answers 422 — including the cross-field overlap rule for a
  single-sided size update that would land at or below the currently
  persisted overlap (such PUTs were accepted before this change). One
  precision on "at configuration time": the embedder's EFFECTIVE per-text
  budget also subtracts the document prefix (82 chars for the auto-applied
  Qwen prefix), so a `chunk_size_chars` within the prefix distance of the
  8192 cap can still produce chunks that are skipped per-chunk at embed
  time rather than rejected at startup. These startup failures apply to
  environment variables and fresh construction; a value already persisted
  in `settings_kv` that fails validation at startup is logged as a warning
  and replaced by the default (the existing lifespan replay behavior) —
  and jointly-valid persisted pairs now replay as a unit (batch-validated)
  instead of being split by per-key replay order.
- The Windows launcher applies the new `--max-client-batch-size 128` only
  when it CREATES the TEI container: if `harrier-embed` is already running
  from before the upgrade, remove it first (`docker rm -f harrier-embed`)
  and re-run `start-services.ps1`, or it keeps TEI's default 32-text client
  batch cap (docker-compose deployments are unaffected).
- The embedding-cache identity change intentionally invalidates every
  pre-existing cache row (one-time cold cache on first deploy; the table is
  a rebuildable cache, not user data).
- Bandit baseline re-anchored for pure line drift (131 findings, signature
  multiset unchanged).
