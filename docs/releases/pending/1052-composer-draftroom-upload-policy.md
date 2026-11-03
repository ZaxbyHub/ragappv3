# Composer and Draft Room attach paths enforce the server-configured upload policy (#1052)

Issue: #1052 (follow-up from PR #1048 — the two upload surfaces the PR's scope
boundary left on client-side-only guards).

## What changed

The chat Composer attach path and the Draft Room source-upload path now enforce
the same server-configured upload policy the Documents dropzone learned in PR
#1048, reusing the shared `frontend/src/lib/uploadLimits.ts` helpers so the
client decision cannot drift from the server pipeline again:

- **Chat Composer** (`frontend/src/components/chat/Composer.tsx`): `enqueueFiles`
  filters every attach path (file picker, drag-and-drop, file paste, and the
  large-text paste-to-file conversion) through `serverAcceptsFilename` using
  `settings.allowed_extensions` from the settings store BEFORE calling
  `addUploads`. A disallowed extension is rejected client-side with the
  Documents dropzone's per-file message shape — `"<name> (file type not
  allowed)"` — instead of enqueuing a transfer the server would 400 after the
  bytes are uploaded. The size half was already server-configured (the shared
  upload store guards via `effectiveUploadLimitMb()`); only the extension half
  was missing.
- **Draft Room** (`frontend/src/components/draft-room/DraftSourceUpload.tsx`):
  `addFiles` — the single chokepoint shared by the native file input and the
  dropzone drop path — previously had neither a size guard nor an extension
  filter. It now rejects at attach time using `isUploadTooLarge` /
  `uploadSizeExceededMessage` against `settings.max_file_size_mb` resolved
  through the SAME shared rule the Composer/upload store uses
  (`uploadLimits.resolveUploadLimitMb`, read at call time), and `serverAcceptsFilename`
  against the allowlist minus raster images.
- **Draft Room has no missing-suffix fallback** (review PRR-002): the two
  server pipelines differ here, so the shared predicate is now parameterized.
  Documents maps an extensionless name to `<name>.txt` and accepts it iff
  `.txt` is allowed, while `draft_input_storage.stage_upload` computes
  `Path(secure_filename(name)).suffix.lower()` with no fallback — so `README`,
  `Makefile` and dotfiles are 415 `unsupported_input` there even on the
  default allowlist. `serverAcceptsFilename` takes
  `{ missingSuffixFallback }`; the Composer and Documents surfaces keep the
  `.txt` default and Draft Room passes `null`. Without this the client called
  those names acceptable, transferred the whole body, and then got the 415 —
  the late-rejection class this release note is about.
- **Large-text paste no longer claims a false success** (review PRR-001):
  `enqueueFiles` now returns the accepted count, and `handlePaste` calls
  `preventDefault()` and shows "Pasted text attached as a file" only when the
  conversion was actually enqueued. Previously it consumed the paste gesture
  first and toasted success unconditionally, so a deployment whose allowlist
  omits `.txt` saw a rejection toast and a success toast for one paste while
  the text was never inserted inline.
