# Vector-store schema migration safety (#706)

Workstream C PR 1 of 14 (audit 20260923T174456Z; findings P03-SK2-03, P03-SK2-04,
T1-25-S2-06, RT-S7-02). Four boot-path defects in the LanceDB schema-migration
machinery: a deadline that could cancel a migration mid-swap, a reconcile rule that
could silently discard a corpus generation, no-op probes that loaded the whole table
on every boot, and a concurrent-rebuild race — plus the undocumented v1.1.0 upgrade
fail-closed.

## Upgrade note: from v1.1.0 or any pre-#353 store

Stores created by v1.1.0 or any version before the embedding-identity sidecar
(#353, resolving #220) intentionally **fail closed** on first boot after upgrading:
chat and search return **503 "admin reindex required"** until a full reindex runs.
Clear it with `POST /api/documents/reindex` (admin-authenticated; re-embeds every
vault and records the embedding identity). This has been the behavior since #353 —
it is now documented in README's Upgrading section as well.

## What changed

### Backend

- **`backend/app/lifespan.py`** — the four vector-store `migrate_add_*` calls now
  run through `_await_migration`, which has **no cancel-on-timeout deadline**. A
  10s `asyncio.wait_for` could cancel a 20k-50k-row staging swap mid-drop;
  `CancelledError` is a `BaseException` and bypassed the swap's except-Exception
  critical handling. Fail-soft is preserved: genuine migration failures are still
  caught, logged, and boot continues.
- **`backend/app/services/vector_store.py`**
  - `_reconcile_chunks_rebuild_state` no longer resolves a leftover
    `chunks_rebuild` staging table by row-count majority. Rebuild-from-staging now
    requires `ids(canonical) ⊆ ids(staging)` with matching `text` on the overlap;
    drop-staging requires the reverse subset. Any divergence — canonical-only ids,
    staging-only ids, or a content mismatch on an overlapping id — logs CRITICAL,
    preserves BOTH tables untouched for manual recovery, and raises (the boot-time
    caller warn-and-continues). A majority rule must never silently choose which
    generation survives.
  - `migrate_add_vault_id` / `migrate_add_chunk_scale` / `migrate_add_parent_window`
    answer their no-op null checks with `count_rows(filter="<col> IS NULL")`
    instead of materializing the whole table (embeddings included) into pandas —
    ~2.4s and ~1GB RSS per check on a 200k-row store, times three, gone; above
    500k rows the old probe raised outright. A `TypeError` fallback keeps test
    doubles / alternative backends on the previous pandas semantics. The
    genuinely-needed backfill and add-columns paths are unchanged.
  - `begin_dimension_rebuild` now serializes admission: a dedicated
    `_rebuild_admission` condition + slot (separate from the #696
    `rebuild_in_progress` serving gate, whose lifecycle is unchanged). Same-task
    re-entry keeps the #696 loud raise; **cross-task contention waits** for the
    open rebuild's terminal state and then re-checks convergence — returning
    `None` when the live index already holds the target generation so the caller
    proceeds bare instead of failing (previously two concurrent reindex jobs
    raced: the second begin dropped the first's staged table as "stale" and the
    orphaned job's file landed in `error`). Convergence is re-checked only after
    actually blocking, so uncontended begins (including same-dimension
    identity rebuilds) behave exactly as before.
  - `commit_dimension_rebuild` persists the job-start embedding identity to the
    settings_kv sidecar as part of the swap (when the caller supplied
    `staged_metadata`), so a concurrent reindex job's convergence check sees the
    committed table's true identity immediately — the commit-to-completion
    metadata window cannot trick it into opening a partial rebuild whose commit
    would wipe the first job's rows. A sidecar write failure after the durable
    swap logs CRITICAL (the waiter window reopens loudly) and the commit still
    succeeds; the R1-guarded completion write remains the second chance.
  - `record_embedding_metadata` accepts optional `model_id` / `prefix_hash` to
    persist a caller-supplied identity instead of live settings (defaults
    unchanged for all existing callers).
- **`backend/app/services/background_tasks.py`** — `_reindex_embed_all` captures a
  job-start embedding-identity snapshot (settings-only + guarded shape-probe),
  passes a `converged` closure (dimension AND identity) plus `staged_metadata` to
  `begin_dimension_rebuild`, handles the converged `None` return by re-embedding
  bare without a staged rebuild, and records the job-start identity at the
  completion-time metadata write (closing the guard→write TOCTOU; identical
  values whenever the R1 in-flight guard passes).

### Tests

- `backend/tests/test_c01_migration_deadline.py` — AST census: no `migrate_add_*`
  call may sit under a cancel-on-timeout wrapper in `lifespan`.
- `backend/tests/test_c01_reconcile_divergent.py` — divergent canonical/staging
  generations must not be silently resolved.
- `backend/tests/test_c01_noop_migrations.py` — no-op migrations must not load the
  whole table into pandas.
- `backend/tests/test_c01_concurrent_dim_rebuild.py` — two concurrent
  dimension-changing reindex flows both finish; neither file lands in `error`.
- `backend/tests/test_c01_upgrade_docs.py` — README Upgrading names the reindex
  remedy.
- `backend/tests/test_c01b_migration_safety_extras.py` — new-contract behavior
  tests: waiter convergence both outcomes, uncontended same-dim begin contract,
  commit-time identity persistence (including its failure path), and the
  same-id-content-conflict / partial-overlap / reverse-branch reconcile
  divergence cases.

## Known limitations

- Rebuild admission serialization is single-process (same scope as the #696
  serving gate); multi-worker/lease-mode overlap across processes remains a
  documented limitation.
- Migrations now run unbounded by design: a boot that genuinely must backfill a
  large corpus takes as long as the swap needs (previously it was cancelled at
  10s and could silently corrupt state). Already-migrated boots are strictly
  faster (three full-table loads removed).
- A divergent reconcile (cancelled swap + post-boot ingestion) now fails loudly
  on every boot, with both tables preserved, until an operator resolves the
  divergence manually — by design; silently discarding one generation is the
  bug this removes.
