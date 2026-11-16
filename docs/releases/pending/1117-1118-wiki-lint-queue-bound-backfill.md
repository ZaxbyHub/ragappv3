# Wiki lint run admission bound, promotion identity gate, legacy lint backfill (#1117, #1118)

## What changed

- **Lint-run admission bound (`POST /api/wiki/lint/run`)** — the route now
  admits at most `_WIKI_LINT_MAX_CONCURRENT_RUNS` (default 2) in-flight
  runs and rejects the excess immediately with a retryable
  `503 Wiki lint scan capacity reached; retry shortly`. Previously every
  concurrent request queued unboundedly on the single-worker lint executor
  while pinning its pooled DB connection for the whole wait — a burst of
  ten pinned the entire 10-connection pool and 500'd every endpoint in the
  process (execution-proven, PR #1113 OOB review F-02). The default of 2
  keeps the frontend's documented dual-vault overlap (start a lint, switch
  vaults, start another) succeeding. The slot is released when the scan
  WORKER completes — not when the handler exits — so client cancellation
  mid-scan cannot free a slot the executor still occupies, and a scan that
  outlives its event loop (embedded/multi-cycle servers) releases through
  the worker thread itself (the release is independent of the
  submitting event loop's lifecycle). Operators can tune the bound by patching
  `app.api.routes.wiki._WIKI_LINT_MAX_CONCURRENT_RUNS`; it is read at
  request time.
- **Promotion identity gate (wiki claims)** — a compile or curator run
  that re-derives a claim which differs from an existing one by INTERNAL
  punctuation ("U.S. economy shrank" vs "US economy shrank",
  "GPU-heavy workload" vs "GPUheavy workload", "model, version 2" vs
  "model version 2") no longer promotes that existing `unverified` claim
  to `active` or reactivates a `superseded` one. Dedup/source-attach still
  rides the fuzzy match (DD-C011 design); only the STATUS LIFTS now
  require text identity (exact match, or equality under the new
  `promotion_identity_key`, which tolerates only trailing `!`/`?`
  terminals — "Alpha is verified" ≡ "Alpha is verified!" still promotes,
  preserving the #929 parity; note a trailing PERIOD is now
  identity-relevant and blocks promotion, a visible narrowing of the
  pre-#1117 fuzzy-promotion behavior). Both promotion paths (the deterministic
  `_find_or_create_claim` twin and the curator-persist twin inside the
  compile savepoint) are gated; the #1113 F-03 numeric gate (5.10-vs-510)
  stays.
- **Legacy lint-row backfill (boot migration)** — one journal-recorded,
  naturally idempotent migration over the pre-2026-10-16 cohort:
  (1) legacy `resolved` rows flip to the system-only, rule-3-reopenable
  `auto_resolved`, so a recurring detection reopens them instead of being
  permanently suppressed (rule 2 keeps protecting post-cutoff and
  dismissed/acknowledged rows); (2) legacy rows the #791 backfill
  mislabeled `origin='linter'` (cohort boundary: the later of
  2026-10-16 and the database's own origin-migration upgrade instant,
  covering lagging-upgrade installs) that were actually curator-produced
  (identifiable by the "Curator "-prefixed title or details
  `subtype:"curator"`) re-label to `origin='curator'`, restoring
  same-origin rule-3 reopening for curator detections. Converged boots
  journal nothing (clean-boot contract). Linter-shaped legacy rows are
  data-indistinguishable and stay `linter` (disclosed boundary).

## Why

Issues #1117 (queue-bound + lossy-promotion residuals from the
#929/#932/#935 lifecycle, execution-proven) and #1118 (maintainer-decided
backfill for #935 items 5+6), collated per session authorization.

## Operator notes

- The lint 503 is retryable; the frontend surfaces route errors as
  before. Single-operator lint runs never hit the bound.
- The migration runs at boot; no DDL, no downtime, safe to re-run.
- The `wiki_lint_findings.origin` CHECK vocabulary is unchanged.
