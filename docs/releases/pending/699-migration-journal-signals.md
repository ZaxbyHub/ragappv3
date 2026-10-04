# Migration journal signals reflect real recovery and failure state (issue #699, Workstream B PR 10 of 16)

## What changed

### Backend

- **`backend/app/models/database.py`** — the migration journal now tells the
  truth about what migrations actually did:
  - Every journaled migration opens its `BEGIN IMMEDIATE` INSIDE the try whose
    except journals the terminal `failed` row (nine sites), so a lock error at
    the transaction open records exactly one terminal `failed` outcome instead
    of stranding a bare `start` row (`migrate_add_files_content_fts` still
    opens — and rolls back — its transaction on every call, even when the
    index is already complete, so that lock-error path is always observable).
  - Clean boots are journal-silent: all nine per-boot journal writers gained
    probe-first prologues (already-done probes return silently, with no start
    row), eliminating the ~19 routine rows per restart that buried the
    operator's "latest 3 outcomes" startup summary within one or two boots.
    The `migrate_add_user_onboarding_state` writer that master gained from
    #782 mid-flight received the same gate in this PR's merge follow-up (ten
    gated writers total). Recovery probes keep their #512 DB-001/DB-002
    first-position ordering: backup-table states are still restored/dropped
    before any silent return, and genuine recoveries still journal
    `recovered` signal rows.
  - `migrate_add_files_content_fts` regained an already-run gate: when the
    virtual table AND all three sync triggers exist, the boot writes no
    journal rows and skips the per-boot full FTS rebuild + `rebuilt`
    invalidation. Safe because #512 made creation+triggers+backfill ONE
    transaction — a failed attempt rolls the table away, so the gate cannot
    skip a half-built index, and a retry re-runs everything (SEARCH-005
    semantics unchanged, pinned by the existing atomic-retry test). The
    docstring's stale "gated on table existence" promise now matches reality.
  - Every `PRAGMA foreign_key_check` integrity gate is scoped to the tables
    the enclosing migration actually rewrote — child-table checks for parent
    rebuilds (`wiki_claim_sources` + `wiki_relations` for the wiki_claims
    rebuild/dedup paths; `draft_claim_sources` for draft_claims; the eight
    files(id) children for the files status widen; the already-scoped
    siblings unchanged; the childless wiki_lint_findings check kept in
    table-arg form and documented as vacuous by construction). An unrelated
    pre-existing orphan anywhere else in the database can no longer
    permanently block an otherwise-unrelated migration.
  - The claims dedup now remaps `wiki_relations.claim_id` to the surviving
    twin before deleting duplicate claims (same shape as the #512 DB-003
    evidence remap), so the `ON DELETE CASCADE` no longer silently destroys
    relation rows; the stale "only child" comment is corrected.
  - Every destructive dedup delete is now logged with its row count and
    journaled INSIDE the delete's own transaction (row exists iff the delete
    committed): `chat_messages` (session/turn/role), the two
    `user_sessions` refresh-hash paths in `run_migrations`, the
    `wiki_relations` triple dedup, and the `wiki_claims` dedup count row.
- **`backend/app/models/migration_journal.py`** — `invalidate_derived_data`'s
  docstring no longer claims consumers "know to rebuild" (no automated
  consumer reads `outcome='rebuilt'`; the row is an operator-visible audit
  marker). New `latest_outcomes_with_signal()` composes the unchanged
  newest-first tail with the newest failure/recovery row when that signal
  sits outside the window.
- **`backend/app/lifespan.py`** — the startup summary uses the new helper
  and logs one extra signal line when a failure/recovery is buried under
  pre-existing routine rows (databases upgraded from before this change).

### Tests

- `backend/tests/test_b10_migration_journal_signals.py` (new, frozen
  acceptance checks AC1-AC6) and
  `backend/tests/test_b10_journal_noise_guard.py` (new: zero-noise clean
  boots, signal-window helper branches, user_sessions count journaling,
  wiki_relations remap, recovery states for the previously-untested
  `files_old`/`document_reindex_jobs_old`/`draft_claims_old` trio,
  stale-backup-when-complete disposition for curator/lint/claim-sources, and
  an AST guardrail requiring every executed `PRAGMA foreign_key_check` in
  backend/app to name a table).
- `backend/tests/test_b08_near_dups_migration.py` — the no-op near-dups run
  now asserts journal silence (declared forced change; it previously pinned
  the per-boot noise this PR removes). The #782 onboarding migration's rerun
  test (`backend/tests/test_m02_onboarding_migration.py`) asserts the same
  silence for the tenth writer (same declared forced-change shape).

## Known scope (explicitly not covered here)

- A *sustained* write lock can still starve a journal write past the sqlite
  busy timeout (the never-raises journaling contract from #512 is kept
  deliberately), and a hard crash mid-attempt can leave a bare `start` row;
  such states are detected by the #512 recovery probes on the next boot, not
  terminalized retroactively.
- A pre-#512 database whose FTS creation committed but whose backfill never
  did is not detected by the completeness gate (reachability: any #512-era
  boot re-ran the rebuild and self-healed it).
- The connection-pool lock-timeout hazards around concurrent-worker
  migrations are owned by #700 (PR 11); the destructive rebuild/dedup
  row-selection correctness is owned by #701 (PR 12).
