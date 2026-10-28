# Backend CI fail-fast on hang-class wedges + secretscan/coverage artifact-race closure (#1021, #943)

## What changed

- The Backend job's pytest (and the nightly full-suite job, plus the four
  local/doc mirrors of that command) now run with `--timeout-method=thread`.
  The previous POSIX default (signal method) behaves per hang class: a
  signal handler only runs when the main thread executes bytecode, so on a
  GIL-releasing C-block call — the wedge class behind the four silent
  60-minute stalls on 2026-10-21..24, including on master — it fires late
  (when the call returns) or never on unbounded calls; on starved-pool
  joins it DOES interrupt, as a per-test failure. The thread method covers
  both classes: it aborts the wedged worker within the per-test timeout.
- New env-gated canary `backend/tests/test_ci_hang_canary.py` (runs only
  under `RUN_CI_HANG_CANARY=1`) plus a four-leg Backend CI step proving the
  contract on every run, on Linux: both wedge members (pbkdf2 C-block and
  the issue-named starved-pool join) abort under the thread method with the
  full Timeout banner + thread-stack dump; the xdist arm produces the named
  crashed-worker signature; and the old signal method is shown to still
  cover interruptible waits (the coverage boundary).
- The #258 coverage-gate test now passes
  `--coverage.reportsDirectory=<per-test tmp>` so vitest's transient
  coverage artifacts never appear under the repository root mid-suite —
  closing the race that crashed the secretscan contract test (#943).
- `scripts/check_secretscan.py`'s stale-glob advisory walk (its second
  full-repo `rglob`) got the same vanish-tolerant scandir walk as the main
  scan, closing the remaining #943 AC1 leg; a new deterministic unit
  guardrail (`test_943_secretscan_walk_robustness.py`) choreographs a
  mid-walk vanish so the class cannot silently return. Deliberate semantic
  change: symlinked directories are listed but not followed by the new
  walk (matching the main walk's directory handling; rglob recursed into
  them) — a `<dir>/**` ignore pattern reachable only through a symlinked
  directory would now be reported stale. The repo carries no symlinked
  directories today.

## Operator-visible effects

- In-suite (the realistic wedge): a hung test aborts within the per-test
  timeout and the output names it —
  `worker 'gwN' crashed while running '<nodeid>'` — instead of a silent
  60-minute stall. xdist replaces the crashed worker and the suite keeps
  running; the RUN goes red at end-of-suite with the crashed node named.
  The in-suite crash does not carry a stack dump (xdist does not surface
  it); the canary step's standalone legs carry the full Timeout banner +
  all-thread stack dump for the canary wedges.
- A slow-but-completing test over 300 s now terminates its worker (still a
  red run, with more diagnostics; previously a per-test TimeoutError).
- Coverage of a killed worker is lost (fail-fast priority).
- Disclosed residual: a native call that holds the GIL indefinitely starves
  both timeout methods; that subclass is NOT covered (an out-of-process
  watchdog would be required — recorded in the PR body's Considered table).
- The production trigger of the 2026-10-21..24 wedges is INFERRED (the logs
  carried no stacks, which is the defect); with this change the next
  occurrence is identified and substantially narrowed instead of silent.

## Issue closure mapping

- #1021: AC1 closed by the thread-method flags + the committed canary and
  its CI step (frozen check C1); diagnosis recorded in the PR body.
- #943: AC1's remaining (advisory-walk) leg closed by the scandir repair +
  deterministic unit guardrail + targeted pre-fix crash probe; AC2 closed
  by the reportsDirectory confinement (frozen check C3); AC3 evidence accrues
  post-push (consecutive green Backend CI runs on this PR's heads; the
  count is recorded in the PR's Merge status once the runs land — master's
  post-`31197279` history is green for the pre-existing walk fix).
