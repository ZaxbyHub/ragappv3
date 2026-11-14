# fix(wiki): expose lint-finding origin on every read path and in the lint panel (#949)

## What changed

- `WikiLintFinding` (`backend/app/services/wiki_store.py`) gains an `origin`
  field (default `"linter"`, mirroring the column's DEFAULT), and
  `_to_lint_finding` projects it from the row. Because every lint-finding
  route serializes through `dataclasses.asdict` (`_as_dict` in
  `backend/app/api/routes/wiki.py`), the field now appears on all four
  response surfaces with no route-code change: `GET /wiki/lint`,
  `POST /wiki/lint/run`, `POST /wiki/lint/{finding_id}/resolve`, and the
  per-page `lint_findings` payload of `GET /wiki/pages/{page_id}`.
- The frontend `WikiLintFinding` interface
  (`frontend/src/lib/api/wiki.ts`) gains an optional
  `origin: "linter" | "invalidation" | "compiler" | "curator"` field.
- `WikiLintPanel` (`frontend/src/pages/WikiLintPanel.tsx`) renders the
  origin as a segment of the finding's meta line whenever it is not
  `linter` (e.g. `high · contradiction · curator`); `linter` rows and
  payloads without the field render exactly as before.

## Why

PR #791 added the `origin` column so the lint reconciler can tell its own
rows from invalidation/compiler/curator rows, but the column was
write-only: no read path projected it, so no consumer, operator, or audit
could distinguish a compiler row from a curator row. The PR #930 review's
top surviving finding (PRR-001, curator class-level suppression) is
exactly the defect class a visible provenance field makes diagnosable in
the field. Issue #949 tracks this follow-up.

## Migration steps

None. The change is purely additive on the read side; the column and its
CHECK vocabulary (`linter`/`invalidation`/`compiler`/`curator`) already
exist from #791 (schema plus rename-rebuild migration). Clients that
ignore the new field are unaffected; the TypeScript field is optional so
payloads cached before this change still parse.

## Known caveats

- `origin` is read-only provenance: no route accepts it as input, and
  `WikiStore.update_lint_finding` continues to reject every status outside
  its user-settable set (`resolved`/`dismissed`/`acknowledged`) — pinned by
  `backend/tests/test_o01_lint_resolve_route.py`.
- The reconciler's auto-resolve scoping (`origin='linter'` rows only),
  severity rank ordering, and auto-resolve/reopen semantics are untouched;
  the frozen #791 suites
  (`backend/tests/test_o01_wiki_lint_reconcile.py`,
  `backend/tests/test_o01_origin_guards.py`,
  `backend/tests/test_o06b_lint_reconcile_race.py`) still pass.
- New coverage: `backend/tests/test_o03_lint_origin_exposed.py` (origin on
  all four surfaces, per-writer values, unchanged field set) and
  `frontend/src/pages/WikiLintPanel.origin.test.tsx` (badge for the three
  non-linter producers, silence for `linter`, back-compat without the
  field).
