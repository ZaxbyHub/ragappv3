# Recoverable ingestion, enrichment and reindex (Issue #513)

## What changed

### User-visible behavior

- **Truthful partial-ingest status.** An ingest that completes with failed
  chunks no longer reports plain success. The upload/reindex path
  (`process_existing_file`) writes status `partial` when `chunks_failed > 0`;
  the scan/sync path (`process_file`) keeps status `indexed` and sets the new
  `files.partial_embeddings = 1` marker. Either way, "indexed but incomplete"
  is distinguishable from full success and remains retryable. The
  `files.status` CHECK is widened to allow `partial`. Success transitions
  also clear the attempt-scoped `error_message`.
- **Reindex interruption is an explicit, recoverable state.** A restart (or
  shutdown) no longer leaves `document_reindex_jobs` rows stranded as
  `running`: they are marked `interrupted` — a terminal, operator-visible
  status with job identity and progress preserved — and pending jobs are
  re-enqueued. Same-dimension reindexes behave as before.
- **Dimension-migrating reindex without a search outage.** When a reindex
  targets a different embedding dimension than the live index, the rebuild
  writes into a temp table (`chunks_dim_rebuild`) while the old-dimension
  table keeps serving searches; only after every file succeeds is the
  validated swap performed. Any failure aborts the rebuild (temp table
  dropped) and leaves the old index fully intact.
- **KMS auto-compile for async uploads.** The converged ingest finalization
  path (shared by the sync and async/recovered entry points) enqueues the
  KMS compile under the existing `kms_enabled` / `kms_compile_on_ingest`
  gates, so recovered/retried uploads no longer skip knowledge-store
  compilation.
- **Periodic orphan rescan with a live-lease.** Stranded upload rows are no
  longer only recovered at startup: a periodic rescan (default hourly)
  re-enqueues `pending`/`queued` rows and `processing` rows older than the
  30-minute stranded timeout, and rows currently held by a live worker (the
  active-file lease) are never stolen. At startup, rows that already reached
  a post-parse phase are recovered unconditionally — at startup they are
  orphans by definition — while rows still inside a parse phase stay
  age-gated so a restart alone never interrupts a long legitimate parse.
- **Retry scheduler outside worker slots.** Retryable ingestion/enrichment
  failures are parked with a dedicated deferred-retry scheduler (bounded
  backlog) instead of sleeping inside a worker slot, so workers keep
  consuming during backoff and a retry storm can no longer self-deadlock a
  bounded queue's only consumer.
- **Near-duplicate advisory grouping.** After a successful ingest, the
  file's chunk-embedding centroid is compared (bounded, vault-scoped) against
  recent centroids and similar files share a `group_id`, exposed as a
  nullable `near_duplicate_group` field on document detail/list responses.
  The grouping is advisory only: it never blocks, deletes, rejects, or
  otherwise affects retrieval; re-ingest replaces the file's centroid row.
- **Enrichment override readback.** The per-file/vault enrichment override is
  now included in the document detail and list SELECTs, so the toggle round-
  trips through the API instead of silently reading as null.
- **Token-cost-bounded embedding batches.** `embed_batch` closes a batch
  before a text would exceed the new `embedding_batch_max_chars` budget
  (default 131072) in addition to the count limit; order and
  one-embedding-per-text are preserved.
- **Persistent embedding reuse cache.** Enrichment/retry embeddings are
  looked up in a disk-backed cache (`<data_dir>/embedding_cache.db`) keyed by
  the immutable embedding contract — model id, embedder revision
  (implementation class + provider mode), doc prefix, dimension, and
  normalized text. Byte-identical text under the same contract is never
  re-embedded (survives restarts); any contract change produces different
  keys (invalidation by construction). Capacity-bounded LRU (default 50,000
  entries).
- **Spreadsheet NA preservation.** CSV/Excel parsing passes
  `keep_default_na=False`, so literal cell text like "NA", "N/A", "NULL" is
  kept verbatim instead of being coerced to missing/empty.
- **Schema parser quoted identifiers.** `CREATE TABLE` parsing accepts quoted
  table/schema names (`"order items"`, `` `backticked` ``, `'single'`), so
  schema definitions with spaces/quotes round-trip and multi-statement files
  parse per-block.
- **Chunk boundary coverage.** The embedding chunker never discards a
  below-minimum fragment: boundary fragments merge into the neighbor when
  they fit, otherwise they are emitted as their own chunk (coverage wins
  over size aesthetics).
- **Contextual prefix budget.** The prepended context in contextual chunking
  is capped so prefix + chunk stays within the embedding text limit
  (`MAX_TEXT_LENGTH` minus the doc prefix); the canonical text is never
  truncated and the actually-prepended context is recorded in metadata.
- **Single content hash per upload.** The upload route computes the file
  hash once and threads it through the work item to the processor, instead
  of hashing the same bytes twice per ingest.
- **Upload compensation on enqueue failure.** If enqueueing fails (e.g.
  maintenance mode) and the request created the `files` row, the row is
  deleted transactionally and the uploaded bytes are unlinked — no orphan
  row that 409-blocks re-uploads. Pre-existing rows are left untouched.
- **Coherent bulk delete.** Vault-wide document deletion collects ids first
  and deletes exactly those rows (`WHERE id IN (...)`), so documents
  uploaded during the collection window keep row + file coherently.
