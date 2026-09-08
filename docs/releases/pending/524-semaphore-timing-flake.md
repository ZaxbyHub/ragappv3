# Test-only: de-flake test_semaphore_admits_parallel_execution (Issue #524)

## What changed

- `backend/tests/test_multiscale_search.py`
  (`TestMultiScaleSemaphoreConcurrency.test_semaphore_admits_parallel_execution`,
  renamed from `test_semaphore_parallel_execution_timing` since it no longer
  measures timing):
  the wall-clock bound (`elapsed_time < 0.06` for six 10 ms mock tasks behind a
  4-permit semaphore) is replaced with scheduling observability. The mock
  `_search_single_scale` now tracks an in-flight counter (same `current_concurrent`
  / `max_concurrent` nonlocal pattern already used by
  `test_semaphore_allows_four_concurrent_searches`, ~line 192, and
  `test_exactly_four_scales_boundary`, ~line 319, in the same file) and the test
  asserts `max_concurrent >= 2` after `store.search()` completes. The unused
  `import time` was removed.

## Why

The semaphore itself is not broken — the issue (ZaxbyHub/ragappv3#524, surfaced
during PR #523 review; the file was reworked by PR #244, but the wall-clock
bound itself predates that PR, dating to commit `3c0f1c6`) documents that the timing
assertion fails intermittently under load (reproduced during two concurrent
full-suite `pytest -n auto` runs, then passed on immediate retry) while always
passing in isolation and on dedicated CI runners. A wall-clock `< 60 ms` bound
on scheduler behavior is inherently load-dependent; the fix asserts the actual
invariant the test was written to prove — that the semaphore admits parallelism
(4 permits, 6 scales → at most one task would ever be in flight if execution
were serial).

## Migration steps

None. Test-only change; no config, API, wire, or persistence impact.

## Known caveats

- The assertion threshold is deliberately loose (`>= 2`), mirroring the
  sibling-boundary test's intent: with 6 scales and 4 permits it structurally
  cannot exceed 4 concurrent tasks, so the lower bound only distinguishes
  "parallel" from "serial". It does not pin the exact permit count (that is
  `test_exactly_four_scales_boundary`'s job) and does not measure wall-clock
  runtime — load-dependent timing is intentionally out of scope for this test.
- The sibling observation in the issue (that
  `test_phase2_coverage.py::TestRRFFuseReceivesClampedWeights` stubs away the
  dense arm, silent-degradation risk via `asyncio.gather(..., return_exceptions=True)`)
  is tracked separately (issue comment) and deliberately NOT included here to
  keep this a small standalone test-only PR.
