# 774: A failed, loading or stalled read stops reading as "down", "logged out", "empty", or hanging forever

Issue: #774 ([Workstream L] PR 3 of 9; frontier audit run 20260923T174456Z
findings UI-R3-01, UI-R4-09, UI-R4-10, T1-20-K-07, TQ-sibling-batch-08-04,
TQ-sweep-B09-01, TQ-sweep-B05-02, TQ-sweep-B05-03, TQ-sibling-batch-06-04).

## What changed

- **Settings → Overview is honest while checking.** While `useHealthCheck`'s
  first poll is in flight (`loading: true`), each service row renders a muted
  "checking" label and dot instead of "down" — the same "UNKNOWN is not DOWN"
  rule the reconnect banner already follows (PR #606). Only a real poll result
  renders ok/down.
- **A mid-session outage no longer logs you out.** `refreshAccessToken` now
  distinguishes transport-class failures from session verdicts: a network
  error, a 5xx, or a refresh that exceeds its new 10 s deadline REJECTS
  (transport-class), while a genuine 401/CSRF-403 still resolves `null` and
  clears auth exactly as before. The delivered legs are the apiClient 401
  interceptor (a transport refresh failure no longer redirects to login; the
  request re-rejects in the shared normalized error shape with the session
  intact) and the in-memory-token init path (remount/hot-reload). Scope note:
  a cold page reload during an outage still lands on the login screen
  (unchanged from before — the refresh cookie path cannot restore
  `isAuthenticated` without a reachable backend); only the persisted `user`
  identity is retained. Timeout errors are deliberately never named or worded
  "AbortError"/"abort" so user-cancel sentinels (chatStream, useSendMessage)
  cannot swallow them. A rate-limited refresh (HTTP 429 — the refresh endpoint
  enforces 30/minute, see the 659 release note) still resolves `null` and
  clears the session, exactly as before; only network errors and 5xx changed
  behavior.
- **The CSRF and refresh fetches are bounded.** Both `/csrf-token`
  (`ensureCsrfToken`) and `/auth/refresh` (`_doRefresh`) carry an
  AbortController + 10 s setTimeout deadline (fake-timer-friendly; no
  `AbortSignal.timeout`). On timeout the fetch settles, the singleton
  in-flight promise clears, and a retry issues a new request — previously one
  hung CSRF fetch wedged every mutating request forever.
- **Panel failures stop reading as confirmed-empty.** OrgsPage's organization
  list renders an `ErrorState` with Retry on failure (title "No organizations
  found — couldn't load" — the surface stays identifiable, the definite
  verdict is gone); the member-search dropdown shows "User search failed"
  instead of "No users found"; VaultsPage surfaces `listOrganizations`
  failures with a toast (previously swallowed, leaving the org selector
  silently absent); MaintenanceSettings renders "No recent jobs — couldn't
  load" as a `role="alert"` line on failure; the Wiki activity panel and
  WikiPageDetail's Version History / Attachments / Backlinks sections each
  render a destructive failure line instead of their "No …" empty verdicts.
  Genuine empty results keep their empty states.
- **The wiki-jobs read is bounded server-side.** `GET /api/wiki/jobs` accepts
  `limit` (1–500, validated; `WikiStore.list_jobs` already supported it in
  both SQL paths) and the Maintenance tab passes `limit: 10`, matching what
  its own docstring always claimed. The `{jobs: [...]}` payload shape is
  unchanged.
- **The chat stream ends on silence.** `parseSSEStream` races each
  `reader.read()` against a 150 s inactivity window (10 × the backend's 15 s
  SSE heartbeat, so a healthy generation can never trip it): on timeout the
  reader is cancelled and the stream takes the existing interrupted path
  (`ChatInterruptedError`), which in the full `chatStream` context drives the
  existing Last-Event-ID resume machinery. Previously a connection that
  delivered nothing parked the stream forever.
- **Disposal cancels backoff sleeps.** The chat `token_expired` retry wait
  and both event-stream hooks' reconnect backoffs now use the abort-aware
  sleep (timer + abort listener) shipped for the resume backoff in 9e4ebf7e:
  unmount wakes the sleep immediately, and the disposed chat stream checks
  its abort signal before calling `refreshAccessToken()` — no post-disposal
  work fires.

## Contract notes (disclosed)

- `refreshAccessToken`'s failure contract changed: rejection ⇔ transport-class
  (network / timeout / 5xx); `null` ⇔ auth-shaped rejection or non-5xx 4xx.
  Awaiter disposition: the 401 interceptor, the auth store, and both
  event-stream hooks handle the new rejection explicitly (the hooks treat a
  transport rejection exactly like the old `null`, i.e. "stop", which was
  already their behavior on a failed refresh). The two `chatStream` refresh
  awaits (pre-stream near-expiry check and the token_expired retry) rely on
  the stream's outer catch, so a transport failure surfaces the underlying
  error to the chat UI (e.g. "auth refresh timed out" or a connection-error
  message) instead of the old "Session expired. Please log in again." — the
  stream's own retry/abort semantics are unchanged.
- Two test files pinned the old contracts and were updated with in-file
  rationale: `core.subpath-refresh-diagnostic.test.ts` (its 503 and
  network-error cases now expect rejection; suppression assertions unchanged;
  also note this file was the meridian-canvas-401-vault-zero trace's frozen
  check C1 — that trace's freeze-time evidence predates this change) and
  `WikiPageDetail.sections.test.tsx` (its two "swallows a rejected fetch and
  shows the empty state" tests pinned exactly the defect #774 classifies and
  now pin the failure lines).

## Not changed (recorded, out of this issue's scope)

- `GroupFormDialog`'s swallowed `listOrganizations` (the Workstream L PR 1
  groups-family pattern), `WikiJobsPanel`'s unbounded jobs pull (the wiki
  management surface), `useWikiEventStream`'s unguarded read loop, and
  `ProfilePage`'s allSettled-keeps-empty pattern are same-class survivors
  dispositioned in the trace's recurrence census rather than silently
  dropped or quietly widened into this PR.
- The Overview tab's "checking" state covers the first poll and the cold-cache
  re-poll window only: after the health hook's re-poll budget is exhausted, or
  on a failed first check, never-probed services still render "down" until the
  next poll succeeds (pre-existing `useHealthCheck` behavior, unchanged
  here).
