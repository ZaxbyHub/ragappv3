# 1040 + 991 — health 401 routing, chat cap sync, and stream error detail

Issues: #1040, #991 (collated; one PR authorized this session).

## #1040 — session expiry no longer renders as a service outage

`useHealthCheck` now reads the rejection's HTTP status shape-tolerantly (the
normalized top-level `status` the api client produces, with the raw-axios
fallback — the `httpStatusOf` convention). A 401 on the auth-gated deep probe
(the #774 refresh-transport residual is the only 401 that reaches the hook)
routes as an AUTH event: a new `auth_unknown` reducer outcome — gated on the
read still being the latest, like its failure sibling — retains the last real
service booleans, holds `loading` ("checking"), stamps `lastChecked`, advances
the publish sequence (stale outcomes cannot clobber), and resets the failure
streak — so two expired-token probes no longer publish the all-services-down
state. The silent refresh goes through the canonical
`useAuthStore.refreshToken()` action: a resolved-null outcome clears the
session on the store's terms; a transport failure keeps it for the next poll.
Network and 5xx failures keep today's failure-streak behavior exactly.

## #991 — the composer bound is the server's, retries never truncate first, and error responses name their reason

- `GET /api/settings` now exposes `chat_message_max_chars` (not redacted for
  members — it is their own composer bound; read-only: the setting stays
  env-configured).
- A per-owner cap loader in `useSendMessage` is the sole authority: its own
  `getSettings()` fetch (never the shared settings store), re-armed with an
  immediate refetch on auth-owner replacement and after any failed
  acquisition. Four value states: fetch failure blocks; ABSENT field (legacy
  mixed-version server, cap unknowable) blocks; present-but-malformed blocks;
  a valid integer is authoritative with no ceiling clamp.
- While the authoritative cap is unresolved, the composer's display, change
  feedback, submit gate, and disabled state use the 20_000 advisory value
  (the shipped default) — an input over the advisory is refused with the
  named bound until the fetch resolves or fails, rather than being handed to
  the transport against an unknown server limit (fail-closed).
- Every send path awaits admission (`checkSendAdmissionAsync`, measured on
  the trimmed payload — the same string the server caps) BEFORE any
  destructive or observable step: `handleSend` (which also re-verifies that
  the composer text and active chat are unchanged across the await — an edit
  or chat switch during the window blocks the stale snapshot and preserves
  the newer state), `sendDirect` (backstop, which restores the original
  revision's rows whenever a resend is dropped after truncation), and —
  critically — the TranscriptPane retry entry, which validates before the
  server truncate and any row removal (after the non-destructive
  persist-wait; an over-cap persisted turn keeps its history and rows; edit
  remains the recovery path and surfaces the bound at load). No session is
  created for a blocked send. A failed acquisition re-arms the loader, so
  the next attempt genuinely retries the fetch.
- Composer change feedback, the submit gate, the disabled state, and the
  character counter read the effective cap reactively; draft STORAGE keeps
  the fixed legacy 100_000 ceiling, decoupled from the cap (drafts beyond
  the effective cap are restorable but not sendable until shortened or the
  server cap is raised). Both send closures (normal submit and "Send
  without these files") defer draft clearing and attachment detachment until
  the send reports admission — a blocked send leaves the user's draft and
  attachments intact.
- The chat stream's `!response.ok` arm surfaces a JSON error body's
  non-empty string `detail` (with `.status` attached) for any failed
  stream-route status — the server's named rejection reaches the user
  instead of `HTTP error! status: 422`. (Stream-route error details are
  fixed server literals today; none carry per-request data.) Non-JSON
  bodies, failed reads, empty/non-string details, and the already-consumed
  401-refresh body (whose parse is retained and reused, not re-read) keep
  the exact status-only message.

## Residual disclosed

If the settings endpoint itself is unreachable, sends are blocked with an
actionable "message limit unavailable" error (fail-closed per the four-state
policy) — the app is degraded in that state regardless. Against a backend
older than this change, the cap field is absent and sends stay blocked
(deploy the backend before or with this frontend).