- **Draft Room surface allowlist** (`frontend/src/lib/uploadLimits.ts`): new
  `draftRoomAllowedExtensions()` (+ exported `RASTER_IMAGE_EXTENSIONS`) mirrors
  backend `DRAFT_ROOM_ALLOWED_EXTENSIONS` (`backend/app/api/routes/draft_room.py`):
  `settings.allowed_extensions` minus the raster image set (`RASTER_IMAGE_EXTENSIONS`,
  `backend/app/services/document_artifacts.py`) — Draft Room is a parse-only
  text seam (issue #460) and does not accept standalone images even when the
  vault allowlist includes them.

While settings have not loaded (`settings.allowed_extensions === undefined`),
both surfaces skip the **extension** check and let the server validate — the
same `allowedExtensionsLoaded` gate UploadDropzone uses. The **size** half does
not defer: with no settings loaded `effectiveUploadLimitMb()` yields the 100 MB
fallback, so a file over 100 MB is rejected at attach time and the toast names
"Max size: 100 MB." — a number the operator may not have configured. This is
the upload store's pre-existing behavior for the Composer half, deliberately
shared rather than re-derived per surface (see the caveat below). Both surfaces
report each rejected file individually and still enqueue the acceptable files
of a mixed batch.

## Why

PR #1048 review (K-22/K-08 findings): the same server configuration produced
different client behavior per upload surface. A disallowed extension attached
in the Composer or Draft Room produced a wasted upload and a late server 400
(or 413), while the same file on the Documents dropzone was rejected instantly
client-side. The Draft Room had no client-side guard at all: any size, any
extension, straight to the wire.

## Migration steps

None. No API, schema, or configuration changes — the client now mirrors the
existing server policy. Users will simply see disallowed/oversized files
rejected at attach time on these two surfaces, with the server-configured
limit number in the message.

## Known caveats

- The Draft Room raster-image exclusion is a client mirror of the backend
  constant set: if the backend `RASTER_IMAGE_EXTENSIONS`
  (`backend/app/services/document_artifacts.py`) ever changes,
  `uploadLimits.RASTER_IMAGE_EXTENSIONS` must change with it. Review PRR-005
  found the original guard for this was tautological — the test built its
  expected value from the same constant under test, so mutating `.webp` to
  `.svg` kept it green. It now asserts against a literal copy of the backend
  set, so a drift fails CI. `scripts/check_config_contract.py` still covers
  `max_file_size_mb` only; there is no automated backend↔frontend cross-check
  beyond that test.
- `serverAcceptsFilename` tests membership against the entries **exactly as
  the caller supplies them**. Every surface pre-normalizes (following
  UploadDropzone's precedent), so a server configured with uppercase/dotless
  allowlist entries is accepted client-side and rejected by the server, which
  compares the lowercased suffix against the RAW `settings.allowed_extensions`.
  `allowed_extensions` is env-only and not mutable through the settings API, so
  this is an operator-misconfiguration case, disclosed rather than silently
  claimed as parity (review PRR-003; the predicate's own docstring previously
  asserted raw-entry parity and has been corrected).
- With settings unloaded, the size half fail-closes at the 100 MB fallback
  while the extension half defers to the server — the two halves of one gate
  degrade in opposite directions. Accepted deliberately: the fallback rule is
  the upload store's pre-existing one, factored into
  `uploadLimits.resolveUploadLimitMb` so every surface resolves one server
  value through a single implementation instead of re-deriving it (review
  PRR-004). A server-reported `max_file_size_mb: 0` is now applied literally
  (rejects everything) instead of being masked by the fallback, matching the
  server.
- Rejections surface as sonner toasts on both paths (consistent with the
  upload store's size-rejection toasts), not the Documents page's
  `RejectedFilesBanner` widget — the message shape is shared, the surface
  presentation follows each surface's existing pattern.

## Tests

- `frontend/src/components/chat/Composer.issue1052.test.tsx` — AC1: rejects a
  disallowed extension before enqueue with the dropzone message shape
  (addUploads/attachToChat never called); mixed batches enqueue only allowed
  files; unloaded settings keep server-side validation.
- `frontend/src/components/draft-room/DraftSourceUpload.issue1052.test.tsx` —
  AC2: size guard against the server-configured `max_file_size_mb` (a 3 MB file
  under a 2 MB server limit is rejected at attach with "Max size: 2 MB."),
  extension rejection, raster-image exclusion on the Draft Room surface,
  inclusive boundary at exactly the limit, mixed-batch partial acceptance, and
  the pre-load passthrough.
- `frontend/src/lib/uploadLimits.issue1052.test.ts` — `draftRoomAllowedExtensions`
  unit contract (subtraction, normalization, order, empty set), the
  `missingSuffixFallback` mode for both server pipelines (PRR-002), and a
  `RASTER_IMAGE_EXTENSIONS` sync check against a literal copy of the backend
  set so a mirror drift fails CI (PRR-005 — the original check derived its
  expected value from the constant under test and was mutation-proven unable
  to fail).
- Review-round regressions (swarm-pr-review run `pr1053-20261102`):
  `Composer.issue1052.test.tsx` gains the large-text paste cases — a decline
  must neither call `preventDefault()` nor fire the success toast (PRR-001) —
  and `DraftSourceUpload.issue1052.test.tsx` gains the extensionless-name
  rejection and the literal `max_file_size_mb: 0` guard (PRR-002 / PRR-004).
  Each was verified to fail against the pre-fix code.

## Invariant audit (evidence for the PR body)

- Client/server upload-policy parity (PR #1048's contract, extended to all
  three surfaces): Composer and Draft Room now consult the shared
  `serverAcceptsFilename` predicate; Draft Room size guards use
  `isUploadTooLarge`/`uploadSizeExceededMessage` with the server-configured
  limit; `grep -c serverAcceptsFilename frontend/src/components/chat
  frontend/src/components/draft-room` is non-zero in both (AC3).
- Server remains source of truth: no backend validation changed; every guard
  added client-side mirrors an existing server check for **its own** surface
  (`documents.py` extension check including the `.txt` fallback for the
  Composer/Documents surfaces; `draft_input_storage.py`'s no-fallback suffix
  check + `draft_room.py` `DRAFT_ROOM_ALLOWED_EXTENSIONS` for Draft Room;
  `max_file_size_mb` staging limits on both). The earlier blanket wording
  here overclaimed uniform mirroring — Draft Room needed its own
  no-fallback mode to be faithful at all (review PRR-002). Unloaded settings
  defer to server validation for extensions only; the size half uses the
  shared fallback (see the caveat above).
- The Draft Room **cumulative** per-draft input cap
  (`max_total_input_bytes`, enforced by `draft_store.reserve_input`) is still
  not mirrored client-side — the component never receives current total bytes.
  A batch that passes every per-file check and then exceeds the total is
  uploaded and rejected 413 `limit_exceeded` by the server. Out of this
  issue's scope (AC2 names `max_file_size_mb` only) and recorded here so the
  remaining late-rejection surface is not mistaken for closed.
- No behavior change for accepted files or for the Documents dropzone (its
  tests pass unchanged).
