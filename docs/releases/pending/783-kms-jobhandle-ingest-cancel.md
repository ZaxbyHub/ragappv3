# Workstream M PR 3: KMS recompile job-handle wiring + document ingest cancel + shared job-family contract (Issue #783)

## What changed

### Backend

- **`backend/app/api/routes/documents.py`** — new
  `POST /api/documents/{file_id}/cancel`: per-file vault-admin auth
  (delete-route parity) + CSRF + admin rate limit. `indexed`/`partial`/
  `error` rows are terminal (409, data untouched — retry owns error
  recovery); `pending`/`processing` rows get the synchronous
  `request_ingest_cancel` (registry + queued-item cancellation) and a
  guarded `UPDATE ... WHERE status IN ('pending','processing')` flip to the
  new terminal `cancelled`; rowcount 0 re-reads for the concurrent
  double-cancel (idempotent 200) vs. completed race (409). HMAC audit rows
  recorded on every decision.
- **`backend/app/services/document_processor.py`** — the cancel-request
  registry (`request_cancel`/`clear_cancel`/`is_cancel_requested`) and
  `IngestCancelledError`; between-steps gates in BOTH ingest paths
  (after parse/chunking before embedding, and immediately before the
  vector write — the same seam as the issue-#692 staleness gate);
  `except IngestCancelledError: raise` carve-outs before both generic
  handlers (a cancellation is never `status='error'`); the
  error-raced-a-cancel branch in both handlers; the idempotent
  `rollback_cancelled_ingest` (guarded status write + vector/atom cleanup
  + terminal phase); and the finalize guard that unwinds a generation when
  a cancel lands after the vector write (no orphans on a `cancelled` row).
- **`backend/app/services/background_tasks.py`** — synchronous
  `request_ingest_cancel` (registry + existing #516 queued-cancel);
  `_handle_cancellation` on the legacy transport; the lease transport
  settles cancelled ingestion jobs terminally (`cancel`, never requeue);
  every enqueue clears a stale registry entry (retry-after-cancel works).
- **`backend/app/services/job_lease.py`** — fenced `cancel` settle
  (mirrors `fail`, lands `jobs.status='cancelled'`).
- **`backend/app/models/database.py`** — `files.status` CHECK gains
  `'cancelled'` (schema constant + `migrate_add_files_status_cancelled`
  rebuild mirroring `migrate_widen_files_status` with the current full
  column set including `extraction_diagnostics`; registered after
  `migrate_add_files_extraction_diagnostics` and before the
  `_widen_files_hash_vault_unique_index` block, which stays the sole
  recreator of that partial unique index).
- **`backend/app/services/document_progress.py`** — `PHASE_CANCELLED` and
  keyword-only `phase`/`phase_message` overrides on `clear_progress`
  (defaults preserve the historical indexed+cleared behavior).

### Frontend

- **`frontend/src/hooks/useJobStatus.ts` (new)** — the shared job-family
  contract: `JOB_FAMILIES` (ingest, wiki, draft-room, kms, reindex),
  `TERMINAL_JOB_STATUSES`, per-family poll defaults, and a bounded,
  ref-tracked, unmount-safe polling hook parameterized by family. PR 4's
  Activity tray (#784) consumes the same module.
- **`frontend/src/pages/KMSPage.tsx`** — Recompile keeps the
  `{job_id, status}` handle and polls through `listKMSJobs` (the audit's
  unwired export — RV-FU158-01) until terminal, then refetches entries and
  toasts the real outcome; the unconditional "will refresh shortly" toast
  is removed; the button disables while a poll is live; the api client is
  dereferenced only inside the poll-time fetcher.
- **`frontend/src/components/shared/StatusBadge.tsx`** + **`core.ts`** —
  the `cancelled` chip and an accurate full status-enum doc comment.

### Guardrail

- **`frontend/src/lib/api/kms.unused-export.guardrail.test.ts` (new)** —
  the missing unused-export check the audit named: every KMS-module
  function export needs a non-test importer or a dated allowlist entry
  (`compileDocumentKMS`, `searchKMS`), and a wired allowlist entry fails
  the test so the list cannot rot.

## Why

KMS recompile already returned a job handle and served job-status routes,
but the frontend discarded the handle and toasted an unconditional
"will refresh shortly" — a failed or slow compile was indistinguishable
from success (`listKMSJobs` had zero non-test callers: unwired code by the
repo's own non-negotiables). Separately, document ingest had retry but no
cancel; the client-side cancel only covered pre-parse uploads, and the
Activity center (#784) needs a real cancel path for the ingest family.
The 5-family contract exists so this PR's KMS polling and PR 4's tray
share one module instead of growing per-page copies.

## Verification

- Frozen acceptance checks C1-C9 (trace 783-kms-jobhandle-ingest-cancel):
  C1/C2 RED→GREEN (KMSPage polling + terminal refetch/toast), C3-C6
  ERROR→GREEN (cancel route, mid-ingest unwind with no orphan atoms or
  vectors, 409-on-indexed with data kept, the 5-family contract), C7-C9
  GREEN→GREEN (KMS compile/job round trip, superadmin retry, stale-search
  guard).
- Companions: worker unwind leaves `files.status='cancelled'` + truthful
  terminal phase; route 404/409-error/idempotent-200/403 branches; the
  scan/sync path's gate; finalize-guard post-write cleanup; registry
  clearing on re-enqueue; the destructive re-ingest semantic; migration
  convergence (extraction_diagnostics/folder_id/parsed_text survive,
  indexes + partial unique recreated, idempotent).
