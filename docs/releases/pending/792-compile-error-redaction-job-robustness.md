# fix(wiki,kms): compile job failures stop leaking raw exception text and malformed job rows stop crashing readers (#792)

## What changed

A failing wiki or KMS compile dispatch persisted `str(exc)` verbatim into
`jobs.error` (capped at 8000/2000 chars), which `GET /wiki/jobs` and
`GET /kms/jobs` return to any vault-read user and the wiki jobs panel
renders — an information-disclosure gap (paths, DB errors, upstream payload
fragments could all ride the string). The same raw text fanned out on the
`job_failed` wiki SSE event. Three adjacent robustness gaps sat on the same
surface: the frontend never refreshed on a `job_cancelled` SSE event, both
stores parsed `payload_json` with an unguarded `json.loads` (one malformed
row raised out of every job reader), and the lease-mode vault filters
embedded `json_extract(payload_json, ...)` in SQL, where SQLite itself
raises `OperationalError: malformed JSON` before any Python guard runs.

- **Stable error codes at the reader boundary.** Both compile processors
  now persist `COMPILE_FAILED: <wiki|KMS> compile dispatch failed; see
  server logs` (the #562 ingest-code convention) instead of `str(exc)`, and
  the wiki processor publishes that code on the `job_failed` event (KMS has
  no SSE stream). The full exception text still reaches the server log via
  `logger.exception`. Retry/backoff/attempt-cap semantics are untouched —
  only the string payload changes, so the settled value composes as
  `attempt_cap_exceeded: COMPILE_FAILED: ...`.
- **Corrupt-payload rows settle failed, never silently succeed.** A claimed
  job whose `payload_json` is unparseable, valid JSON but not an object, or
  too deeply nested to parse degrades at the store read and is settled
  terminally failed at claim with `PAYLOAD_MALFORMED: compile job payload
  is corrupt; see server logs` (wiki also publishes `job_failed`), instead
  of crashing the poll loop or — worse — dispatching as a vault-less
  "manual" job that would complete as a fabricated success.
- **`job_cancelled` is terminal for the UI.** `useWikiEventStream`
  dispatches its terminal callback for `job_cancelled` alongside
  `job_completed`/`job_failed`, matching the processor's published
  vocabulary and the refetch-on-terminal-event behavior documented at the
  `WikiPage` handler.
- **Malformed job rows degrade everywhere they are read.** Both
  `_compile_job_from_jobs_row` implementations guard the payload parse
  (including non-object and recursion-overflow shapes), and all 11
  lease-mode `json_extract(payload_json, ...)` filters (7 wiki, 4 KMS:
  list/get/cancel/retry scoping) wrap the extraction in
  `CASE WHEN json_valid(payload_json) THEN ... ELSE NULL END`, so a
  malformed row is excluded from vault views and UPDATEs scoped to it
  safely match zero rows instead of raising.
- **Guardrail census ships.** `test_o02_guardrail_census.py` pins the
  stable-code constants (call shapes matched for direct and executor
  calls), the `attempt_cap_exceeded: COMPILE_FAILED` cap composition, the
  per-file `json_extract` site counts with line-level THEN-guard pairing,
  and the widened parse guards;
  `test_o02b_feedback_pins.py` adds the positive contracts (exact codes at
  the reader boundary, the corrupt-payload terminal-fail disposition,
  valid-flow completion, requeue-branch persistence, legacy-mode
  redaction, and behavioral malformed-row coverage for the remaining
  reader paths).

## Migration

No schema or settings changes. `jobs.error` content changes from raw text
to stable codes for failures written after this deploy. Rows written
before it are MASKED AT READ TIME for vault readers by #953's PR
(read-time mapping, not a scrub migration) — see that PR's release
fragment for the at-rest posture and the `REDACTED_LEGACY_ERROR` marker.

## Known caveats

- Operators lose the raw message in the jobs API and must consult server
  logs for failure detail (the issue's stated trade).
- Exception class names are not part of the stable code.
- Rows written before this deploy no longer expose their stored error text
  to vault readers: #953's PR masks it at read time (no scrub migration).
- The panel and KMS toast render the stable code verbatim (no
  code-to-prose mapping); the previous raw text went through the same
  sinks.
- The ingest queue's `enqueue_deduped` and legacy-sync anti-join carry the
  same unguarded `json_extract` pattern outside this issue's file scope,
  tracked in #950 and #954.
- `backend/security/bandit-baseline.json` was regenerated for line-shift
  churn only (no new findings; finding multiset unchanged).
