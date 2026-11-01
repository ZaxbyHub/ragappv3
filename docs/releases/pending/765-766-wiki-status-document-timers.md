# Wiki status convergence and document-page timer durability (Issues #765, #766)

## What changed

### Wiki compile status is transient until the job actually runs (`backend/app/api/routes/wiki.py`)

- A `pending` ingest job now reports the transient `compiling` semantic
  status from `GET /wiki/documents/{id}/status` instead of collapsing into
  the terminal `not_compiled` — the "Compile" affordance no longer sticks
  while a queued job exists. Every pre-existing mapping (running, failed,
  cancelled, completed/skipped, no-job) is unchanged.
- `POST /wiki/documents/{id}/compile` is now idempotent while a job for that
  document is still active: a second Compile returns the existing `pending`
  or `running` job (202) instead of enqueueing a duplicate. The
  lookup-then-create sequence runs under `BEGIN IMMEDIATE` on the request
  connection, so concurrent Compile requests cannot both observe "no active
  job" and double-enqueue.

### The documents-table wiki hydration converges (`frontend/src/components/documents/useDocumentPolling.ts`)

- The wiki hydration key now includes each document's status, so a
  `processing`→`indexed` transition with an unchanged id set re-fetches that
  document's wiki status.
- A failed per-document wiki fetch is no longer indistinguishable from
  "not compiled": the document is marked failed and rendered as "Wiki status
  unavailable" instead of a Compile button. Failed documents are owned
  exclusively by the retry interval — five attempts per document (initial +
  four retries at 5/10/20/40 s spacing), then the schedule stops — and the
  failure state is scope-owned: a vault or identity switch releases it, so
  the next scope starts fresh. A cached status plus a LATER failed refresh
  renders the unavailable state too (the failure is never masked by the
  stale value).
- A superseded batch no longer discards in-flight per-document results, and
  a newer wiki read no longer truncates a batch mid-flight: commits are
  ordered per document by a successful-commit watermark — a newer FAILURE
  never raises the watermark (a held older success still lands), a newer
  SUCCESS blocks a subsequent older late success, and every batch runs to
  completion.
- The post-compile toast says "Wiki compile already in progress" when the
  idempotent route returns an existing running job.

### The document detail page converges its own status (`frontend/src/pages/DocumentDetailPage.tsx`)

- While a document's status is non-terminal (`pending`/`processing`), a
  dedicated background refresh re-reads it every 2 s and merges the fresh
  metadata (status, chunk counts, failure counts, extraction diagnostics)
  plus the top-level progress fields — the skeleton, tag selection, tag
  vocabulary, preview, and foreground load generation are untouched. Any
  terminal status disarms the loop, five consecutive refresh failures stop
  it (a deleted or permanently failing document is not polled forever), and
  a route change resets the loop cleanly. "Ask about this document" enables
  without a reload when the document finishes indexing.
- Failed and cancelled documents now tell the truth in a VISIBLE failure
  line in the Details card (plus the Ask button's tooltip): "Indexing
  failed" / "Indexing was cancelled" with the server's failure message and
  the actual remediation (re-upload to index again) — previously the only
  guidance was an unreachable tooltip claiming a nonexistent "retry from
  Documents" action on every non-searchable state.

### Confirmed deletes survive page teardown (`frontend/src/pages/DocumentsPage.tsx`, `frontend/src/lib/api/core.ts`)

- A confirmed single-document delete is flushed on `pagehide` (reload, tab
  close) through a keepalive DELETE carrying the cached CSRF token and
  bearer; its outcome is logged (never silently dropped), and on a bfcache
  restore (`pageshow`, `persisted`) any delete whose dispatch could not be
  confirmed is replayed through the normal path — so an Undo click can
  never resurrect a document whose delete already survived teardown (Undo
  says "Delete already sent" instead). When no CSRF credential is cached at
  teardown, nothing is attempted (a fallback could not complete) and the
  intent is replayed on return.
- The flush is owner-keyed: a delete confirmed by one principal is never
  dispatched under another principal's bearer after an identity switch.
- Logout now flushes pending deletes BEFORE the auth store clears the
  bearer and CSRF credential (`ragapp:before-logout`), so the confirmed
  deletes no longer die as 401s with a spurious error toast on the login
  screen.
- The SPA-unmount flush and the pagehide flush share one exactly-once
  helper (each timer entry is claimed before its channel fires), and a
  failed unmount flush surfaces an error toast — aggregated with a count
  when several deletes fail. The 3 s undo window itself is unchanged.

### Refresh timers key on transitions, not array identity (`useDocumentPolling.ts`)

- The list refresh fires once per NEW upload id (acceptance — a newly
  accepted document appears in the list within a bounded delay, not only
  after it reaches `indexed`) and once per NEW upload→`indexed` transition
  (the seen-set reconciles against the currently-indexed ids, so a
  retryUpload round trip re-arms). The timer cannot be starved by progress
  churn: it is owned by the scope lifecycle, not the uploads effect, and a
  transient failure of the background refetch no longer wipes a loaded
  list to the error state.
- The adaptive status poll now actually resets to 2 s when the fetched
  documents changed since the previous committed response (signature over
  status, metadata progress, and the top-level progress/phase fields); the
  ×1.5 backoff and 30 s ceiling are unchanged for static data.

### No bearer token in the documents-surface console logs (`frontend/src/lib/api/core.ts`)

- The four documents-surface failure handlers (documents list, stats, tags,
  folders) log through a shared `logApiFailure` helper that prints the
  error message plus response status and request URL (read from both the
  apiClient's normalized error shape and a raw AxiosError) — never the raw
  error, whose `toJSON()` serializes the request config and its
  `Authorization` header. Toasts and the header itself are unchanged.
  Other surfaces' failure logs are unchanged in this PR.

## Upgrade notes

- No schema, settings, or API response-shape changes. The only observable
  API behavior change is the idempotent compile route (duplicate Compile
  clicks return the active job instead of enqueueing another; the sequence
  now holds a sqlite write lock briefly) and the `pending` → `compiling`
  semantic mapping.
