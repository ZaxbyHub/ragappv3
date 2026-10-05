# 784: Shell-level Activity tray — every background job in one server-sourced view

Issue: #784 ([Workstream M] PR 4 of 4; proposal UI-ENH-11 stage 2 of 2).

## What changed

- **New shell surface**: `PageShell` now mounts an `ActivityTray` next to the
  upload indicator on every authenticated route — one `role="region"`
  landmark named "Activity" listing the running and recent background jobs
  of all five families (ingest, wiki, draft-room, kms, reindex). Rows are
  visible by default; the "Activity" button expands (never collapses — a
  click made while the first poll is in flight must not hide the rows it
  is about to deliver); a separate Hide control collapses an expanded list.
- **Server-sourced, reload-safe**: the tray's data comes from the new
  `useActivityJobs` aggregation export in `@/hooks/useJobStatus` (the #783
  contract module). It polls the five family adapters on one bounded
  setTimeout chain (~8s, never setInterval); every family failure —
  including the legitimate 403s for feature/admin-gated families — degrades
  to zero rows for that tick. The ingest family reads the document list
  (`pending` + `processing`, deduplicated by file id) through the
  `documentProgress` phase vocabulary; it never reads the client upload
  store, so a reload or another tab loses nothing.
- **Actions**: Cancel on wiki / draft-room / ingest rows calls
  `cancelWikiJob` / `cancelDraftJob` / the new `cancelDocumentIngest`
  client (issue #783's `POST /documents/{file_id}/cancel` endpoint finally
  has a frontend caller); Retry on failed wiki / draft-room rows calls
  `retryWikiJob` / `retryDraftJob`. Failed wiki rows surface the job's own
  error text as their title. Action failures (403 vault-admin, 409
  already-finished) surface as a toast; the next poll reconciles the row.
- **New backend read route**: `GET /documents/reindex/jobs` lists the 20
  most recent reindex jobs for the tray's reindex family — admin-gated,
  dual-store (unified `jobs` table when the reindex lease switch is on,
  else `document_reindex_jobs`), `interrupted` mapped to `failed` in both
  branches. Deliberately CSRF-free and rate-limit-free: the sibling
  per-job route's guards are fitted to a rare manual lookup, not to a
  route the tray polls every ~8s (the SPA's CSRF interceptor only arms on
  mutating methods, and `admin_rate_limit` is 10/minute IP-keyed).
- **e2e**: the stub backend gained the job-list/action/seed routes the tray
  and the new `activity-tray.m04.spec.ts` use (both e2e tests: five-family
  reload survival; cancel + retry reach the stub).

## Notable non-events

- The per-page job panels (WikiJobsPanel, DraftWorkspace's cancel UI,
  KMSPage's #783 polling) are untouched — the tray is additive.
- `UploadIndicator` is untouched: it tracks this tab's in-flight HTTP
  uploads (bytes on the wire before a server row exists), a different
  concern from server-side job state.

## Tests

- Frozen acceptance checks C1-C10 (trace 784-activity-tray-shell-job-center):
  PageShell landmark (C1), tray row/cancel/retry/KMS-completion behaviour
  (C2-C6), the e2e spec (C7), and the three preserving baselines (C8-C10).
- `frontend/src/hooks/useJobStatus.m04.test.ts`: adapter semantics
  (vault fan-out, ingest union dedupe, kms 403 tolerance, draft two-hop
  fan-out, terminal-row retention, unmount safety).
- `backend/tests/test_m04_reindex_jobs_list.py`: legacy + unified store
  reads, ordering, limit, `interrupted` mapping, admin gating, the
  no-CSRF-header GET contract, and poll fitness (a 25-request burst does
  not 429).
