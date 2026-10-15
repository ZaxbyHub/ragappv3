# fix(wiki): lint findings sort by severity rank and the reconciler stops resolving or suppressing findings it does not own (#791)

## What changed

`GET /wiki/lint` sorted its findings by a lexical `ORDER BY severity DESC`, so
`high` and `critical` rows rendered below `medium` and `low`. The same
reconciliation pass that keeps the lint list current treated every open row in
the vault as linter-owned: it force-resolved `stale`/`weak_provenance`/
curator/compiler rows the linter never produced, and its `resolved` state was
indistinguishable from a user's `dismissed`, so a resolved issue that recurred
was suppressed forever. Adjacent in the same seam: every curator recompile
inserted a fresh open copy of each curator lint finding, and a repeated
curator `claim_text` hit `UNIQUE(vault_id, claim_text)`, rolled the
claim+source savepoint back, and silently dropped the second file's
provenance. `WikiStore.update_lint_finding` and `KMSStore.update_entry` both
returned from their not-found paths with the pooled connection still
`in_transaction`.

- **Severity sorts by rank.** `list_lint_findings` orders through a
  `CASE severity` rank expression (critical, high, medium, low) with
  `created_at` breaking ties inside a rank. The frontend panel never
  re-sorted, so this is the whole fix for the ordering defect.
- **Producer provenance is persisted.** `wiki_lint_findings` gains an
  `origin` column (`linter` / `invalidation` / `compiler` / `curator`) and a
  system-only `auto_resolved` status through a rename-rebuild migration
  modeled on `migrate_add_wiki_lint_findings_json_check` (journal, recovery
  prologue, row-count parity, AUTOINCREMENT high-water). The reconciler's
  resolve pass now runs only for the linter's own reconciliation and only
  over `origin='linter'` rows: findings the linter did not create are never
  force-resolved by a lint run. All five writers carry an explicit origin
  (linter upsert path, one-off seed default `invalidation`, the two
  invalidation writers, `compile_query_job`'s unverified-claim INSERT, and
  the curator path).
- **Auto-resolution is distinguishable from user dispositions, and recurrence
  reopens.** The resolve pass writes `auto_resolved`, which no user can set.
  For each detected fingerprint: an open row is kept; any user-terminal row
  (dismissed/resolved/acknowledged) suppresses — always, even alongside
  `auto_resolved` siblings; otherwise the newest same-origin `auto_resolved`
  row flips back to open, so a recurring issue reopens the same row instead
  of growing the list.
- **Curator persistence is idempotent.** Curator lint findings reconcile
  through the fingerprint-aware `upsert_lint_findings(origin='curator')`
  instead of a raw INSERT, and accepted claims use exact- then
  normalized-text lookup with a `(source_kind='document', file_id)` dedupe
  rule before attaching provenance — same-file reruns attach nothing,
  cross-file reruns attach one source per file. Persist failures are still
  recorded into the curator summary, never raised into the compile job.
- **The two 0-row rollback gaps are closed.** `update_lint_finding` and
  `KMSStore.update_entry` roll back before returning None on a missing id,
  matching every sibling 0-row path in both stores.

## Behaviour change worth knowing

- Lint lists now order critical, high, medium, low (was medium, low, high,
  critical).
- Open findings that go away now read `auto_resolved` instead of `resolved`;
  the frontend `WikiLintFinding` status union gained that value (type-level
  only — the panel sends only resolved/dismissed). `auto_resolved` reopens on
  recurrence; user dispositions keep suppressing forever.
- Disclosed migration limits (both identical to pre-fix behavior, so no
  regression): legacy reconciler-written `resolved` rows are
  data-indistinguishable from user resolves and stay user-terminal
  (suppressed forever); legacy curator rows carrying linter-emitted finding
  types backfill as `linter` and remain resolvable. AC3's reopen contract
  applies to auto-resolutions occurring after this fix.

## Migration

`migrate_wiki_lint_findings_origin_auto_resolved` (automatic, idempotent):
rebuilds `wiki_lint_findings` with the `origin` column and the five-value
status CHECK, backfilling origins (fixed "Unverified claim: extracted from
query answer..." title → `compiler`; `stale`/`weak_provenance` →
`invalidation`; else `linter`), preserving ids, statuses and row counts, and
preserving the AUTOINCREMENT high-water. No settings change, no restart
required beyond the normal migration-on-boot.

## Caveats

- Curator claims deliberately do NOT borrow the deterministic compile path's
  unverified→active promotion or superseded→active reactivation (WIKI-006):
  in `active_if_verified` deployments a quote-verified curator claim matching
  an existing unverified/superseded claim attaches provenance without
  promoting it — strictly better than today (the source was dropped entirely
  via the UNIQUE violation), with full parity filed as follow-up work.
- `compile_query_job`'s unverified-claim rows are now origin-scoped against
  resolution, but their per-query creation semantics are unchanged (a re-asked
  query answer can still insert a second row); only reconciler ownership was
  in scope.
- The `origin` vocabulary decision gates Workstream O PR 5 (#795, curator
  lint interaction), per #791's Dependencies note.
