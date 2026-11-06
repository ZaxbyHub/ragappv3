# 1040 + 991 — health 401 routing, chat cap sync, and stream error detail

Issues: #1040, #991 (collated; one PR authorized this session).

## #1040 — session expiry no longer renders as a service outage

`useHealthCheck` now reads the rejection's HTTP status shape-tolerantly (the
normalized top-level `status` the api client produces, with the raw-axios
fallback — the `httpStatusOf` convention). A 401 on the auth-gated deep probe
(the #774 refresh-transport residual is the only 401 that reaches the hook)
routes as an AUTH event: a new `auth_unknown` reducer outcome retains the last
real service booleans, holds `loading` ("checking"), stamps `lastChecked`,
advances the publish sequence (stale outcomes cannot clobber), and resets the
failure streak — so two expired-token probes no longer publish the
all-services-down state. The silent refresh goes through the canonical
`useAuthStore.refreshToken()` action: a resolved-null outcome clears the
session on the store's terms; a transport failure keeps it for the next poll.
Network and 5xx failures keep today's failure-streak behavior exactly.

## #991 — the composer bound is the server's, retries never truncate first, and 422s name their reason

- `GET /api/settings` now exposes `chat_message_max_chars` (not redacted for
  members — it is their own composer bound; read-only: the setting stays
  env-configured).
- A per-owner cap loader in `useSendMessage` is the sole authority: its own
  `getSettings()` fetch (never the shared settings store), re-armed with an
  immediate refetch on auth-owner replacement. Four value states: fetch
  failure blocks; ABSENT field (legacy mixed-version server, cap unknowable)
  blocks; present-but-malformed blocks; a valid integer is authoritative with
  no ceiling clamp. The 20_000 advisory value is display/change-feedback only
  while unresolved.
- Every send path awaits admission (`checkSendAdmissionAsync`) BEFORE any
  destructive or observable step: `handleSend`, `sendDirect` (backstop), and —
  critically — the TranscriptPane retry entry, which validates before the
  server truncate and any row removal (after the non-destructive
  persist-wait; an over-cap persisted turn keeps its history and rows; edit
  remains the recovery path and surfaces the bound at load). No session is
  created for a blocked send. A failed acquisition re-arms the loader, so the
  next attempt genuinely retries the fetch.
- Composer change feedback, the submit gate, the disabled state, and the
  character counter read the effective cap reactively; draft STORAGE keeps the
  fixed legacy 100_000 ceiling, decoupled from the cap. Both send closures
  (normal submit and "Send without these files") defer draft clearing and
  attachment detachment until the send reports admission — a blocked send
  leaves the user's draft and attachments intact.
- The chat stream's `!response.ok` arm surfaces a JSON body's non-empty
  string `detail` (with `.status` attached) — the server's named cap rejection
  reaches the user instead of `HTTP error! status: 422`. Non-JSON bodies,
  failed reads, and empty/non-string details keep the exact status-only
  message.

## Residual disclosed

If the settings endpoint itself is unreachable, sends are blocked with an
actionable "message limit unavailable" error (fail-closed per the four-state
policy) — the app is degraded in that state regardless.
