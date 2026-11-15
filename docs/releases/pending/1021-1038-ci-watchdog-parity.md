# Backend CI out-of-process watchdog + command-parity pin + secretscan link-dir contract (#1021, #1038)

## What changed

- **#1021 (reopened residual):** the Backend CI Test step now runs under an
  out-of-process watchdog, `scripts/ci_backend_watchdog.py`
  (`python ../scripts/ci_backend_watchdog.py --quiet-window 360
  --poll-interval 5 --hard-deadline 2700 -- pytest <canonical flags>
  tests/`). The #1034 fix (thread-method timeout + canary) cannot bound the
  disclosed residual — a GIL-held hang starves BOTH pytest-timeout methods,
  and it recurred twice on master post-closure (runs 42416548773 on
  2026-11-07 and 43515466761 on 2026-11-13: 99% progress, zero FAILED
  lines, canary passing, step killed at the ceiling). The watchdog watches
  from OUTSIDE: 360s of output silence (the 300s per-test timeout + 60s
  grace, so the in-process timeout always fires first when it can) or the
  45-minute hard deadline fires a `WATCHDOG-FIRED` banner naming EVERY
  pending test (dispatched-but-uncompleted node ids — the tracker keeps an
  ordered pending set from the raw-chunk stream, including
  newline-terminated-but-uncompleted and final partial dispatch lines),
  captures per-PID Python stacks (opportunistic `py-spy` 0.4.3 installed
  CI-only; primarily the committed `scripts/ci_watchdog_faulthandler_hook.py`
  copied to the Test step's `PYTHONPATH` sitecustomize, registering
  `faulthandler.register(SIGUSR2, file=<per-PID dump>)` — a C-level dump
  that needs no GIL and writes to files pytest/xdist capture cannot
  swallow; per-PID provider failures are disclosed in the banner), kills
  the process group bounded, and fails the step with exit 88 — exiting by
  ~47m05s worst case (2700s + ≤5s poll + ≤120s bounded FIRE, 175s inside
  the 50-minute step ceiling). The watchdog preflights the hook fail-closed
  on POSIX (a missing readiness marker refuses to run the suite unmonitored
  BEFORE the expensive run starts). The production trigger stays UNDIAGNOSED
  (GIL-held native call or another mechanism); the captured stacks settle
  the attribution on the next recurrence.
- **#1038 Item A:** `backend/tests/test_1021_timeout_wiring.py` (the #1034
  wiring gate) now pins the full canonical invocation
  `pytest --tb=short -v --timeout=300 --timeout-method=thread -rs -n auto
  --cov --cov-report=term-missing tests/` at every declared mirror (ci.yml
  Test step, justfile backend-test, `docs/engineering/testing.md`, the
  ci-compatibility-audit and ci-fix-monitor skills) with failures naming
  the site AND each missing flag; `.agents/skills/ci-fix-monitor/SKILL.md`'s
  Backend rows are rewritten to the canonical single invocation (the
  phantom `--cov=app` split coverage run and the flag-dropped reproduction
  command are gone, #1038's live drift instances); the
  ci-compatibility-audit local-mirror fence now carries the canonical flags
  too. The Test-step locator accepts the watchdog-wrapped run line while
  still excluding the canary block.
- **#1038 Item B:** both secretscan walkers (`on_disk_files` /
  `on_disk_dirs` in `scripts/check_secretscan.py`) list linked directories
  lexically and never descend them, per platform: POSIX symlinks (detected
  by `is_symlink()`) and Windows junctions/directory-symlinks (reparse
  points, detected by `st_file_attributes & 0x400` — `is_symlink()` is
  False for them, probe-verified). Pre-fix, Windows junction targets WERE
  descended (the documented no-follow contract was false) and POSIX
  link-dirs were dropped entirely (never listed). Advisory-only surface
  (the walkers feed stderr diagnostics; the exit code never depends on
  them).

## Tests

- New `backend/tests/test_1021_watchdog_wedge.py`: watchdog passthrough;
  silent-wedge fire (banner, pending naming, independent child-liveness
  proof); synthetic-stream pending-set semantics (the critic-probe shape);
  POSIX stack arms (SIGUSR2 file capture, py-spy-denied fallback,
  production-capture real `pytest -v -n 2` two-worker wedge); POSIX
  group-kill parent+grandchild arm. Windows skips carry explicit reasons;
  Linux CI runs everything.
- `backend/tests/test_1021_timeout_wiring.py`: canonical-flag mirror pins
  (site+flag-named failures), both ci-fix-monitor site pins (`cov=app`
  count 0), and the watchdog-wiring pin (watchdog on the run line + hook
  copy step + `PYTHONFAULTHANDLER`/`PYTHONPATH`/`WATCHDOG_DUMP_DIR` env).
- `backend/tests/test_943_secretscan_walk_robustness.py`: junction arm
  (Windows; sentinel file+dir under the target never enumerated through the
  link, lexical listing asserted, regular-dir recursion control) and POSIX
  symlink arm; per-walker revert probes recorded in the issue-tracer trace.

## Operator-visible effects

- A Backend silent wedge now ends ≤~47m with a named diagnostic instead of
  the 50-minute ceiling: look for `WATCHDOG-FIRED` + exit 88 in the Test
  step; the pending-test list and per-PID stacks land in the same log.
- `check_secretscan` advisory walks no longer traverse Windows
  junctions/dir-symlinks; POSIX symlinked dirs now appear lexically in the
  directory census (previously invisible).
