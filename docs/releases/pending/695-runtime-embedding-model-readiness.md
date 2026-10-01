# Workstream B PR 6: runtime embedding-model change flips readiness (Issue #695)

## What changed

### Backend

- **`backend/app/api/routes/settings.py`** — saving an embedding-identity
  field through `PUT /api/settings` or `POST /api/settings` now immediately
  marks the live vector store not-ready, so chat/memories/search/wiki gate
  with the existing 503 "Embedding model mismatch — admin reindex required"
  instead of silently comparing new-model query vectors against document
  vectors embedded by the old model (issue #695, ENH-08 stage 1):
  - `_effective_embedding_identity()` snapshots
    `(embedding_model, doc_prefix, query_prefix)` with the same falsy-to-`""`
    normalization `VectorStore._compute_embedding_prefix_hash` applies, so
    `None`↔`""` writes and redundant saves of the same values are not
    identity changes (a no-op save never takes chat down).
  - Both handlers snapshot the identity before applying the update and call
    `_invalidate_vector_store_readiness(request.app)` right after the apply
    — before `_hot_rebind_llm_clients`, which is not exception-tolerant and
    runs after the settings row has already been persisted.
  - The hook performs the same transition `VectorStore.mark_ready(False)`
    performs, synchronously, and logs a warning naming the admin-reindex
    recovery. How readiness can return:
    - An all-vault reindex job that BEGINS after the change and completes
      (the pre-existing `record_embedding_metadata` + `mark_ready(True)`
      completion path). A vault-scoped reindex also flips readiness back but
      leaves a mixed index (every other vault still embedded under the old
      model) — recovery requires a full reindex.
    - Restart, ONLY when the configured identity is reverted to the stored
      sidecar value: after a forward change (A→B) with no reindex, startup
      replays the persisted B into the singleton and `validate_schema`
      re-detects the stored-sidecar-A vs configured-B mismatch, so the store
      stays gated — restarting does not clear the 503.
    - Recovery is API-only today: `POST /api/documents/reindex` has no
      frontend caller, and the SettingsPage banner's suggested remediations
      (per-file reprocess, wiki recompile) never restore readiness.
- **`backend/tests/test_b06_runtime_model_change_readiness.py`** — frozen
  acceptance checks: PUT/POST `embedding_model` flips, prefix sibling flips,
  and the no-op guard.
- **`backend/tests/test_b06_settings_identity_readiness_hook.py`** — hook
  coverage beyond the frozen module: the flip survives a failing
  `_hot_rebind_llm_clients` (ordering pin), POST + `embedding_doc_prefix`,
  `embedding_query_prefix`-only change, and missing-store tolerance.

### Known out-of-scope races (owned by the zero-downtime switchover work)

- A reindex already in flight when the identity save lands completes by
  recording the live identity and unconditionally calling `mark_ready(True)`,
  re-opening the mismatch window; because the completion records the live
  identity, a later restart no longer detects the mismatch either — the
  mixed index stays queryable until a full re-embed. Owned by #696
  ([Workstream B] PR 7) and #736 (Workstream G PR 4), which this PR
  deliberately does not implement (no dual-embedder serving).
- Upload/ingest paths are not `require_model_ready`-gated; documents ingested
  during the not-ready window still embed under the new model into the old
  index (pre-existing behavior, outside this issue's query-vector invariant).
