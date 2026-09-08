# Workstream C1: non-destructive, restart-safe index and database recovery (Issue #512, PR 1 of 3)

## What changed

### Backend

- **`backend/app/models/migration_journal.py` (new)**: migration/recovery
  journal recording schema version (`MIGRATION_SCHEMA_VERSION`), migration
  phase (`start`/`succeeded`/`failed`/`recovered`), recovery outcome, and
  the authoritative index generation. Exposes the contract surfaces the
  issue assigns to this slot: `record_migration_outcome`,
  `record_schema_version`, `invalidate_derived_data` (re-exported at module
  scope on `app.models.database` — the explicit schema/claim-source
  invalidation interface for roadmap slots D1/D2), and
  `publish_index_generation` (exposed from `app.services.vector_store` —
  the generation publication interface for slot C2). A new
  `migration_journal` base table is defined in BOTH the SCHEMA constant
  (fresh DBs) and `migrate_add_migration_journal` (existing DBs), with the
  schema-drift guard bumped accordingly.

- **`backend/app/models/database.py`**:
  - **DB-001** (`migrate_add_curator_claim_support` + the lint analog
    `migrate_add_wiki_lint_findings_json_check`): the rename→recreate→copy
    swap now runs inside ONE explicit `BEGIN IMMEDIATE` transaction using
    individual `execute()` calls only (`executescript` implicitly commits
    pending transactions and was the crash-window culprit), with rollback
    on every failure path including FK validation. Retry reconciliation is
    row-identity-guarded (count parity AND `EXCEPT` id-parity), so a
    lingering `wiki_claims_old`/`_wiki_lint_findings_old` backup is only
    dropped when the destination provably contains every backup row;
    otherwise the backup is authoritative and gets restored.
  - **DB-002** (`migrate_widen_wiki_claim_sources_source_kind`): the
    canonical-table probe no longer early-returns before the backup check —
    the renamed-only state (`wiki_claim_sources_old` without canonical) is
    restored on retry (previously unreachable dead code), and the
    both-present state uses the same row-identity guard.
  - **DB-003** (`migrate_add_wiki_claims_unique_claim_text`): before the
    dedup DELETE, every `wiki_claim_sources` row pointing at a doomed twin
    is remapped to the surviving `MAX(id)` twin, transactionally, with
    `PRAGMA foreign_key_check` required empty before commit — evidence is
    no longer orphaned by dedup.
  - **SEARCH-005** (`migrate_add_files_content_fts`): virtual-table
    creation, triggers, and the backfill rebuild now run in ONE explicit
    transaction; the `table_is_new` gate is gone. A failed first rebuild
    rolls the whole sequence back, so a retry re-runs creation+backfill
    instead of silently leaving existing documents unsearchable by body.

- **`backend/app/services/vector_store.py`**:
  - **VECTOR-001a**: a transient `open_table("chunks")` failure at startup
    no longer drops and overwrites the authoritative table — the error
    propagates as `VectorStoreConnectionError` (table preserved).
  - **VECTOR-001b**: the four rewrite migrations
    (`migrate_add_vault_id`/`chunk_scale`/`sparse_embedding`/`parent_window`)
    now rewrite through a shared staging swap: the replacement table
    (`chunks_rebuild`) is created and row-count-validated BEFORE any drop;
    only then is the old table dropped and the canonical table recreated
    from the validated data. Any failure leaves the original (pre-drop) or
    the staging table (post-drop) recoverable on disk and RAISES —
    failure-as-zero is gone. A stale staging table from an interrupted run
    is reconciled at the next startup (restored or validated-then-dropped).
    A successful swap publishes the authoritative generation to the
    migration journal.
  - **VECTOR-005**: `migrate_add_parent_window` on an EMPTY legacy-schema
    table now applies the current schema (mirroring the sibling
    vault_id/chunk_scale/sparse empty-branches) instead of skipping and
    leaving new-format writes failing.

- **`scripts/migrate_embeddings.py`**:
  - **VECTOR-006**: dimension detection reads the table through the
    installed LanceDB API (`open_table("chunks").schema` →
    `embedding.list_size`), with the raw pyarrow path kept as fallback and
    the dead duplicated branch removed. A matching dimension is a no-op; a
    genuine mismatch takes the explicit migration path; an UNREADABLE
    dimension on a non-empty index is an explicit operator-facing error —
    the helper no longer schedules a wipe/reset of an index it could not
    even read.

### Tests

- `backend/tests/test_storage_recovery_sqlite.py` — real-sqlite recovery
  states: DB-001 backup-only / backup+empty-destination / injected
  partial-copy failure; lint analog states; DB-002 renamed-only restore and
  both-present parity; DB-003 duplicate-claims evidence remap with
  `foreign_key_check`; SEARCH-005 fail-rebuild→retry→body-FTS MATCH.
- `backend/tests/test_vector_recovery.py` — VECTOR-005 empty-legacy schema
  application + new-format write; VECTOR-001a no-drop-on-transient-open-
  error; VECTOR-001b staging-create-failure preserves the original and
  raises; final-create failure after drop leaves staging recoverable and a
  retry restores canonical from staging; success path cleans staging and
  publishes a journal generation.
- `backend/tests/test_migration_journal.py` — journal DDL in both SCHEMA and
  legacy paths; record/invalidate/publish round-trips.
- `backend/tests/test_migrate_embeddings_dim.py` — real lancedb-0.36 table
  dimension detection (1024); unknown-dimension errors without wiping.
- `backend/tests/test_vector_probes.py` — FTS reinit no-duplicate/no-warning
  pin and parent-window probe true/false pin (VECTOR-002/VECTOR-003
  preserved-correct behavior).
- `backend/tests/test_init_table_adversarial.py` realigned to the new
  no-drop contract (the destructive overwrite expectations were pinning the
  defect the issue orders removed).

## Rollout and rollback

- Migrations remain idempotent; no settings keys added; no API surface
  change. Rollback = revert the diff; the journal table and any leftover
  `chunks_rebuild` staging table are inert additives.
- Operator-visible: recovery failures now raise loudly instead of returning
  0, and the journal's latest outcomes are available via
  `migration_journal.latest_outcomes`.

## Known limitations

- The reconciliation guard uses row-identity (id-set parity + count), not
  full content hashes; a same-id-different-content divergence between
  backup and destination is treated as complete. This matches the copy
  semantics of the repaired migrations (verbatim column copies).
