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
(user id via the existing `_user_id_str` helper, which renders `unknown`
for a missing user **or** for a falsy id — so a `users_enabled=false`
deployment, whose admin-secret principal carries `id: 0`, reads
`user_id=unknown` on every line. That helper is pre-existing and already
governs `document_actions.user_id`, so the new line is consistent with
the audit trail rather than introducing a new attribution gap). To carry
the user id, `_do_upload`
gained an optional trailing `user: dict | None = None` parameter and the
two route handlers that call it (`upload_document_root`,
`upload_document`) pass their `require_vault_permission("write")` user
through.

Nothing else changed: validation rules, the extension allowlist, the
response bodies, and the status codes are byte-identical, and no log
line was added on any success path.

Regression tests:
`backend/tests/test_1041_upload_rejection_logging.py` — one behavioral
check per rejection path (exactly one record at INFO or WARNING carrying
reason, filename where present, vault id, user id; HTTPException
status/detail pinned to the existing strings), a route-level check that
`upload_document_root` threads its `require_vault_permission("write")`
principal into the line, an AST guard that `_log_upload_rejection` is
called exactly three times all inside `_do_upload` with metadata-only
arguments in **every** slot plus a pin on `_do_upload`'s total `logger.*`
site count (AC2's "no new per-success line" half), and planted-violation
pins proving each of those guards fails on a tree that violates it.

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
  the #287/#710/#730 default-log redaction contract. That clause is
  enforced by the AST guard in
  `backend/tests/test_1041_upload_rejection_logging.py`, which walks
  **every** argument of every `_log_upload_rejection` call for content
  expressions; `backend/tests/test_no_query_text_in_default_logs.py`
  stays untouched and green, but its Guard A enumerates query-derived
  names only and is not a substitute for the content clause. Filenames
  are user-supplied strings and appear verbatim (repr-quoted) in the log
  — deliberately, because AC1 asks for "the submitted filename". Note
  that this differs from the file's other filename log sites
  (`logger.exception("Error uploading file: %s", ...)` and friends),
  which log the `secure_filename`-sanitized `file_name`; the raw form is
  what the operator needs to match a batch-upload rejection back to the
  file the member actually selected.
- The rejection reason logged for the extension path omits the
  `Allowed: {...}` suffix the response body carries; the allowlist is
  static config (`Settings.allowed_extensions`), not per-request state.
- The second "Filename cannot be empty" raise — in
  `validation_exception_handler` (`backend/app/api/routes/documents.py`,
  the request-validation fallback) — is intentionally out of scope per
  the issue's investigation comment.
- Rejections that bypass these three paths remain unlogged, as before —
  they were not in this issue's scope. The full set raised by
  `_do_upload` today: `Invalid file path` (two sites), `File is empty`,
  `File content does not match the declared extension`, `Image header
  does not match the declared extension`, `File is not a valid <ext>
  archive`, `File is not a valid <ext> image`, both 413 `File too large`
  sites, and the 409 duplicate-hash raise. The extension check that this
  change does cover is the one the 2026-10-28 incident actually hit;
  the content/format validators above are the likeliest next candidates
  if the operator triage loop still cannot attribute a rejection.
