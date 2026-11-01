# Access-log truthfulness: client_ip redaction, case-insensitive credential-param redaction, monotonic durations, logged maintenance 503s, and a working forced-password-change exit (Issues #730, #731)

## What changed

### Every `http_request` access-log line stops leaking the caller's address (`backend/app/middleware/logging.py`)

- `client_ip` joined `SCRUB_FIELDS`, so the production filter chain
  (`SensitiveFieldFilter` on the root handler) rewrites the emitted
  `client_ip` attribute to `"[redacted]"` — byte-identical to the shape
  `docs/release.md` has always documented. Operators keep the field (it
  reads `[redacted]`) rather than losing it silently.
- Query-string redaction is now case-insensitive on the decoded parameter
  NAME: `?API_KEY=secret` redacts exactly like `?api_key=secret`. The
  legacy raw-fragment credential test is unchanged, and name-only matching
  means a benign VALUE that merely contains a credential-looking word in
  mixed case (`?note=API_KEY`) stays intact — values are not over-redacted.
- `duration_ms` is computed from `time.monotonic()` — an NTP clock step
  backwards can no longer produce a negative duration on the access line.

### Maintenance 503s become access-logged and request-IDed (`backend/app/main.py`)

- The middleware registration order changed (call order:
  Maintenance → SlowAPI → Logging → CORS; effective stack outermost-first:
  CORS → Logging → SlowAPI → Maintenance). `LoggingMiddleware` now wraps the
  middleware that short-circuits maintenance 503s, so a 503 during a window
  carries an `X-Request-ID` response header and produces an `http_request`
  access-log line with `status_code` 503, like every other response.
- CORS stays outermost (headers on all responses, unchanged); the
  SlowAPI-vs-Maintenance relative order is unchanged. Rate-limit outcomes
  per route class are unchanged: on maintenance-exempt limited routes
  (login) the decorator 429 still fires; on blocked limited routes the 503
  can still precede the decorator.
- Note for operators reading old copies of the admin guide: the previous
  "maintenance-window 503s produce no application access line" caveat no
  longer holds and the guide is updated; invalid-Host 400s and unhandled
  500s remain outside the access logger's reach.

### A flagged user can finish the forced password change during a maintenance window (`backend/app/middleware/maintenance.py`)

- `POST /api/auth/change-password` joined `_EXEMPT_POST_PATHS`. It is the
  one POST the `must_change_password` gate leaves open; before this change
  a flagged user could authenticate during a window but had no exit (every
  route 403 except change-password, which 503'd). No other route became
  reachable.
- New guard in `backend/tests/test_exempt_paths_guard.py`: every
  `must_change_password`-exempt POST path (canonicalized to its mounted
  `/api` spelling) must be maintenance-exempt — the two allowlists can no
  longer silently disagree.

### Docs match emission

- `docs/release.md`'s structured-logging example now spells the timestamp
  key exactly as `JsonFormatter` emits it (`ts`, was `timestamp`); every key
  the example documents is emitted, and `client_ip`'s documented
  `[redacted]` value is now true.
- `docs/admin-guide.md`'s access-log caveat is rescoped (see above).
- `app/utils/request_context.py`'s docstring now points at
  `app.middleware.logging.SensitiveFieldFilter` (it previously claimed the
  module provides the class itself).

### Dead code removed

- `_scrub_value` (a case-folding helper with zero callers whose docstring
  falsely claimed `_sanitize_query` used it) is deleted.

## Tests

- `backend/tests/test_f01_access_log_contract.py` — the #730 contract suite
  (renders lines through the production filter chain): client_ip redaction,
  inbound request_id emission and 50-request header parity, mixed-case
  query redaction, non-negative duration under a -10 s clock step,
  docstring-name honesty, documented-keys-are-emitted, plus the preserved
  attribute-scrubbing/message-body/RequestIdFilter-registration pins.
- `backend/tests/test_f02_short_circuit_logging.py` — the #731 suite:
  maintenance 503 carries X-Request-ID and is access-logged, stack-order
  pin (Logging wraps Maintenance + SlowAPI, CORS outermost-of-core),
  change-password maintenance exemption, and the 429 request-id guard.
- `backend/tests/test_f03_logging_guards.py` — behavioral
  RequestIdFilter-registration proof through the real lifespan startup
  (both fresh-handler and existing-handler arms; frozen C10 is a source
  pin) and the query name-only case-scoping regression.
- `backend/tests/test_exempt_paths_guard.py` — the maintenance-subset guard
  (+ its bites-on-disagreement mutation probe).

## Known limitations

- `client_ip` is now always `[redacted]` in the access log; operators who
  need network coarse-graining (incident triage by subnet) get no signal —
  that is the documented contract's choice, and X-Forwarded-For trust is a
  separate product decision (see #981's PRR-012 record).
- The access logger still does not see three response classes, all answered
  outside it and unchanged from before this PR: CORS preflight OPTIONS
  responses (CORSMiddleware stays outermost and answers them itself),
  invalid-Host 400s (the host-header validator sits in an outer
  user-middleware layer), and unhandled 500s (converted by the server's
  error middleware); uvicorn's own log remains the source for those, as the
  admin guide states precisely.
