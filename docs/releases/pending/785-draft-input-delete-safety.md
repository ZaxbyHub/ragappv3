# fix(draft): single-input delete is provenance-true, race-safe, and gated (#785)

## What changed
Deleting one Draft Room input (`DELETE /api/draft-room/drafts/{id}/inputs/{input_id}`) now matches SPEC 6.3/8.2/9.2 on every path the audit flagged:

- **The in-use guard reads the provenance production actually writes.** It previously keyed on `draft_jobs.input_id`, a column no compile writer sets, so a used input either crashed with a raw `sqlite3.IntegrityError` (500) or was deleted silently out from under its immutable revision. It now checks the compile's evidence rows *and* the compile's immutable `input_json` input snapshot, and accepts a compile in any terminal state (completed/failed/cancelled) — a run that wrote evidence and then failed still holds derivative data, so restricting to `completed` only turned a correct refusal into a misleading one.
- **The row delete refuses inside its own transaction**: `409 input_in_use` for a used input, `409 active_job` when a compile is pending or running (the same code the shared `_assert_no_active_compile` now raises for every sibling mutation, so one condition has one code), and an in-transaction re-check closes the window between the service-level check and the `BEGIN IMMEDIATE`. The evidence CHECK's integrity error is translated to the same conflict as a backstop.
- **The tombstone flow no longer strands data.** Absent bytes are tolerated (with a warning log); a `DraftNotFoundError` from the row delete now *commits* the tombstone rather than restoring it, so two concurrent DELETEs cannot end with bytes on disk and no row. A failed restore can no longer mask the conflict it was compensating for, and a missing storage root fails closed instead of being silently recreated by `mkdir(parents=True)`.
- **Upload compensation is exempt from the guards.** `_delete_input_row_best_effort` rolls back the row it just created after a failed upload; applying the new refusal guards there stranded a row whose bytes were already discarded and permanently 409'd the owner's re-upload.
- **The route applies `_require_enabled()`** like every other mutating Draft Room route: with `draft_room_enabled=false` it returns `503 draft_room_disabled`.

## Behaviour change worth knowing
An input listed in a completed compile's input snapshot is now treated as in use **even when nothing derived from it directly** — for example a `style`-role input that the retrieval stages never cite. The snapshot is the only durable record of which inputs a compile was given, and a late-stage retry can finish without writing any evidence row at all, so this is the honest reading of "used by a compile". SPEC 6.3 and `docs/draft-room.md` were amended to state it; previously-succeeding deletes of such inputs now return `409 input_in_use`, and whole-draft deletion is the way to remove one.

## Migration
No schema or settings change. No restart required.

## Caveats
- The legacy `draft_jobs.input_id` in-use legs are kept for backward compatibility with rows written before those writers existed; the existing test that fabricates that shape (`test_draft_deletion.py::test_input_in_use_by_completed_compile_is_refused_and_file_kept`) still passes against them and is a known post-freeze cleanup candidate.
- Precedence when an input is both in use and byte-less: `409 input_in_use` wins (SPEC 6.3's 409 is unconditional); the row remains escapable via whole-draft delete.
- `DraftInputStorage.reconcile` still age-purges crash-window tombstones without consulting the database; this is now harmless (the retried delete removes the row idempotently) and teaching the storage layer about DB rows was rejected during plan review as a layering violation.
- An `OSError` raised while restoring a tombstone (for example a Windows file lock) is logged and the original conflict is re-raised; the bytes stay in `.trash` for startup reconcile.