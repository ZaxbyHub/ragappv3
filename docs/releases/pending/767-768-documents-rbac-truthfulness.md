# Documents-surface RBAC gates and truthful counts, copy, and upload limits (Issues #767, #768)

## What changed

### Document-organisation UI gates now match backend RBAC (`frontend/src/pages/DocumentsPage.tsx`)

- The organise capability (folder create/rename/move/delete, bulk Tag/Move, row selection,
  wiki Compile/Recompile) is gated on vault **write**, matching what the routes actually
  enforce (`folders.py`, `tags.py`, `wiki.py` all call `_require_vault_write`). A vault writer
  sees the organisation controls the API already allowed them; previously these demanded vault
  admin client-side.
- The destructive capability (per-row Delete, Delete Selected, Delete All in Vault) stays
  gated on vault **admin**, matching the per-file vault-admin delete routes
  (`documents.py` delete/batch/delete-all).
- The detail page's tag Save is enabled only for write/admin on the document's vault
  (fail-closed when the vault or permission is unknown); the wiki Compile/Recompile cells are
  likewise write-gated — read-only viewers no longer get enabled controls whose only feedback
  is a 403 toast.
- **Scan Directory** is gated on the user's GLOBAL role (admin/superadmin), mirroring the
  route's `require_admin_role` dependency; the client no longer sends the `vault_id` parameter
  the route silently ignored. The server route itself is unchanged.

### The scan response stops misreporting what it did (`backend/app/services/file_watcher.py`, `backend/app/api/routes/documents.py`)

- `scan_once` returns a `ScanResult(files_examined, files_enqueued, errors)` and
  `_find_new_files` a `DirectoryScan(examined, new_files)`, so the scan endpoint reports
  `scanned = files_examined` (files actually examined) instead of aliasing the enqueued count.
  A scan that examined hundreds of already-known files no longer reads as "0 scanned".
  Per-directory failures are returned in
  `errors` — vault-relative scope plus exception class only, never absolute
  paths or raw exception text (the #562 no-path-echo direction) — and the
  scan toast surfaces them to the operator instead of living only in logs.

### Documents-surface counts and copy say what actually happened (`frontend/src/pages/DocumentsPage.tsx`)

- The header count badge shows the matching server total with the loaded-window floor (the
  floor keeps the badge sane during the optimistic-delete window before the refetch lands;
  issue #768 AC1).
- Bulk delete and Delete All now refetch document stats, so the Chunks/Size/Indexed cards
  agree with the server after destructive actions (previously only `total_documents` was
  hand-patched locally).
- A tag/folder filter with zero matches in a populated vault renders "No documents match your
  filters" with a Clear-filters action, instead of falling through to "No documents yet".
- The single-document delete confirmation names the document, and its destructive button is
  labelled "Delete" (per-dialog `ConfirmDialogState.confirmLabel`; every other confirmation
  dialog keeps the "Confirm" default).

### Server upload limits reach the upload surfaces (`frontend/src/components/layout/PageShell.tsx`, `UploadDropzone.tsx`, `lib/uploadLimits.ts`, `stores/useUploadStore.ts`)

- The authenticated shell's existing settings fetch now also populates the settings store
  (fill-only: it never overwrites a store the Settings page already loaded/saved), so the
  server's `max_file_size_mb` and `allowed_extensions` reach upload surfaces without visiting
  Settings.
- The dropzone advertises the server's extension allowlist and enforces it client-side with a
  predicate that mirrors the server's filename pipeline step for step (secure_filename rules,
  Python-`Path.suffix` semantics, the missing-suffix `.txt` fallback, lowercase membership).
  The react-dropzone `accept` option is advisory UX only (a single valid MIME key carrying the
  extension list); acceptance truth is the predicate on both drop paths — a type-only
  attribute rejection the server would accept (extensionless names) is recovered, and an
  attribute acceptance the server would reject (octet-stream-typed files) is refused.
- A 413 upload failure now names the server-configured limit instead of the hardcoded 100 MB
  fallback.
- The disabled dropzone dims only its icon affordance; the instructions keep full contrast
  (previously `opacity-60` on the whole card dropped them below WCAG AA).

## Caveats

- The client acceptance predicate now falls back to `.txt` exactly as the server does when a
  filename sanitizes to empty (the server renames it to `unnamed_file.txt` and accepts it
  iff `.txt` is allowed) — the earlier "refused client-side" divergence is closed. Membership
  is tested against the RAW configured allowlist entries, exactly as the server does (an
  uppercase or dotless configured entry behaves identically on both sides).

- Before settings load (failed fetch or first paint), the dropzone keeps the pre-existing
  behavior: hardcoded copy, the 100 MB fallback limit, and no client-side extension filter —
  the server's own validation is the guard, as before.
- The Scan Directory gate mirrors the route's global-admin requirement; making the scan itself
  vault-scoped is server work outside this change (the route still scans every vault's upload
  directory for global admins, as before).

## Verification

- 23 frozen acceptance checks (issues #767/#768): 16 DISCRIMINATING RED→GREEN, 7 PRESERVING
  GREEN→GREEN, replayed at the fix head (`replay-all.sh`).
- New: `backend/tests/test_k04_scan_mixed.py` (mixed/rejected/partial-failure scan counts),
  `UploadDropzone.accept-contract.test.tsx` (exact server-pipeline acceptance matrix incl.
  `.htm`/`.bin`/extensionless/dotfile/trailing-dot/multi-dot/case/sanitize edges, recovery
  exact-once, no mount warnings), `DocumentsPage.k04.matrix.test.tsx` (bidirectional RBAC
  matrix + dialog labels + filter empty-state copy/clear), `PageShell.k05.integration.test.tsx`
  (integrated settings path, deferred-response race, failure path, unmount).
- Bandit baseline: the regeneration kept the finding identity multiset identical (125 = 125);
  non-gating `metrics.loc` counters refreshed for files whose line counts moved since the
  previous anchor (including files this PR does not touch).
- Upload-limit reach: the settings-store fill covers the Documents upload surface (the
  dropzone and its effective-limit consumers); the Composer and Draft Room attach paths keep
  their own size-only guards and remain out of this change's scope.
- Updated contracts: watcher suites moved to the `ScanResult` field access
  (adversarial/library-config/n02/b04-real-rows); DocumentsPage suites mock the auth store for
  the new global-role gate; detail-page suites seed write permission; the K-PR3 `Confirm`
  selector updated to `Delete` per #768 AC6.
