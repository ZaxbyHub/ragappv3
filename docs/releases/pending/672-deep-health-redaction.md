# fix(health): deep-health error detail stops disclosing infra identifiers to non-admin users (#672, #720)

## What changed
Two related changes land together (audit run 20260923, Workstream D slots D-02 and D-01):

**Deep-health redaction (#672).** `GET /api/health?deep=true` now replaces the
identifying error-detail text in its `models`, `llm` and `vector_store`
sections with fixed non-identifying messages ("model not configured or
unavailable" / "service unavailable or not configured" / "embedding index
needs re-indexing") whenever the caller is a non-admin USER principal
(viewer or member). The HTTP-error `status_code` each model probe attaches
is nulled for those callers too — it distinguishes 401/404/5xx on
admin-only endpoints (provider-key validity). Configured model names,
provider URLs, available-model inventories, vector-store error strings and
embedding-dimension detail no longer reach those callers.

Retained, by contract, for every principal: the signal fields — `available`,
`ok`, `status` (including the `not_configured` markers), `stale_embeddings`,
`vector_store.rows`, `llm_modes` booleans, `services` booleans — and the
whole shallow (`deep` omitted/false) response. Unlike `GET /settings`
redaction (#660), deep health keeps a per-endpoint configured/not-configured
signal: a non-admin can still see WHICH provider slots are configured, just
not their identities.

**Repo-wide rate-limit guard (#720).** New test
`backend/tests/test_d01_limiter_route_introspection.py`: every mounted route
whose endpoint the shared slowapi limiter registered must store the
limiter-wrapped handler (the issue #659 decorator-order class), the mounted
set must reconcile exactly with the limiter's registries (static AND
dynamic), and a 43-entry policy roster pins each limited endpoint — a lost
`@limiter.limit` decorator now fails CI naming the exact route. Companion
suites (`test_d02_*`) pin the redaction contract from both directions:
disclosure absent for viewer/member, full detail kept for admins at level 3
exactly, signal fields preserved, replacement constants exact, and probe
`status_code` hidden below admin.

Admins, superadmins, and principals authenticated by the `X-API-Key`
monitoring credential (`settings.health_check_api_key`) keep the full,
unredacted deep-health detail, preserving the issue #551 C12 monitoring
contract. Unchanged for everyone: the shallow poll, the 401 gate on
`?deep=true`, and the `available`/`ok`/`status` signal fidelity.

## Why
The ModelChecker "model not found" error strings embed the configured model
name and the provider's full available-model listing; LLMHealthChecker
errors can carry provider URLs; the vector probe can carry store paths and
dimension detail. Any authenticated user could read all of that from
`?deep=true` while `GET /settings` blanked the same values for the same
principal (issue #672, audit run 20260923 slot D-02). This is a similar
role-aware boundary to #660's settings redaction, applied at the deep-health
response; unlike #660 it deliberately retains the configured/not-configured
signal per endpoint (see above).

## Migration
No migration required. Monitoring integrations that parse the removed error
strings for non-admin sessions must switch to the `available`/`ok`/`status`
fields or call with the monitoring API key. Note that the monitoring API key
is a high-value credential: the same value exempts its holder from every
`@limiter.limit` route in the app (see the #659 release note) — distribute
it to monitoring systems only, not to human users who merely need the
non-redacted view (admins already see full detail in their sessions).

## Caveats
- The replacement text is intentionally uniform per section and does not
  distinguish timeout / not-found / not-configured causes for non-admin
  users; the `status: "not_configured"` marker (where the producer emits
  one — the models entries and the chat leg) continues to carry the
  not-configured state. The embeddings and vector_store legs have no
  `status` key, so a non-admin cannot distinguish not-configured from
  unreachable on those two legs.
- For a non-admin, a true `stale_embeddings: true` alarm now reads
  "embedding index needs re-indexing" without the dimension numbers or the
  `scripts/migrate_embeddings.py` pointer — the remediation steps live in
  README (Embedding model migration) and are visible to admin sessions.
- Redaction is response-boundary only; this change adds no server-side
  logging either way. The changed producers (`model_checker.py`,
  `llm_health.py`) do not log at all; the deep-path log lines in `health.py`
  are unchanged and unchanged in audience (operator-facing WARNING/DEBUG).
- Known adjacent residual (pre-existing, recorded on the PR): the
  `GET /health/vector-reconciliation` diagnostic returns `str(sqlite3.Error)`
  to any authenticated user — SQL-layer text with no configured identifiers;
  fold it into the redaction helper if it ever starts interpolating
  configured values.
