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
  now persist and publish `COMPILE_FAILED: <wiki|KMS> compile dispatch
  failed; see server logs` (the #562 ingest-code convention); the full
  exception text still reaches the server log via `logger.exception`.
  Retry/backoff/attempt-cap semantics are untouched — only the string
  payload changes, so the settled value composes as
  `attempt_cap_exceeded: COMPILE_FAILED: ...`.
- **`job_cancelled` is terminal for the UI.** `useWikiEventStream`
  dispatches its terminal callback for `job_cancelled` alongside
  `job_completed`/`job_failed`, matching the processor's published
  vocabulary and the documented "refetch on any terminal job event"
  contract.
- **Malformed job rows degrade everywhere.** Both
  `_compile_job_from_jobs_row` implementations guard the payload parse with
  the sibling `except (json.JSONDecodeError, TypeError)` pattern, and all
  11 lease-mode `json_extract(payload_json, ...)` filters (7 wiki, 4 KMS:
  list/get/cancel/retry scoping) wrap the extraction in
  `CASE WHEN json_valid(payload_json) THEN ... ELSE NULL END`, so a
  malformed row is excluded from vault views and UPDATEs scoped to it
  safely match zero rows instead of raising.
- **Guardrail census ships.** `test_o02_guardrail_census.py` pins the
  stable-code constants, the fail-path call shapes, the cap-prefix
  composition, the per-file `json_extract` site counts with the
  `json_valid` guard requirement, and the guarded payload parses —
  demonstrated failing on the pre-fix tree for every family the fix
  changes.

## Migration

None. No schema, settings, or API shape changes; `jobs.error` content
changes from raw text to stable codes.

## Known caveats

- Operators lose the raw message in the jobs API and must consult server
  logs for failure detail (the issue's stated trade).
- Exception class names are not part of the stable code.
- The ingest queue's `enqueue_deduped` carries the same unguarded
  `json_extract` pattern in `job_lease.py`; it is out of this issue's file
  scope and tracked in a filed follow-up issue.
