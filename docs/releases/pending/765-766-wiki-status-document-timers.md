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
  or `running` job (202) instead of enqueueing a duplicate. Status-filtered
  limit-1 lookups cover both the unified lease store and the legacy table.

### The documents-table wiki hydration converges (`frontend/src/components/documents/useDocumentPolling.ts`)

- The wiki hydration key now includes each document's status, so a
  `processing`→`indexed` transition with an unchanged id set re-fetches that
  document's wiki status.
- A failed per-document wiki fetch is no longer indistinguishable from
  "not compiled": the document is marked failed, retried on the 5 s transient
  interval with capped per-document backoff (5 s → 80 s, five attempts,
  then stop), rendered as "Wiki status unavailable" instead of a Compile
  button, and cleared on success. The failed set is scope-owned (a vault or
  identity switch resets it).
- A superseded batch no longer discards in-flight per-document results:
  commits are ordered per document by a successful-commit watermark, so a
  held older success still lands while a newer success still wins.
- The post-compile toast says "Wiki compile already in progress" when the
  idempotent route returns an existing running job.

### The document detail page converges its own status (`frontend/src/pages/DocumentDetailPage.tsx`)

- While a document's status is non-terminal (`pending`/`processing`), a
  dedicated background refresh re-reads it every 2 s and merges ONLY the
  live status/progress fields — the skeleton, tag selection, tag vocabulary,
  preview, and foreground load generation are untouched, and any terminal
  status disarms the loop. "Ask about this document" enables without a
  reload when the document finishes indexing.
- The disabled Ask button's tooltip is now truthful for terminal states:
  `error` (and legacy `failed`) documents say "Indexing failed — retry from
  Documents", `cancelled` says "Indexing was cancelled", instead of the
  always-wrong "Waiting until indexed".

### Confirmed deletes survive page teardown (`frontend/src/pages/DocumentsPage.tsx`, `frontend/src/lib/api/core.ts`)

- A confirmed single-document delete is flushed on `pagehide` (reload, tab
  close) through a keepalive DELETE carrying the cached CSRF token and
  bearer, falling back to the normal delete path only when no CSRF
  credential exists. The SPA-unmount flush and the pagehide flush share one
  exactly-once helper (each timer entry is claimed before its channel
  fires), and a failed unmount flush now surfaces an error toast instead of
  silently discarding a confirmed destructive action. The 3 s undo window
  itself is unchanged.

### Refresh timers key on transitions, not array identity (`useDocumentPolling.ts`)

- The upload-complete refresh fires exactly once per NEW upload→`indexed`
  transition (the seen-set reconciles against the currently-indexed ids, so
  a retryUpload round trip re-arms) and cannot be starved by progress churn:
  the timer is owned by the scope lifecycle, not the uploads effect.
- The adaptive status poll now actually resets to 2 s when the fetched
  documents changed since the previous committed response (signature over
  status, metadata progress, and the top-level progress/phase fields); the
  ×1.5 backoff and 30 s ceiling are unchanged for static data.

### No bearer token in the console (`frontend/src/lib/api/core.ts`)

- The four documents-surface failure handlers (documents list, stats, tags,
  folders) log through a shared `logApiFailure` helper that prints the
  error message plus response status and URL — never the raw Axios error,
  whose `toJSON()` serializes the request config and its `Authorization`
  header. Toasts and the header itself are unchanged.

## Upgrade notes

- No schema, settings, or API response-shape changes. The only observable
  API behavior change is the idempotent compile route (duplicate Compile
  clicks return the active job instead of enqueueing another) and the
  `pending` → `compiling` semantic mapping.
