# fix(draft): single-input delete is provenance-true, race-safe, and gated (#785)

## What changed
Deleting one Draft Room input (`DELETE /api/draft-room/drafts/{id}/inputs/{input_id}`) now behaves the way SPEC 6.3 and the section 8.2 route table document, on every path:

- The in-use guard reads the provenance production actually writes: the compile's `draft_evidence` rows and the immutable `input_json` snapshot every compile enqueue freezes (a compile that finishes without evidence rows — a late-stage retry — is still usage). Previously the guard keyed on `draft_jobs.input_id`, a column no compile writer ever sets, so a used input either crashed with a raw `sqlite3.IntegrityError` (500 at the route) or was deleted silently out from under its immutable revision.
- The row delete refuses, inside its own `BEGIN IMMEDIATE`, while a compile is pending/running on the draft (`409 active_job`), matching the sibling mutations (`reserve_input`, `update_input_metadata`, `create_manual_revision`); the evidence CHECK's integrity error is translated to the same `input_in_use` conflict as a backstop.
- The tombstone step tolerates already-absent bytes (`FileNotFoundError` → skip the rename): an input whose file was lost to a crash window whose tombstone startup reconciliation aged away is deletable again instead of being permanently stuck with a 500.
- The route now applies `_require_enabled()` like every other mutating Draft Room route: with `draft_room_enabled=false` it returns `503 draft_room_disabled` instead of executing (single-input delete was never on SPEC 9.2's cleanup allowlist; whole-draft delete remains the disabled-mode cleanup path).

## Why
Frontier-audit findings T1-08-S-02, T1-08-S-01, T1-08-S2-03, T1-08-S-03, and T1-08-K-04 (issue #785): five independent defects on one route — a dead guard, a missing in-transaction concurrency check, an unguarded tombstone rename, a reconcile that strands rows, and a skipped capability gate. Trace: `.agents/issue-traces/785-draft-input-delete-safety` (issue-tracer v3; 9 frozen acceptance checks, anchor published pre-implementation).

## Migration
None. No schema or settings change; the new 409s replace former 500s and former silent successes, both of which were the reported defects.

## Caveats
- The legacy `draft_jobs.input_id` in-use legs are kept for backward compatibility with pre-writer rows; the existing test that fabricates that shape (`test_draft_deletion.py::test_input_in_use_by_completed_compile_is_refused_and_file_kept`) still passes against them and is a known post-freeze cleanup candidate.
- Precedence when an input is both in use and byte-less: the `409 input_in_use` wins (SPEC 6.3's 409 is unconditional); the row remains escapable via whole-draft delete.
- `DraftInputStorage.reconcile` still age-purges crash-window tombstones without consulting the database; this is now harmless (the retried delete removes the row idempotently) and teaching the storage layer about DB rows was rejected as a layering violation during plan review.
