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
  `uploadSizeExceededMessage` against `settings.max_file_size_mb` (with the
  same positivity guard as UploadDropzone: a non-positive server value falls
  back to the 100 MB client default) and `serverAcceptsFilename` against the
  allowlist minus raster images.
- **Draft Room surface allowlist** (`frontend/src/lib/uploadLimits.ts`): new
  `draftRoomAllowedExtensions()` (+ exported `RASTER_IMAGE_EXTENSIONS`) mirrors
  backend `DRAFT_ROOM_ALLOWED_EXTENSIONS` (`backend/app/api/routes/draft_room.py`):
  `settings.allowed_extensions` minus the raster image set (`RASTER_IMAGE_EXTENSIONS`,
  `backend/app/services/document_artifacts.py`) — Draft Room is a parse-only
  text seam (issue #460) and does not accept standalone images even when the
  vault allowlist includes them.

While settings have not loaded (`settings.allowed_extensions === undefined`),
both surfaces keep today's behavior and let the server validate — the same
`allowedExtensionsLoaded` gate UploadDropzone uses. Both surfaces report each
rejected file individually and still enqueue the acceptable files of a mixed
batch.

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
  constant set: if the backend `RASTER_IMAGE_EXTENSIONS` set ever changes,
  `uploadLimits.RASTER_IMAGE_EXTENSIONS` must change with it (same mirror
  discipline as `serverAcceptsFilename` vs the server's filename pipeline).
- `serverAcceptsFilename` membership semantics follow the normalized-entry
  form UploadDropzone established; a server configured with uppercase/dotless
  allowlist entries is outside every client surface's sane-config contract.
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
  unit contract (subtraction, normalization, order, empty set).

## Invariant audit (evidence for the PR body)

- Client/server upload-policy parity (PR #1048's contract, extended to all
  three surfaces): Composer and Draft Room now consult the shared
  `serverAcceptsFilename` predicate; Draft Room size guards use
  `isUploadTooLarge`/`uploadSizeExceededMessage` with the server-configured
  limit; `grep -c serverAcceptsFilename frontend/src/components/chat
  frontend/src/components/draft-room` is non-zero in both (AC3).
- Server remains source of truth: no backend validation changed; every guard
  added client-side mirrors an existing server check (`documents.py` extension
  check, `draft_room.py` `DRAFT_ROOM_ALLOWED_EXTENSIONS` + `max_file_size_mb`
  staging limits); unloaded settings defer to server validation.
- No behavior change for accepted files or for the Documents dropzone (its
  tests pass unchanged).