- **Transactional vault deletion.** Vault delete commits the relational
  cascade first (`BEGIN IMMEDIATE`), then reconciles the vector store; a
  vector-store failure after commit records per-file
  `vector_delete_pending` tombstones for the hourly sweep and startup retry
  instead of being swallowed. A pre-commit SQL failure rolls back with both
  stores intact.
- **Same-generation republish is idempotent.** Republishing artifacts for
  the file's current generation hash performs no old-generation retirement
  and enqueues no cleanup tombstones — referenced bytes survive the sweep.

### New settings (both UI/API and config layers validate the same ranges)

| Setting | Range | Default |
| --- | --- | --- |
| `embedding_batch_max_chars` | 4096–1048576 | 131072 |
| `embedding_cache_max_entries` | 1000–1000000 | 50000 |
| `near_dup_threshold` | 0.5–0.999999 | 0.96 |
| `orphan_rescan_interval_seconds` | 60–86400 | 3600 |

## Rollout and rollback

- Additive, non-destructive migrations only, journal-backed with the issue
  #512 recovery semantics (rename-recreate-copy inside one `BEGIN IMMEDIATE`,
  crash recovery from the `_old` backup, idempotent re-runs):
  - `migrate_widen_files_status`: rebuilds `files` with the widened status
    CHECK plus the new `partial_embeddings` column (both FTS projections and
    their triggers are rebuilt with the table).
  - `migrate_widen_document_reindex_jobs_status`: widens
    `document_reindex_jobs.status` CHECK with `interrupted` (table rebuild).
  - `migrate_add_document_near_dups`: new `document_near_dups` table +
    `vault_id` index (fresh databases get it from the base schema).
- No public API breaks: responses gain the nullable `near_duplicate_group`
  field only; `PUT /settings` semantics unchanged.

- The previous deploy tolerates the additive schema: the extra
  `document_near_dups` table, the `partial_embeddings` column, and the
  widened CHECKs are inert for old code (old code simply never reads or
  writes them).
- Operator note: this design DOES write both new status values —
  `files.status='partial'` (upload/reindex-path ingests that complete with
  `chunks_failed > 0`) and `document_reindex_jobs.status='interrupted'`
  (rows a restart/shutdown caught mid-reindex). Before downgrading with such
  rows present, map them back to vocabulary the previous deploy understands
  (old code filters on `'pending'/'processing'/'indexed'/'error'` and would
  never match these):
  - `UPDATE files SET status = 'indexed' WHERE status = 'partial';`
    (required — a `partial` row is invisible to the previous deploy's
    filters; the `partial_embeddings` marker column is inert for old code);
    optionally also
    `UPDATE files SET partial_embeddings = 0 WHERE partial_embeddings = 1;`
    to keep rows byte-identical to a pre-upgrade state;
  - `UPDATE document_reindex_jobs SET status = 'failed', error = COALESCE(error, 'interrupted pre-rollback') WHERE status = 'interrupted';`
    — an `interrupted` row would otherwise never match the old deploy's
    status filters.
- Downgrade steps: revert the deploy; no schema down-migration is required.
  The embedding cache file (`data/embedding_cache.db`) can be deleted
  freely — its absence just means cache misses.

## Known limitations

- **`embedding_doc_prefix` changes yield a mixed-prefix index.** Existing
  vectors keep the OLD prefix they were embedded under until re-embedded;
  operators must run a reindex after changing the prefix so the whole index
  shares one prefix contract.
- **Near-duplicate grouping is advisory and bounded.** Comparison scans at
  most the 500 most recent other centroids per vault; it never affects
  retrieval, blocking, or deletion. Group data is computed at ingest time —
  files ingested before this change have no centroid until re-ingested.
  Vault deletion purges the vault's `document_near_dups` rows in the same
  transaction as the files cascade, and per-document delete clears its row
  via `clear_file_centroid` — no stale groups survive deletion.
- **Startup recovery is unconditional for post-parse rows by design.** At
  startup, any `processing` row past the parse stage is treated as an orphan
  and re-enqueued regardless of age; only parse-stage rows keep the
  30-minute age gate. A crashed ingest resumed this way re-runs from the
  recovered entry point, not from its last completed stage.
- **Atom-proxy writes racing a dimension-migrating reindex are deferred.**
  During a dimension-migrating reindex, atom proxy writes that race the
  rebuild window fail their dim check and are deferred (marked
  `failed_retryable`); they self-heal via the retry/startup-resume
  machinery after the new index commits, but proxies are not refreshed
  into the new index as part of the reindex itself.
- The embedding cache has no TTL — invalidation is exclusively by contract
  key (model/embedder revision/prefix/dim/text) and capacity pruning (LRU
  beyond `embedding_cache_max_entries`).

## Operator-visible outcomes

- Files that indexed with failures now show a truthful incomplete state
  instead of a silent success: upload-path ingests land in status `partial`,
  scan/sync ingests in status `indexed` with the `partial_embeddings` flag
  set — and per-file retry repairs them without re-parsing.
- After a restart, the documents and reindex job lists show interrupted
  reindex jobs as `interrupted` (re-enqueueable) instead of a stuck
  `running` row; upload orphans recover within one rescan interval
  (default 1 hour) without a restart.
- A reindex onto a new embedding dimension completes with no search-downtime
  window during the rebuild, and a failed dimension migration leaves the
  previous index untouched.
- Repeated enrichment/retry embeds of unchanged text no longer hit the
  embedding provider (observable as reduced provider traffic and faster
  retries).
