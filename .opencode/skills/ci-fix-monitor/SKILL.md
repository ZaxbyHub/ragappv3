---
name: ci-fix-monitor
description: >
  Monitor and fix CI on an open RAGAPPv3 pull request until every required check
  is green. Load when asked to watch CI, diagnose red checks, or drive a PR to
  passing. Maps the four real CI jobs (Frontend, Backend, Quality contracts, SAST),
  enforces diagnose-before-fix, and covers re-push / rebase. This repo has NO
  dist-check, biome, bun, or per-OS matrix — ignore that guidance. Updated for
  PR #215 (issue #209): Backend now runs the full `pytest tests/` suite
  (3918 tests, ~18m on CI Linux) — NOT the old 3-file subset. Job timeout is
  60m. pytest-timeout=300 caps per-test hangs.
---

# CI Fix & Monitor Protocol (RAGAPPv3)

Activates when the user asks to monitor CI, fix CI failures, or resolve red
checks on a PR in this repository.

For the commit/push/rebase/force-with-lease mechanics themselves, defer to
**`.claude/skills/commit-pr/SKILL.md`** — this skill is about *diagnosing and
fixing* the failures, not re-deriving publication rules. To reproduce a gate
locally before re-pushing, use `ci-compatibility-audit`.

## The CI surface (what can go red)

CI is a single PR-triggered workflow (`.github/workflows/ci.yml`, triggered on
`pull_request` against `master`) with **exactly four jobs**, all Ubuntu-only:

| Job | Steps that can fail | Run locally from |
|-----|--------------------|------------------|
| **Frontend** | `npm ci --engine-strict`, toolchain graph check, `npm run typecheck`, `npm run lint` (`--max-warnings 0`), API smoke subset, `npm test`, `npm run build`, two subpath builds | `frontend/` |
| **Backend** | `ruff check .`, **`pytest --tb=short -v --timeout=300 tests/`** (full suite, 3918 tests), `pytest --cov=app --cov-report=term-missing -q --timeout=300 tests/` (coverage) | `backend/` |
| **Quality contracts** | `python scripts/check_config_contract.py`, `python scripts/check_pr_scope_drift.py`, `python scripts/check_sast_baseline.py`, `python scripts/check_skill_sync.py` (skill-tree drift), `python scripts/check_secretscan.py` (`.secretscanignore` globs) | repo root |
| **SAST** | `python scripts/run_bandit.py` (bandit baseline gate; fails on new findings vs `backend/security/bandit-baseline.json`) | repo root |

