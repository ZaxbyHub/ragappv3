# Commit-boundary audit durability, FTS relevance truth, tray 403 suppression (#1068, #1069, #1070)

## What changed

- **Security-audit rows survive cancellation at the commit boundary (#1068).** The 21 remaining
  commit-then-audit tails in `auth.py` (register, login success/failed, logout — including the
  access-token denial sequence, password change, session revocations), `users.py` (admin password
  reset with its cache invalidation, org/group replacements), `vaults.py` (org adoption +
  visibility in ONE drained unit, group access), and `draft_room.py` (delete, cancel, retry,
  compile, ready, export, both promotion arms, all three finding dispositions) now run their
  commit → post-commit steps → mandatory audit sequence inside a new `drained_unit(...)`
  (task_drain.py, factored with `drain_commit_audit`'s settle logic). A cancellation delivered
  exactly at the commit await — after the commit work completed — is absorbed until the unit
  settles, then re-raised; the tamper-evident row for the applied mutation is no longer lost.
  Non-cancellation paths are unchanged (same calls, same order, same exception translation;
  logout's commit-failure-still-audits behavior preserved).
- **FTS relevance is real (#1070).** `filter_relevant`'s no-`_distance` branch reads the actual
  LanceDB FTS key `_score` (then the legacy `score` tier, then the 1.0 default) in both threshold
  modes and in `expand_window`/the debug-log reader. FTS-only hits no longer bypass the relevance
  floor with a fabricated 1.0. The wire now carries an honest per-source score kind (`"fts"`)
  through default-on context distillation, and the frontend (SSE mapper, relevance labels,
  SourceCards, MessageContent, RightPane) prefers the per-source kind and suppresses the
  relevance badge for uncalibrated FTS scores instead of misreading them as lower-is-better
  distances. The three #745 `xfail(strict=True)` quarantines are removed — those assertions pass
  un-quarantined.
- **The Activity tray's post-403 suppression works in production (#1069).** `errorStatus` reads
  all three shapes the API client produces (`status` → `response.status` →
  `originalError.response.status`), so a 403 from a tray action now hides the Cancel/Retry
  affordances that could only ever fail again.

## Why

- #1068: a cancellation at the commit await unwound handlers before the audit coroutine existed —
  the audit row for an APPLIED mutation (including auth lockout counters) was silently lost.
- #1070: real LanceDB 0.40.0 FTS rows carry `_score`; the legacy `score` read made every FTS-only
  hit take the 1.0 pass-everything default, so BM25 relevance never gated anything and the wire
  score was fabricated.
- #1069: the client's interceptor normalizes errors to a top-level `status` with no `response`
  property; the tray's single-tier reader never fired.

## Operator impact

- Audit trails: security-audit rows for cancelled-but-applied mutations now consistently exist.
  No schema or settings changes.
- Retrieval quality: FTS-only results below the relevance floor are dropped (previously all
  passed); source scores in responses are real BM25 values for FTS-only hits, with `score_type:
  "fts"` on those sources; relevance badges are hidden for them (uncalibrated vs distance
  thresholds).
- UI: tray affordances disappear after a 403 refusal for that row (previously they stayed).

## Tests

- New frozen suites: `test_{auth,users,vaults,draft_room}_commit_boundary_family.py`,
  `test_score_followup.py`, `ActivityTray.normalized-403.test.tsx` (RED at base, GREEN at head;
  10/10 named-mutation probes killed).
- New companions: `test_task_drain_drained_unit.py`,
  `test_commit_boundary_translation_preservation.py` (route-level exception-translation
  preservation + 21-site census), `test_fts_score_kind_wire.py`,
  `sse-score-precedence.test.ts`.
- Un-quarantined: `test_rag_engine_filtering_adversarial.py`,
  `test_relevance_cutoff_calibration.py`, `test_rag_engine_none_threshold_adversarial.py`.
