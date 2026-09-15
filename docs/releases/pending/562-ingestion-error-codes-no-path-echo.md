# 562 — User-facing ingestion error codes; server paths no longer echoed

**Issue:** #562 (Workstream J, PR 3 of 3; finding C26)
**Rebaseline:** 2026-09-11 · **Roadmap index:** #574

## Outcome

A failed ingestion now returns a stable user-facing error code plus a short operator-safe message
to any vault reader, and the documents API no longer echoes the server's absolute filesystem path.
Previously every parse/embedding failure persisted
`Failed to parse document '<absolute server path>': <raw Python exception text>` verbatim into
`files.error_message` and `files.phase_message` (and, after retries were exhausted, again through
the background worker), while `DocumentResponse.file_path` returned the stored absolute upload
path — all readable by any user with vault-`read` access (`GET /api/documents/`, document detail,
status polls). The raw exception text now stays in the server log only.

## What changed

- `backend/app/services/document_processor.py`: new redaction boundary — stable codes
  `PARSER_UNAVAILABLE` / `PARSE_FAILED` / `FILE_MISSING` with fixed content-free reasons
  (`classify_ingest_error` / `format_ingest_error` / `redact_ingest_error`), mirroring
  `document_extraction.py`'s shipped stable-code convention. Applied at every persist site: the
  `process_file` and `process_existing_file` exception handlers (both `error_message` and the
  `phase_message` written via `set_phase`), both `process_existing_file` missing-file pre-check
  branches, and the post-index enrichment failure path (`enrichment_error`). The `process_file`
  handler now also logs the raw exception (`logger.exception`) — previously it logged nothing, so
  the raw detail existed only on the wire.
- `backend/app/services/background_tasks.py`: the worker passes the caught exception (not
  `str(e)`) to `_handle_failure`; `_mark_task_permanently_failed` redacts exception payloads
  before persisting. Operator-constructed constant strings (e.g. `admission rejected: …`) remain
  trusted verbatim; the raw exception continues into the server-log line.
- `backend/app/api/routes/documents.py`: `DocumentResponse.file_path` is projected through
  `_vault_relative_file_path` (vault-relative when derivable, bare file name as fallback,
  idempotent for already-relative values) on the list, detail, and enrichment-toggle surfaces.
  The synchronous ingestion HTTP details that interpolated raw exceptions — upload (`Processing
  error` / `Server error` / `Upload failed`), whole-document retry, retry-chunks, directory scan,
  single/bulk delete, reindex-job creation, and the duplicate-file 409 (whose text carried the
  duplicate's stored server path) — now return fixed text; each handler's `logger.exception` (or
  a newly added one) keeps the detail server-side.
- `backend/tests/test_issue562_ingest_error_redaction.py` (new): end-to-end regression suite —
  real `DocumentProcessor` + SQLite, deterministic corrupt-parse failure (independent of the
  `unstructured` environment gap tracked as C30): redacted `error_message`/`phase_message`, raw
  text retained in the server log, both missing-file branches, worker persist redaction and
  trusted-string passthrough, and vault-relative `file_path` on the list/detail API.
- `backend/tests/test_issue562_guardrail_no_raw_ingest_error_persist.py` (new): structural
  guardrail census — no raw-exception/f-string interpolation into persisted error fields, an AST
  pin that the worker persists through `redact_ingest_error`, a response-layer pin that the raw
  stored `file_path` is never forwarded, and a pin of the shipped code set.
- `backend/tests/test_document_processor.py`: three tests that pinned the old raw persisted text
  now assert exact equality with the stable-code message (stricter); their raw-detail assertions
  moved to the raised-exception/server-log side where that detail now lives.
- `backend/security/bandit-baseline.json`: regenerated for line-shift churn (no new findings; no
  new suppressions).

## Deliberate deviation

The issue's example code set included `UNSUPPORTED_FORMAT`; it is intentionally not shipped. The
only raise site naming an unsupported extension (`SpreadsheetParser.parse`) is unreachable:
`_is_spreadsheet_file` gates dispatch on exactly `SPREADSHEET_EXTENSIONS` (`.csv/.xls/.xlsx`),
the same set the parser accepts, and the upload route rejects all other extensions before a row
exists. Shipping an unreachable code would be dead surface; if gating ever changes, the blanket
sanitizer still produces a safe `PARSE_FAILED` message. `FILE_MISSING` is added beyond the issue's
examples because the missing-file paths are real, reachable, and distinct failure families.

## Rollout and rollback

Behavior-narrowing only; response fields change shape (error code + short message instead of raw
exception; vault-relative instead of absolute `file_path`), which is the intended fix — any
external consumer that parsed the old raw `error_message` string or absolute `file_path` breaks
by design. No schema or migration: `error_message`/`phase_message` are free-text columns and
`file_path` remains absolute on disk and in the database; only the API projection changed. The
frontend consumes `error_message`/`phase_message` as plain text (compatible) and has zero
`file_path` consumers. Rollback: revert the commit — the raw text returns, with no data migration
in either direction. Note: pre-existing rows written before this change may still contain raw
text until the file is re-ingested; the response-layer projection removes the `file_path` echo
immediately for all rows.