> The Backend job runs the **full pytest tests/** suite since PR #215 / FR-4
> (issue #209). It takes ~18m on CI Linux (5.6x slower than local). A green
> Backend job DOES prove the changed area passes (as long as the touched
> test file isn't skipped via markers). Run the changed-area tests too for
> faster local feedback.
>
> The coverage step (`continue-on-error: true`) cannot fail the job — never
> chase a red there. BUT the job timeout (60m after #215) can be hit by the
> test+coverage combo on very slow runners.

## Tool availability

Use the `gh` CLI when available; otherwise use the GitHub MCP tools. Verify MCP
tool availability with `ToolSearch` before the first call in a session.

| `gh` CLI | GitHub MCP equivalent |
|---|---|
| `gh pr checks <number>` | `mcp__github__pull_request_read` method `get_check_runs` |
| `gh run view <run-id> --job <job-id> --log` | `mcp__github__get_job_logs` (`job_id`, `return_content: true`) |
| `gh pr view <n> --json mergeable` | `mcp__github__pull_request_read` method `get` |
| `gh pr edit --title` | `mcp__github__update_pull_request` (`title`) |

## Step 1 — Fetch current status

List all check runs for the PR head commit. If every required check is green,
report success and stop. Otherwise record which of the four jobs is red.

## Step 2 — Get the real error before fixing anything

Diagnose-before-fix is mandatory. For each failed job, fetch the **tail** of the
log (last ~100 lines unless the error is near the start) and find the exact
marker:

- **Backend → ruff**: a rule code + location, e.g. `F841`, `E501`, `F541`
  (`<file>.py:<line>: <code>`). Fix the cited line.
- **Backend → pytest**: a Python traceback ending in `assert X == Y` (the diff
  tells you expected vs actual) or an error like `database is locked` /
  `Incorrect number of bindings`. Note the `<file>.py::<Class>::<test>`.
  - For pytest-timeout failures: look for `TimeoutExpired` and the test name.
  - For C-level hangs (no log output, pytest-timeout=300 doesn't fire): the
    hang is in a native extension (bcrypt hashpw, sqlite3.connect, fcntl
    locks). Look for the last PASSED test before silence and audit its
    test setUp/tearDown for blocking I/O.
- **Frontend → typecheck/build**: `error TS####` with file + line. `npm run
  build` runs `tsc` first, so a type error fails the build too.
- **Frontend → lint**: an eslint warning line — CI uses `--max-warnings 0`, so a
  *warning* is a failure. Fix it; do not bump the threshold.
- **Frontend → test**: `FAIL src/...test.tsx` + the failing assertion.
- **Quality contracts**: the script prints the specific contract or scope-drift
  violation and exits non-zero — read that message; it names the surfaces that
  disagree.

Do not guess the cause from the job name. Read the log.

## Step 3 — Classify the failure

| Class | How to tell | Action |
|---|---|---|
| **Introduced by this PR** | Failure references a file/area your branch touched | Fix it before merge |
| **Pre-existing on `master`** | Same check fails on `master`'s latest run, unrelated to your diff | Document in PR body; do not fix unless scoped |
| **Environment / branch drift** | `npm ci` lockfile mismatch, or `check_pr_scope_drift.py` empty diff because base history isn't fetched | Re-sync (rebase onto `master`, refetch `origin/master`) |
| **C-level hang (silent)** | pytest -v produces zero output for 30+ min, no TimeoutExpired | Native extension hang. Find the last PASSED test, audit its setUp/tearDown for blocking I/O (bcrypt, sqlite3, fcntl). Apply targeted fix. |

Confirm "pre-existing" by reproducing on `master` in a throwaway worktree (see
`running-tests` → *Confirming a pre-existing failure*) before claiming it.
**Check your own session history first** — a test you edited earlier this
session is not pre-existing.

## Step 4 — Fix, by job

Reproduce locally first (commands above / `ci-compatibility-audit`), apply the
minimal fix, then re-verify locally before pushing.

### Backend (ruff)
```bash
cd backend && ruff check .          # reproduce
ruff check --fix .                  # auto-fixable rules (F841, whitespace, …)
# fix the rest by hand, then:
ruff check .
```

### Backend (pytest)

For PR-introduced test failures, reproduce in isolation:
```bash
cd backend && pytest --tb=short -v tests/<file>.py::<Class>::<test>
```

For full-suite reproduction (mimics CI):
```bash
cd backend && pytest --tb=short -v --timeout=300 tests/
# Expect ~18m on CI Linux, ~3-5m locally. If you don't have 3-5m, use a narrower scope.
```

#### Common Backend test failure patterns (from PR #215 diagnostic history)

1. **Missing dependency override**: Test setUp only overrides `get_current_active_user` but the endpoint uses `Depends(get_evaluate_policy)` or `Depends(get_db)`. FastAPI resolves all dependencies even if not called → real DB connection acquired → pool hang. **Fix**: add the missing override in setUp, matching what the endpoint actually needs.

2. **Singleton connection pool pollution**: `_pool_cache` in `app/models/database.py` persists across the full test run. The conftest.py `pytest_configure` only deletes `app.*` modules at startup, not between tests. **Fix**: add an autouse fixture in `tests/conftest.py` that closes all pools in `_pool_cache` and clears the cache both before AND after yield. Pattern in `tests/conftest.py::_reset_db_pool`.

3. **Cumulative bcrypt slowness**: bcrypt cost factor 14 = ~1 sec per hash. If a test creates 50+ users in a loop, setUp is 38 sec; multiple test classes → cumulative 5+ min. **Fix**: session-scoped autouse fixture in conftest.py that pre-computes the bcrypt hash for common test passwords ('pass123') once at session start. CRITICAL: patch `pwd_context.hash` (the underlying CryptContext method), NOT `hash_password` (the wrapper). Test modules that do `from app.services.auth_service import hash_password` capture the reference at import time, bypassing module-level patches. See `tests/conftest.py::_cache_bcrypt_hash_for_test_passwords`.

4. **Test passes for the wrong reason**: a test that inspects `call_args[0][0]` (positional) when the production code calls with kwargs may pass by inspecting the LAST call (which is from a different code path). **Fix**: use `call_args_list` and find the right call by signature. Always verify WHICH call the test inspects.

5. **Bounded queue with no consumer (deadlock)**: When adding `asyncio.Queue(maxsize=N)` for DoS mitigation, audit all `put()` call sites. If `put()` happens BEFORE any consumer task is spawned, deadlock on >N items. **Fix**: spawn consumers before producers. Pattern in `BackgroundProcessor.start()`.

#### When the suite is too slow for the 60m job timeout

The full suite (3918 tests) takes ~18m on CI Linux. Plus coverage step (re-runs all tests with --cov) = ~36m total. If the job hits 60m, the most likely cause is:
- A test in a tight loop calling a slow function (e.g., bcrypt) — see pattern 3 above
- The CI runner is unusually slow (transient) — re-trigger by force-pushing an empty commit

## Step 5 — Branch drift / rebase

If the failure is branch drift (base moved, lockfile or contract changed on
`master`):
```bash
git fetch origin master
git rebase origin/master
# if the rebase conflicts, abort and escalate — do not force a resolution:
#   git rebase --abort
git push --force-with-lease origin <branch>
```
`--force-with-lease` is required after a rebase and refuses to clobber remote
commits that appeared after your last fetch. Never plain `--force` a shared
branch.

## Step 6 — Push and re-monitor

Push once via the `commit-pr` flow, then wait for the **next** CI result before
pushing again — do not stack pushes on un-confirmed CI. If checks stall in
`queued`, re-fetch status manually and report the stall rather than waiting
indefinitely.

**CI may not trigger after force-pushes.** If no new check-run appears within
2 minutes of a force-push, rebase onto latest `origin/master` first — this
triggers a fresh `pull_request synchronize` event more reliably than close/
reopen cycles or empty-commit pushes.

For multi-iteration CI fix runs: keep each fix commit SMALL and focused.
A single-line test update or single-config change is better than a
multi-file refactor when the goal is to bisect which fix worked. Reviewers
read each commit; giant commits hide intent.

## Step 7 — Verify all green

Do not declare victory until all four required jobs (Frontend, Backend, Quality
contracts, SAST) are green. A `skipped` check is acceptable only if it is skipped on
`master` too (path-filter gate) — confirm that explicitly.

## Anti-patterns

- Do NOT watch CI passively without reading the failing log first.
- Do NOT call a failure "pre-existing" without reproducing it on `master`.
- Do NOT silence `check_pr_scope_drift.py` by editing unrelated files — fix the
  real drift.
- Do NOT raise the eslint `--max-warnings` threshold to dodge a warning.
- Do NOT look for dist-check, biome, bun, or a per-OS matrix — this repo has
  none of them.
- Do NOT stack pushes on un-confirmed CI. Wait for the next result.
- Do NOT make speculative fixes without reading the log — pytest-timeout
  doesn't fire on C-level hangs; the actual hang may be a different mechanism
  than your hypothesis suggests.
- Do NOT add multi-file refactors in CI fix commits — keep each fix focused
  to make the intent reviewable.
- After a rebase, a `--force-with-lease` push is expected; a plain push will be
  rejected.
