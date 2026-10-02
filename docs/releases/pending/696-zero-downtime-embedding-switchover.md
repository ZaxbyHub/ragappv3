# Workstream B PR 7: zero-downtime embedding switchover (Issue #696)

## What changed

### Backend

- **`backend/app/services/background_tasks.py`** — the reindex job
  (`_reindex_embed_all`) now routes a same-dimension embedding-MODEL change
  through the staged rebuild table instead of rewriting the live table in
  place. The staging decision is `dimension changed OR embedding identity
  changed`, where the identity comparison is shape-probed from the
  settings_kv sidecar (a store without `get_embedding_metadata`, or a
  MagicMock/stub return, takes the legacy dimension-only branch — existing
  harnesses stay green). Consequences: every re-embed write carries the
  staged handle (`vector_target`), a per-file failure aborts the staged
  rebuild (the previous generation stays fully intact), and the commit
  swaps only after all files succeed — the cross-dimension guarantees of
  #513 W13 generalized to the common model-swap case.
  - **R1 guard** (tracked in issue #696 comment 5946440228): the embedding
    identity is snapshotted at job start, re-checked immediately BEFORE
    commit (a changed identity aborts — a mixed-generation table is never
    committed) and at completion on both paths (the metadata write and the
    readiness lift are skipped so the mismatch stays detectable on
    restart).
  - **R2 guard**: a vault-scoped reindex now refuses when the identity (or
    dimension) changed, with the same actionable "run a full reindex"
    guidance as the #691 dimension guard — it can no longer lift the 503
    gate over a mixed index.
  - **Commit-failure artifact preservation**: a failure raised BY
    `commit_dimension_rebuild` no longer aborts — the temp table is the
    recovery artifact the commit's CRITICAL log names. Recovery procedure:
    manually promote `chunks_dim_rebuild` to `chunks` BEFORE re-running the
    reindex; the next `begin_dimension_rebuild` drops the preserved temp
    table as stale.
  - When the identity changed but the embedding service is unavailable
    (no dimension probe), the job fails with actionable guidance instead of
    staging (`begin_dimension_rebuild(None)`) or rewriting in place.
- **`backend/app/services/vector_store.py`** — `VectorStore` gains a
  `rebuild_in_progress` flag (set when a staged rebuild opens, cleared at
  commit/abort ENTRY) and the draining-config sidecar
  (`register_draining_embedding_config` / `clear_draining_embedding_config`
  / `serving_embedding_identity`): the full config (model, endpoint URL,
  RESOLVED prefixes) the live generation was built under, persisted
  write-if-absent at the identity-changing settings save and cleared only
  at cutover. A second `begin_dimension_rebuild` while one is open is
  refused. `serving_embedding_identity()` cross-checks the snapshot's model
  against the sidecar identity and refuses to pin on mismatch (a stale
  snapshot degrades to the surfaced unpinned path, never a wrong pin).
- **`backend/app/api/deps.py`** — `require_model_ready` admits queries
  while `_ready` is False AND a staged rebuild is open: the live table is
  still the previous, internally consistent generation (issue #696). The
  flag probe is a strict singleton-bool check (`is True`) so MagicMock
  auto-attributes keep the existing 503 suites green.
- **`backend/app/services/embeddings.py`** — a service-level
  serving-identity funnel: `set_serving_identity_provider(...)`, consulted
  by `embed_single`/`embed_passage` ONLY (`embed_batch` builds the NEW
  generation; `embed_probe` probes the CURRENT endpoint). While a rebuild
  is open, query embeddings run under the OLD generation's captured
  identity (model + URL + resolved prefixes) — this covers every retrieval
  seam at once (chat variants, decomposition sub-queries, search, memories
  writes/queries, agentic retrieval, image search). New
  `EmbeddingIdentity` dataclass and `resolve_effective_prefixes()` (single
  implementation of the Qwen auto-prefix rule, now shared by the live
  properties and the snapshot).
- **`backend/app/api/routes/settings.py`** — the #695 readiness hook now
  also captures the PRIOR embedding config (both `PUT` and `POST`
  handlers) and persists it as the draining snapshot, best-effort and
  strictly non-raising, before `_hot_rebind_llm_clients`.
- **`backend/app/api/routes/health.py`** — healthz parity: while a rebuild
  is open, not-ready becomes a WARNING ("serving previous embedding
  generation during staged rebuild"), never a 503 issue; a second warning
  fires when no draining snapshot exists.
- **`backend/app/lifespan.py`** — wires the embedding service's
  serving-identity provider to the vector store.

## Operator notes and caveats

- Zero-downtime switchovers are fully served when the model change is made
  through the settings API (the draining snapshot pins query embedding to
  the old generation until cutover). When the identity changed via env file
  + restart, the gate still admits during the rebuild but the old endpoint
  is not recorded anywhere app-side, so query embeddings run under the NEW
  model against the OLD index (degraded dense rankings; hybrid FTS intact)
  until cutover — surfaced as the healthz warning above and a loud
  begin-time log.
- Cutover has a brief deny-window: admission clears when the commit starts,
  so the table swap itself is momentarily 503 (never a mixed generation).
- Multi-worker/lease-mode deployments: `rebuild_in_progress` and the
  readiness flip are per-process (`uvicorn --workers`); the same scope
  caveat as #695 applies.
- Known interaction owned by #736 (uploads during not-ready windows): a
  bare ingest during a same-dimension rebuild writes new-model vectors into
  the live table and the commit swap drops them; #736 AC2 owns gating
  uploads.
- An A→B→A identity flip-back WITHIN one reindex job run defeats the R1
  start-vs-end comparison (rows embedded under B in between can commit);
  requires two admin saves inside one job's duration — disclosed blind spot,
  per-row identity binding would be the full fix.
- The sidecar identity does not record the embedding endpoint URL, so a
  same-model swap to a different endpoint is not detected as an identity
  change (pre-existing, shared with restart-time validation; follow-up
  candidate: extend the identity tuple with the URL).
- The memories vector corpus is only migrated by the offline memories
  migration; during a rebuild the funnel pins memory query/write EMBEDDINGS
  to the serving generation, but the corpus itself is version-skewed until
  that migration runs (unchanged behavior, now documented).

## AC5 operator verification procedure (non-CI)

On a deployment with two live embedding endpoints (old and new model):
trigger a same-dimension model change through the settings API, then start
the admin reindex while driving concurrent `POST /chat` queries. Confirm
every response is 200, retrieved sources come from a single consistent
generation, the old-generation query embedder is used until commit (check
the old endpoint's request logs), a mid-rebuild failure keeps the prior
generation fully queryable, and GPU memory headroom holds with both
embedder servers loaded.

## Tests

- Frozen acceptance checks (issue #696):
  `backend/tests/test_b07_same_dim_staged_rebuild.py` (C1-C4, RED at base
  51cf9409 → GREEN on this PR) plus the three preserving suites (C6-C8).
- Supplementary: `backend/tests/test_b07_serving_generation.py` — gate and
  healthz admission semantics (strict-bool probe), serving funnel
  (single/passage pinned, batch/probe unpinned), drain-snapshot lifecycle,
  R1/R2 guards, commit-failure artifact preservation, probe/None guards,
  and the capability-probe legacy branches that keep existing harnesses
  green.
