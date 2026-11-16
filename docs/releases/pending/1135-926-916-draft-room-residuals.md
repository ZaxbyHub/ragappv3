# Draft Room residuals: audit content-free payload, pagination bound, crash-strand sweep, census precision, typed-branch cleanup logs

Issues: #1135, #926, #916
Trace: `.agents/issue-traces/1135-926-916-draft-room-residuals` (issue-tracer v3)

## What changed

- **Content-free audit payload (#1135 claim 1)**: the finding-disposition
  audit event no longer writes `span_text_sha256` into the non-cascaded
  `security_audit_log` (SPEC 6.3 / 17.1 item 24 — content-retention). The
  span hash stays in `draft_findings`, where waiver-text pinning reads it;
  only the audit payload loses the manuscript-prose fingerprint.
- **Pagination overflow bound (#1135 claim 2)**: all SEVEN paginated Draft
  Room list routes (findings + evidence/claims/revisions siblings) bound
  `page` with `le=MAX_LIST_PAGE = ((2**63 - 1) // 100) + 1` — the largest
  page that cannot overflow the SQLite signed-64-bit offset bind for any
  `per_page <= 100`. Requests beyond it now answer 422 instead of an
  unhandled 500. **API tightening**: formerly-valid huge-page requests that
  returned empty 200 lists are now refused; real page counts never approach
  the bound.
- **Re-archive idempotent refusal (#1135 claim 3a)**: archiving an
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
  write only at a live SQL statement position — comments (`--` and `/* */`)
  are stripped quote-aware across all four SQLite quoting styles, the verb
  must start a statement (start-of-string/`;`/`(`), and the prose colon
  after the table rejects sentence noise. Non-first-statement docstrings
  (PEP 258 attribute docstrings, bare strings under `if`/`try`) are excluded,
  and module-level constants feed write expressions across a name boundary
  (`PREFIX + "draft_jobs SET ..."`), counted exactly once per use with
  function-local shadowing resolving to nothing. The census docstring now
  names every disclosed limitation with its trigger (cross-module constants,
  `job_lease` constructor-arg tables, symlinked dirs, `PermissionError`
  traversal, local shadowing). Real counts pinned exactly: 8 route / 7
  processor / 11 pipeline, zero unlisted, store floor 40 (46 measured).
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
