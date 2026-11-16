# Draft Room typed promotion cleanup-incomplete branches now log one ERROR record (#916)

Issue: #916 (pre-existing gap at base `ed23488b`, surfaced by the PR #907
swarm review; the #907 generic-branch `logger.exception` does not cover the
typed branches because they answer 4xx, which the shared handler does not
log).

## What changed

`_run_promotion` in `backend/app/api/routes/draft_room.py` — the error
translation helper shared by the input and revision promote branches — now
emits exactly one ERROR record on the `app.api.routes.draft_room` logger
for each typed promotion branch that fires with
`exc.cleanup_incomplete=True`:

- `DraftPromotionDuplicateError` (409 `duplicate_document`),
- `DraftPromotionTooLargeError` (413 `promotion_too_large`),
- `DraftPromotionPathConflictError` (409 `promotion_path_conflict`).

The record is emitted by the new helper
`_log_promotion_cleanup_incomplete(draft_id, exc)` and carries `draft_id`
and the error class name only — never `str(exc)` (the #789 leak class:
these messages embed file ids, sizes, or filesystem paths). It fires
before the pre-existing `_record_promotion_cleanup_failure` audit write,
mirroring the generic branch's log-then-audit order. Nothing else changes:
client responses, the `draft_promotion_cleanup_failed` security-audit
event, and the generic `DraftPromotionError` branch's `logger.exception`
(#907) are untouched, and clean typed failures (`cleanup_incomplete=False`)
remain unlogged per the module's 4xx-silent doctrine.

Tests: new class
`TestPromoteTypedCleanupIncompleteLogging` in
`backend/tests/draft_room/test_draft_promote.py` drives the real HTTP
route with the route module's `promote_input` binding patched to raise
each typed error with `cleanup_incomplete=True`:

- `test_duplicate_cleanup_incomplete_logs_one_error_record`
- `test_too_large_cleanup_incomplete_logs_one_error_record`
- `test_path_conflict_cleanup_incomplete_logs_one_error_record`
- `test_typed_clean_failures_log_no_error_record` (negative control,
  all three types with `cleanup_incomplete=False`)

Each positive test asserts one ERROR record carrying `draft_id` and the
class name, no exception text, an unchanged client response, and the
`draft_promotion_cleanup_failed` audit row (non-vacuity). The path-conflict
leg also re-pins the #1119 fixed client detail.

## Why

When a typed promotion failure's own compensation (deleting the
just-created `files`/`draft_promotions` rows and the copied bytes) also
fails, an orphan may genuinely exist. That was recorded only as a
`draft_promotion_cleanup_failed` row in `security_audit_log`; the shared
`draft_room_exception_handler` logs 5xx only, so these 4xx branches left
no record in the process log an operator tails — the orphan risk was
invisible without querying the audit table. One ERROR record per
occurrence closes that observability gap at LOW severity with no state
impact.

## Migration steps

None. Log-only change; no schema, config, API, or behavior change.
Operators tailing logs will start seeing records of the form:

```
ERROR app.api.routes.draft_room: draft_room: promotion cleanup incomplete draft_id=42 error=DraftPromotionDuplicateError
```

Treat any occurrence as an orphaned `files`/`draft_promotions` row or
on-disk file to reconcile (cross-reference the `draft_promotion_cleanup_failed`
audit event for the same `draft_id`).

## Known caveats

- The record is intentionally minimal (`draft_id` + class name): it cannot
  tell you *what* was orphaned. Use the audit event's metadata (which
  carries `detail`) and the `draft_id` for reconciliation.
- The generic `DraftPromotionError` cleanup-incomplete branch still logs
  via `logger.exception` (#907) and does not emit the new record — its
  500 response is already logged by the handler; deduplicating the two is
  out of scope (#916 out-of-scope list).
- `DraftPromotionPathConflictError` cleanup-incomplete occurrences also
  keep the pre-existing #1119 WARNING (raw `dest_path`, server-side only)
  in addition to the new ERROR record; the WARNING fires on every path
  conflict, the ERROR only when cleanup could not be verified.
