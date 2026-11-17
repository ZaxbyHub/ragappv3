# Draft Room residuals: finding-disposition audit payload, pagination bound, crash-strand sweep, census precision, typed-branch cleanup logs

Issues: #1135, #926, #916
Trace: `.agents/issue-traces/1135-926-916-draft-room-residuals` (issue-tracer v3)

## What changed

- **Content-free finding-disposition audit payload (#1135 claim 1)**: the
  finding-disposition audit event no longer writes `span_text_sha256` into
  the non-cascaded `security_audit_log` (SPEC 6.3 / 17.1 item 24 —
  content-retention). The span hash stays in `draft_findings`, where
  waiver-text pinning reads it; only the finding-disposition audit payload
  loses the manuscript-prose fingerprint. This claim is deliberately scoped:
  other audit events still carry integrity codes (see Residual audit fields
  below), which remain a maintainer decision.
- **Pagination overflow bound (#1135 claim 2)**: all SEVEN paginated Draft
  Room list routes (findings + evidence/claims/revisions siblings) bound
  `page` with `le=MAX_LIST_PAGE = ((2**63 - 1) // 100) + 1` — the largest
  page that cannot overflow the SQLite signed-64-bit offset bind for any
  `per_page <= 100`. Requests beyond it now answer 422 instead of an
  unhandled 500. **API tightening**: formerly-valid huge-page requests that
  returned empty 200 lists are now refused; real page counts never approach
  the bound.
- **Re-archive refusal / API tightening (#1135 claim 3a)**: archiving an
  already-archived draft now raises the store's transition error (409
  `invalid_transition` over the route) instead of silently bumping
  `lock_version` and writing a second `draft_archived` event. The
  self-transition bypass lived in `_check_transition`'s `target == current`
  early-return; the refusal is placed in `archive_draft` ahead of the
  unconditional UPDATE, leaving the general same-status contract untouched.
- **Crash-strand startup sweep (#1135 claim 3b)**: new
  `DraftStore.recover_stranded_compile_drafts()` runs in
  `_recover_on_startup` right after the orphan-compile reset and settles
  drafts parked on `queued`/`running` whose LATEST compile job is terminal
  `failed` with no live compile job — the exact state a crash between the
  job-settle and draft-settle transactions of `_fail_compile_job_sync`
  leaves. Latest-job-aware by construction (older-failed + newer
  cancelled/completed/pending histories are never mis-settled); idempotent.
  The single-transaction settlement alternative remains with #790's
  single-writer consolidation.
- **Census precision (#926)**: the table-ownership census now recognizes a
  write only at a live SQL statement position. Comments are stripped
  quote-aware across all four SQLite quoting styles (a single-line `/* */`
  comment is replaced by a space so the surrounding tokens do not join), the
  verb must start a statement (start-of-string, `;`, `(`, or `)` — the `)`
  anchor admits CTE-prefixed writes such as
  `WITH x AS (SELECT 1) UPDATE drafts ...`), and the prose colon after the
  table rejects sentence noise. Single-quoted SQL literals are DATA: their
  content is blanked (token boundaries kept), so a write embedded in a
  string value (`SELECT '; UPDATE drafts ...'`) cannot forge a live
  statement, while double-quote/backtick/bracket identifier content — the
  table-name spellings — stays visible. Non-first-statement docstrings
  (PEP 258 attribute docstrings, bare strings under `if`/`try`) are excluded,
  and module-level constants feed write expressions across a name boundary
  (`PREFIX + "draft_jobs SET ..."`), counted exactly once per use with
  function-local shadowing resolving to nothing. The census docstring now
  names every disclosed limitation with its trigger (cross-module constants,
  `job_lease` constructor-arg tables, symlinked dirs, `PermissionError`
  traversal, local shadowing, and the `)` anchor's over-count of a write
  that follows a parenthetical in prose — fail-loud, not silent). Real
  counts pinned exactly: 8 route / 7 processor / 11 pipeline, zero unlisted,
  store floor 40 (46 measured).
- **Typed-branch cleanup logs (#916)**: each of the three typed promotion
  branches (`DuplicateError` 409 / `TooLargeError` 413 /
  `PathConflictError` 409) with `cleanup_incomplete=True` now emits ONE
  ERROR record on the route logger carrying `draft_id` and the error class
  name only — never the exception message (the #789 leak class). The
  generic branch's shipped two-record baseline (`logger.exception` + the 5xx
  response handler) is unchanged, and the PathConflict ERROR coexists with
  the #1119 raw-path WARNING by design (distinct facts, distinct channels).

## Operator notes

- Startup recovery logs `settled N stranded compile draft(s) to failed`
  when the new sweep repairs crash-stranded drafts.
- The cleanup-failure ERROR line (`promotion cleanup incomplete
  draft_id=… error=…`) is the operator-visible signal that a promotion's
  compensating deletes failed and an orphan may exist; it pairs with the
  existing `draft_promotion_cleanup_failed` audit event.

## Residual audit fields (maintainer decision — feedback FB-07)

The finding-disposition audit is now content-free, but other events in the
same append-only, non-cascaded `security_audit_log` still carry values worth
an explicit maintainer call (all pre-existing, none introduced here):

| Event | Field | Why it needs a decision |
|---|---|---|
| `draft_exported` | `content_sha256` | content-derived fingerprint; removal changes export correlation |
| `draft_ready_marked` | `content_sha256` | same class, via `_sync_mark_ready` |
| `draft_promoted` | `source_sha256`, `filename` | integrity code + title-derived filename |
| `draft_job_retried`, `draft_compile_requested` | `compile_input_sha256` | composite of brief + input digests (not manuscript prose) |
| `draft_promotion_cleanup_failed` | `detail: str(exc)` | raw exception text (PathConflict embeds a server path); the new ERROR log is class-name-only but this audit row is not |
| `draft_promoted` error paths | `organization_error`, `draft_event_error` (`str(exc)`) | same raw-exception class |

Each is a product/compatibility decision about the audit ledger, not a defect
introduced by this PR; they are listed in the PR body's Considered table and
are awaiting the maintainer's call.
