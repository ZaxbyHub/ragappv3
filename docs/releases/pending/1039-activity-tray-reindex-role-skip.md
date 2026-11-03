# Activity tray stops polling the admin-only reindex family for non-admins (#1039)

Issue: #1039 (follow-up to #784, which introduced the Activity tray and the
"403 = family hidden" contract).

## What changed

`useActivityJobs` (frontend/src/hooks/useJobStatus.ts) no longer dispatches the
reindex family unconditionally. `fetchAllActivityRows` gates the
`fetchReindexRows()` adapter (kept registered inline, so the #784 family-census
guardrail still finds it) behind a two-layer visibility check:

1. Role-aware skip: when the session's role is known, only `admin` and
   `superadmin` principals dispatch `GET /documents/reindex/jobs` at all — a
   `member` or `viewer` session issues zero requests to the route for the whole
   page session. The role is read at poll time via a new dependency-free getter,
   `captureAuthPrincipalRole()` in frontend/src/lib/api/auth-lifecycle.ts (the
   same publication channel `useAuthStore` already feeds with
   `publishAuthPrincipal`; no store subscription, no new module graph).
2. First-403 backoff: the family is fetched once per principal generation; a
   403 arms a backoff and later ticks skip the dispatch, and a principal change
   (login-as switch without a reload) gives the new principal its own first
   probe. The backoff records the generation the dispatch was made under, so a
   rejection from a parked request that lands after a principal switch cannot
   hide a principal that never probed.

The two layers are combined rather than short-circuited (swarm-pr-review
PRR-001): the role is only a client-side snapshot, republished at
login/init/fetchMe/profile update and never revalidated mid-session, while
`require_admin_role` re-reads `users.role` on every request. The server's 403
verdict therefore wins over the role guess — an armed backoff hides the family
for the rest of that principal's session whatever the snapshot says, so a
demoted admin stops issuing the dead request too. A later principal change
still re-opens the family for the new principal.

The 403 detection reads both error shapes the API client produces (normalized
Error with top-level `status`, or raw AxiosError `.response.status`), mirroring
`logApiFailure`'s shape tolerance. Non-403 failures (network, 5xx) do not arm
the backoff — they stay the ordinary one-tick family miss of the #784 contract.

The other four families (ingest, wiki, kms, draft-room) are untouched, and the
route's backend admin gate is untouched (out of scope per the issue).

Pinned by frontend/src/hooks/useJobStatus.issue1039.test.ts: member/viewer
fixtures dispatch zero reindex requests across multiple polls while all four
sibling families keep polling and rendering; admin/superadmin fixtures keep the
family every poll; the backoff fires exactly once per principal and re-arms
correctly across principal changes.

## Why

`GET /documents/reindex/jobs` sits behind `require_admin_role`
(backend/app/api/routes/documents.py), so for a member session every tray tick
was a guaranteed 403 — swallowed by `Promise.allSettled` (correct UX per #784)
but still served by the backend and logged by the browser as a failed network
resource. Deployment evidence on the issue: 100+ such 403s in one member
session (~7.5 wasted requests/minute per mounted tray), with console noise
burying real errors during incident triage.

## Migration steps

None. Frontend-only change; no API, config, schema, or data changes. Deployed
member sessions simply stop issuing the requests after the frontend updates.

## Known caveats

- A role-less principal still issues exactly one reindex request per principal
  (the backoff trigger) before the family hides; a member session whose auth
  store hydrates before the tray's first tick issues zero, which is the normal
  case.
- The client's role snapshot is not revalidated mid-session (no periodic
  `/auth/me` refresh), so two asymmetric windows exist, both disclosed:
  a principal promoted to admin while the tab is open keeps the family hidden
  until a reload or an explicit profile fetch (the issue's Scope In sanctions
  the role-aware skip, and this matches every other admin-gated surface), and
  the demoted-admin window is closed by the server-verdict-wins rule above
  rather than by a role refresh.
- The backoff is per page session and per principal generation, not persisted:
  a reload of a role-less session re-issues the single probe request.
- Ingest-family pagination bounds from #784 are unchanged (documents beyond the
  3-page budget are still not surfaced).
