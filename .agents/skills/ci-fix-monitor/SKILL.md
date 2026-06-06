---
name: ci-fix-monitor
description: >
  Monitor and fix CI on an open RAGAPPv3 pull request until every required check
  is green. Load when asked to watch CI, diagnose red checks, or drive a PR to
  passing. Maps the three real CI jobs (Frontend, Backend, Quality contracts),
  enforces diagnose-before-fix, and covers re-push / rebase. This repo has NO
  dist-check, biome, bun, or per-OS matrix — ignore that guidance.
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
`pull_request` against `master`) with **exactly three jobs**, all Ubuntu-only:

| Job | Steps that can fail | Run locally from |
|-----|--------------------|------------------|
| **Frontend** | `npm ci --engine-strict`, toolchain graph check, `npm run typecheck`, `npm run lint` (`--max-warnings 0`), API smoke subset, `npm test`, `npm run build`, two subpath builds | `frontend/` |
| **Backend** | `ruff check .`, targeted `pytest --tb=short -v` (`test_path_prefix.py`, `test_auth_routes.py`, `test_main_catchall.py`) | `backend/` |
| **Quality contracts** | `python scripts/check_config_contract.py`, `python scripts/check_pr_scope_drift.py` | repo root |

> The backend job runs only that **targeted pytest subset** — a green Backend
> job does not prove your changed area passes. Run the changed-area tests too.
>
> The coverage step (`continue-on-error: true`) cannot fail the job — never
> chase a red there.

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
report success and stop. Otherwise record which of the three jobs is red.

## Step 2 — Get the real error before fixing anything

Diagnose-before-fix is mandatory. For each failed job, fetch the **tail** of the
log (last ~100 lines unless the error is near the start) and find the exact
marker:

- **Backend → ruff**: a rule code + location, e.g. `F841`, `E501`, `F541`
  (`<file>.py:<line>: <code>`). Fix the cited line.
- **Backend → pytest**: a Python traceback ending in `assert X == Y` (the diff
  tells you expected vs actual) or an error like `database is locked` /
  `Incorrect number of bindings`. Note the `<file>.py::<Class>::<test>`.
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
Read the production `except` clause before adjusting a mock — a mock raising
bare `Exception` when prod catches specific types is the #1 false 500. Fix code
or test, then:
```bash
cd backend && pytest --tb=short -v tests/<file>.py::<Class>::<test>
# plus the CI subset:
pytest --tb=short -v tests/test_path_prefix.py tests/test_auth_routes.py tests/test_main_catchall.py
```

### Frontend (typecheck / lint / test / build)
```bash
cd frontend
npm run typecheck      # fix TS errors
npm run lint           # zero-warning gate — fix every warning
npm test               # vitest run
npm run build          # tsc && vite build
```

### Quality contracts
```bash
git fetch origin master            # scope-drift needs base history locally
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```
`check_pr_scope_drift.py` flags real drift (e.g. auth tests changed without
matching auth runtime/doc changes, CI/tooling changes without a contract
update). **Fix the underlying drift — do not game the diff** to silence it.

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

## Step 7 — Verify all green

Do not declare victory until all three required jobs (Frontend, Backend, Quality
contracts) are green. A `skipped` check is acceptable only if it is skipped on
`master` too (path-filter gate) — confirm that explicitly.

## Anti-patterns

- Do NOT watch CI passively without reading the failing log first.
- Do NOT call a failure "pre-existing" without reproducing it on `master`.
- Do NOT silence `check_pr_scope_drift.py` by editing unrelated files — fix the
  real drift.
- Do NOT raise the eslint `--max-warnings` threshold to dodge a warning.
- Do NOT look for dist-check, biome, bun, or a per-OS matrix — this repo has
  none of them.
- After a rebase, a `--force-with-lease` push is expected; a plain push will be
  rejected.
