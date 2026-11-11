# Caller-side error-status readers walk the client's normalized shape (#1078)

Follow-up from PR #1077 (the #1068/#1069/#1070 collated closure): five
frontend error-status readers walked fewer tiers than the shapes the app's
own API client produces, leaving dead failure-handling branches that tests
kept certifying against a fabricated raw axios shape.

## What changed

- **One shared helper.** `core.ts` now exports `httpStatusOf(err)` — the
  three-tier status walk (`status` → `response.status` →
  `originalError.response.status`, number-guarded) that `logApiFailure`
  already defined in-tree. Its doc comment carries the shared-helper
  census: the four readers routed through it (below) and the convention
  sites that still carry a local copy (`useJobStatus.ts` `httpStatusOf`,
  `useHealthCheck.ts` `httpStatusOf`, `ActivityTray.tsx` `errorStatus`,
  `canvas.ts` `getCanvasErrorStatus`, two-tier).
- **`sessions.ts` `addChatMessagesBatch`** — the rolling-restart 404
  fallback now reads the status through `httpStatusOf`. Over an apiClient
  rejection (normalized Error, top-level `status`, no `response`
  property) the fallback was dead: `status !== 404` always rethrew.
- **`TranscriptPane.tsx`** — both stale-view 409 arms (retry truncate
  catch, edit truncate catch) read the status through `httpStatusOf`. A
  real 409 fell through to the generic "Couldn't update conversation
  history" toast instead of reloading the session (#684 convergence).
- **`useSendMessage.ts`** — the session-create catch reads the status
  through `httpStatusOf`, so a 403 surfaces "You don't have permission to
  chat in this vault." instead of the generic connection message.
- **`draftRoom.ts` `parseDraftRoomError`** — the weaker class: the status
  is now read through `httpStatusOf`'s full walk (it previously read only
  the normalized top-level tier, so a raw axios error reaching the parser
  surfaced no status). Detail/code/context extraction is unchanged.

All five readers import the helper directly from `@/lib/api/core` (or
`./core`), not the `@/lib/api` barrel, so no test's barrel mock can
silence the production walk.

## Why

`core.ts`'s response interceptor rejects with a normalized `Error` whose
status is top-level and which has no `response` property
(`originalError` nests the raw axios error). The five readers each walked
only a tier that shape never carries (or, for `parseDraftRoomError`, only
one of the tiers), so their status-gated branches could not fire in
production. The pre-existing tests stayed green only because they mocked
the raw axios shape callers never receive — the certify-the-fabricated-
shape class (C13 sibling, consumer ignores the real producer shape).

## Migration steps

None. No wire, schema, settings, or API-shape changes; behavior only
widens (every reader keeps taking the branch it took for the raw shape,
and now also takes it for the normalized and re-wrapped tiers).

## Known caveats

- The interceptor's normalization itself and `logApiFailure`'s internal
  inline walk are unchanged by design (issue scope); the census comment
  documents the remaining local copies for a future consolidation sweep.
- `parseDraftRoomError` still extracts `detail`/`code`/`context` only via
  `originalError.response.data` (the normalized path); a raw error
  reaching it gains a status but not envelope decoding — same as before
  for that tier.

## Tests

- `sessions.batch.test.ts` — the 404-fallback legs now drive the api
  client's normalized error (top-level `status`, plus the re-wrapped
  `originalError.response.status`-only tier); the raw axios shape stays
  as a control leg (AC2), and the rethrow leg pins the non-404 path. The
  core mock spreads the real exports (`importOriginal`) so the reader
  runs the production helper.
- `TranscriptPane.normalized-shape.test.tsx` (new) — both 409 arms fire
  for the normalized shape and the re-wrapped tier (RED at base, GREEN at
  head); the raw-shape control leg stays green.
- `useSendMessage.normalized-shape.test.ts` (new) — the 403 permission
  branch fires for the normalized shape and the re-wrapped tier (RED at
  base, GREEN at head); raw-shape control and non-403 generic-message
  legs preserved.
- `draftRoom.test.ts` — `parseDraftRoomError` gains raw-shape and
  re-wrapped-tier status legs; the core mock now spreads the real
  exports.
