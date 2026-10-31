# Log one detail line per upload 400 rejection (#1041)

Issue: #1041 (Workstream F follow-up to #730; companion to #768, which
tracks the client-side accept filter).

## What changed

`_do_upload` in `backend/app/api/routes/documents.py` now emits exactly
one WARNING line per rejected upload via a new helper,
`_log_upload_rejection(reason, filename, vault_id, user)`, immediately
before each of its three 400 raises:

- "No file provided" (no file part),
- "Filename cannot be empty" (falsy filename),
- "File extension 'X' not allowed" (suffix outside
  `Settings.allowed_extensions`).

The line reads
`Upload rejected: <reason>; filename=<repr> vault_id=<id> user_id=<id>`
(user id via the existing `_user_id_str` helper, so it renders
`unknown` when the dict is absent). To carry the user id, `_do_upload`
gained an optional trailing `user: dict | None = None` parameter and the
two route handlers that call it (`upload_document_root`,
`upload_document`) pass their `require_vault_permission("write")` user
through.

Nothing else changed: validation rules, the extension allowlist, the
response bodies, and the status codes are byte-identical, and no log
line was added on any success path.

Regression tests:
`backend/tests/test_1041_upload_rejection_logging.py` — one behavioral
check per rejection path (exactly one WARNING record carrying reason,
filename where present, vault id, user id; HTTPException status/detail
pinned to the existing strings) plus an AST guard that
`_log_upload_rejection` is called exactly three times, all inside
`_do_upload`, each with the four metadata arguments only.

## Why

Deployment evidence (2026-10-28, issue #1041): a member batch-uploaded
29 files; 10 were 400-rejected in 26-47 ms each and the server-side log
had no detail — the reason existed only in the HTTP response body the
operator never saw, so empty-filename could not be distinguished from a
disallowed extension (the "legacy CHCS-era extensions" attribution
remained inference). FastAPI does not auto-log `HTTPException` details,
so the only trace was the middleware `http_request` status line
(`backend/app/middleware/logging.py`). One log line per rejection makes
batch-upload 400s diagnosable from logs alone.

## Migration steps

None. No config, schema, API, or behavior change; deployments get the
new lines with the next image.

## Known caveats

- The lines carry filenames and ids only — never file contents — per
  the #287/#710/#730 default-log redaction contract, enforced unchanged
  by `backend/tests/test_no_query_text_in_default_logs.py` (its Guard A
  queryish-name scan stays green with the new call). Filenames are
  user-supplied strings and appear verbatim (repr-quoted) in the log;
  this matches existing practice (e.g. the file-scoped
  `logger.exception("Error uploading file: %s", ...)` lines).
- The rejection reason logged for the extension path omits the
  `Allowed: {...}` suffix the response body carries; the allowlist is
  static config (`Settings.allowed_extensions`), not per-request state.
- The second "Filename cannot be empty" raise — in
  `validation_exception_handler` (`backend/app/api/routes/documents.py`,
  the request-validation fallback) — is intentionally out of scope per
  the issue's investigation comment.
- Rejections that bypass these three paths (413 size, "Invalid file
  path") remain unlogged, as before — they were not in this issue's
  scope.
