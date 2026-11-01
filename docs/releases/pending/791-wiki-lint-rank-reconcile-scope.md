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
  force-resolved by a lint run. The production writers each carry an explicit
  origin: the linter upsert path (`linter`), the two invalidation writers
  (`invalidation`), `compile_query_job`'s unverified-claim INSERT
  (`compiler`), and the curator path (`curator`). `create_lint_finding`
  remains the store's public seeding API with a non-linter default; it has no
  production caller in this change (the curator moved onto the upsert path).
- **Auto-resolution is distinguishable from user dispositions, and recurrence
  reopens.** The resolve pass writes `auto_resolved`, which no user can set.
  For each detected fingerprint: an open row is kept; any user-terminal row
  (dismissed/resolved/acknowledged) suppresses — always, even alongside
  `auto_resolved` siblings; otherwise the newest same-origin `auto_resolved`
  row flips back to open, so a recurring issue reopens the same row instead
  of growing the list. The reopened row is refreshed from the new detection
  (severity/details/related ids), because those fields are not part of the
  fingerprint.
- **Curator persistence is idempotent.** Curator lint findings reconcile
  through the fingerprint-aware `upsert_lint_findings(origin='curator')`
  instead of a raw INSERT, and accepted claims use exact- then
  normalized-text lookup with a `(source_kind='document', file_id)` dedupe
  rule before attaching provenance — same-file reruns attach nothing,
  cross-file reruns attach one source per file. Curator lint titles carry a
  per-instance discriminator (the claim text, or the contradiction pair), so
  two different claims in one rejection class are two rows and dismissing one
  does not silence the other. Specs are validated before the single batch
  write, so one malformed finding cannot discard its siblings, and a persist
  failure records the exception CLASS only (sqlite3 driver messages quote
  schema vocabulary, and this string reaches vault readers through the job's
  `result_json`). Persist failures are still recorded into the curator
  summary, never raised into the compile job.
- **The two 0-row rollback gaps are closed.** `update_lint_finding` and
  `KMSStore.update_entry` roll back before returning None on a missing id,
  matching every sibling 0-row path in both stores. An AST census over every
  `*_store.py` module fails when a 0-row early return ships without a rollback
  on the taken path (equivalent spellings included).
- **Only open findings are actionable in the panel.** `WikiLintPanel` offers
  Resolve/Dismiss for `status === "open"` only and labels non-open rows.
  Acting on an `auto_resolved` row would convert a system-reopenable row into
  a permanent user suppression, defeating the reopen contract.

## Behaviour change worth knowing

- Lint lists now order critical, high, medium, low (was medium, low, high,
  critical).
- Open findings that go away now read `auto_resolved` instead of `resolved`;
  the frontend `WikiLintFinding` status union gained that value. The panel
  gates its Resolve/Dismiss actions on `status === "open"`, so `auto_resolved`
  rows render with an "auto-resolved" label and no action buttons.
  `auto_resolved` reopens on recurrence; user dispositions keep suppressing
  forever.
- **Findings the linter did not create are no longer auto-closed.** Because
  the resolve pass is scoped to `origin='linter'` rows, `stale` /
  `weak_provenance` / compiler / curator rows stay `open` until a user
  disposes of them — where the pre-#791 reconciler force-resolved every
  undetected open row. They appear in the unfiltered `GET /wiki/lint` list
  (which is what the panel renders) and are dismissable there; per-file
  `lint_count` badges do not count them, because the invalidation writers
  carry no `related_page_ids`. This is the intended reading of the issue's
  constraint that a lint run must never force-resolve findings it did not
  create; the missing producer-side closer is tracked as follow-up work.
- Disclosed migration limits (both identical to pre-fix behavior, so no
  regression): legacy reconciler-written `resolved` rows are
  data-indistinguishable from user resolves and stay user-terminal
  (suppressed forever); legacy curator rows carrying linter-emitted finding
  types backfill as `linter` and remain resolvable. AC3's reopen contract
  applies to auto-resolutions occurring after this fix.
- Rollback note: if this change is reverted after merge, the `origin` column
  and the five-value status CHECK survive (base migrations no-op on the
  rebuilt table), and base code treats any non-open row as a permanent
  suppression — so `auto_resolved` rows written by this change would stay
  suppressed until it is re-applied.

## Migration

`migrate_wiki_lint_findings_origin_auto_resolved` (automatic, idempotent):
rebuilds `wiki_lint_findings` with the `origin` column and the five-value
status CHECK, backfilling origins (fixed "Unverified claim: extracted from
query answer..." title → `compiler`; `stale`/`weak_provenance` →
`invalidation`; else `linter`), preserving ids, statuses and row counts, and
preserving the AUTOINCREMENT high-water. No settings change, no restart
required beyond the normal migration-on-boot. On a database where this
migration has not applied (a failed startup migration — the app then serves
with `migrations_ok = False`), the new origin-bearing INSERTs raise
`OperationalError`, which rolls back the surrounding file/memory delete; a
runtime schema probe is tracked as follow-up work.

## Caveats

- Curator claims deliberately do NOT borrow the deterministic compile path's
  unverified→active promotion or superseded→active reactivation (WIKI-006):
  in `active_if_verified` deployments a quote-verified curator claim matching
  an existing unverified/superseded claim attaches provenance without
  promoting it — strictly better than today (the source was dropped entirely
  via the UNIQUE violation), with full parity filed as **#929**. The persistence/migration robustness items (schema probe, concurrent-upsert duplicates, immortal duplicate open rows, compile-query retry loss, multi-worker migration race) are filed as **#948**; exposing `origin` on the read path is **#949**.
- `compile_query_job`'s unverified-claim rows are now origin-scoped against
  resolution, but their per-query creation semantics are unchanged (a re-asked
  query answer can still insert a second row); only reconciler ownership was
  in scope.
- The `origin` column is currently write-only: it is not projected into
  `WikiLintFinding` or any API response, so no consumer can yet distinguish a
  compiler/curator/invalidation row from a linter row. Exposing it is filed as
  **#949**.
- The `origin` vocabulary decision gates Workstream O PR 5 (#795, curator
  lint interaction), per #791's Dependencies note.
