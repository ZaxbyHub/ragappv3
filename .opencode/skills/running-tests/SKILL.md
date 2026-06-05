---
name: running-tests
description: >
  RAGAPPv3 test EXECUTION patterns. Load when you need to run, scope, or
  diagnose tests — backend pytest and frontend Vitest — not when authoring them
  (see writing-tests for that). Covers per-file/-test targeting, CI-equivalent
  local runs, reading CI failure logs, output truncation recovery, and the
  Python 3.11 event-loop trap. This repo's frontend uses Vitest, NOT bun:test.
---

# Running Tests for RAGAPPv3

This skill is about **executing** tests safely and reproducing CI locally. For
**writing/organizing** tests, load `writing-tests`. For the full CI-mirror gate
before a push, load `ci-compatibility-audit`.

RAGAPPv3 has two test ecosystems:

- **Backend** — `pytest` over `unittest.TestCase` / `IsolatedAsyncioTestCase`
  classes, under `backend/tests/`. Run from `backend/`.
- **Frontend** — Vitest + React Testing Library + jsdom, under `frontend/src/`.
  Run from `frontend/`. `npm test` = `vitest run`.

There is no Bun, no `bun:test`, no `test_runner` tool, and no per-OS matrix —
CI is Ubuntu-only (`.github/workflows/ci.yml`).

---

## ⚠️ The Python 3.11 event-loop trap (read first)

CI pins **Python 3.11**. On a newer local interpreter (3.12+, e.g. 3.14) some
backend tests fail with:

```
RuntimeError: There is no current event loop
```

This is a **local-interpreter artifact, not a regression** — the harness uses
the implicit-event-loop pattern removed in newer Python. Never report this
specific error as a real failure, and never "fix" production code to chase it.
Use a Python 3.11 virtualenv when possible:

```bash
# from backend/
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
```

If you only have a newer interpreter, distinguish this error from a genuine
assertion failure before drawing any conclusion.

---

## Backend — running pytest

Run from `backend/` (CI uses `working-directory: backend`).

```bash
# Whole file
pytest --tb=short -v tests/test_tags_routes.py

# Single class
pytest -v tests/test_tags_routes.py::TestTagRoutes

# Single test method
pytest -v tests/test_tags_routes.py::TestTagRoutes::test_create_tag

# Keyword filter across files
pytest -v -k "cascade and not adversarial"

# Stop on first failure, terse output (fast dev loop)
pytest -x --tb=line -q tests/test_tags_routes.py
```

`asyncio_mode = "auto"` is set in pyproject — async tests are auto-detected, no
marker or flag needed.

### CI-equivalent backend run

CI runs **only a narrow targeted subset**, plus `ruff`:

```bash
# from backend/  — exact CI test invocation
ruff check .
pytest --tb=short -v tests/test_path_prefix.py tests/test_auth_routes.py tests/test_main_catchall.py
```

CI does **not** run the whole backend suite. That means a green CI does not
prove your changed area is tested — **always also run the tests for the area you
touched locally** (e.g. `pytest -q tests/test_tags_routes.py`).

---

## Frontend — running Vitest

Run from `frontend/` (CI uses `working-directory: frontend`).

```bash
# Whole suite (what `npm test` runs in CI: vitest run)
npm test

# Single file — pass args after `--`
npm test -- src/lib/api.test.ts

# Multiple files
npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts

# By test-name pattern
npm test -- -t "renders empty state"

# Watch mode (local dev only, never in CI)
npm run test:watch
```

### CI-equivalent frontend run

The frontend job runs, in order: typecheck, lint, an API smoke subset, the full
test run, then several builds. Reproduce locally before pushing:

```bash
# from frontend/
npm run typecheck
npm run lint          # eslint src --max-warnings 0  (zero-warning gate)
# CI's API smoke subset (runs before the full suite):
npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts src/lib/api.sse.test.ts src/pages/WikiPage.sse.test.tsx src/stores/useAuthStore.api-base.test.ts
npm test              # full vitest run
npm run build         # tsc && vite build
```

`npm run lint` is a hard zero-warning gate (`--max-warnings 0`) — a single
eslint warning fails CI. `npm run build` runs `tsc` first, so a type error
fails the build too.

---

## Quality-contract scripts (repo root)

The **Quality contracts** CI job runs two scripts from the repo root:

```bash
python scripts/check_config_contract.py     # env/config contract across surfaces
python scripts/check_pr_scope_drift.py       # flags auth-test drift, CI-tooling drift
```

`check_pr_scope_drift.py` diffs against `origin/master` (or `GITHUB_BASE_REF` /
`PR_SCOPE_DRIFT_BASE`). Locally it needs the branch history, so fetch
`origin/master` first if the comparison looks empty.

---

## Reading CI failure logs

CI is a single PR-triggered workflow with three jobs: **Frontend**,
**Backend**, **Quality contracts**. When one is red:

```bash
# List checks for the PR and find the failing job
gh pr checks <number>

# Pull the failing job's log (tail is usually enough)
gh run view <run-id> --job <job-id> --log | tail -100
```

If `gh` is unavailable, use the GitHub MCP tools: `mcp__github__pull_request_read`
(method `get_check_runs`) to list checks, then `mcp__github__get_job_logs`
(`return_content: true`) for the log.

Find the exact failure marker:
- **Backend**: a Python traceback with `<file>.py:<line>` and an assertion diff
  (`assert X == Y`), or a `ruff` rule code (`F841`, `E501`, …).
- **Frontend**: a Vitest `FAIL src/...test.tsx`, a `tsc` `error TS####`, or an
  eslint warning line.

Reproduce that exact file locally with the single-file commands above before
changing anything.

---

## Confirming a pre-existing failure

Before calling a failure "pre-existing," prove it on `master` without disturbing
your working tree. Use a git worktree (safer than `git stash`):

```bash
git worktree add /tmp/ragapp-master origin/master
# backend
(cd /tmp/ragapp-master/backend && pytest -q tests/<file>.py)
# frontend
(cd /tmp/ragapp-master/frontend && npm ci && npm test -- src/<file>.test.tsx)
git worktree remove /tmp/ragapp-master
```

- Fails on `master` too → pre-existing. Note it in the PR body; do not fix unless
  scoped.
- Fails only on your branch → you introduced it. Fix before pushing.

**Check your own session history first** — a test you edited earlier this session
is not "pre-existing."

---

## Truncated output recovery

When a test run floods the buffer, write to a file and tail it instead of
re-running:

```bash
# backend
pytest -v tests/test_tags_routes.py 2>&1 | tee /tmp/pytest_out.txt
tail -60 /tmp/pytest_out.txt

# frontend
npm test 2>&1 | tee /tmp/vitest_out.txt
tail -60 /tmp/vitest_out.txt
```

To see only pass/fail summary lines:

```bash
pytest -q tests/test_tags_routes.py 2>&1 | grep -E "PASSED|FAILED|ERROR|passed|failed" | tail -20
```

Prefer `-q`/`--tb=line` over default verbose output for large runs to keep the
buffer small.

---

## Quick reference: common failures and causes

| Symptom | Likely cause | What to do |
|---|---|---|
| `RuntimeError: There is no current event loop` | Local interpreter is 3.12+, not 3.11 | Use a 3.11 venv; ignore as a local artifact |
| `coroutine was never awaited` | Async test not detected / not awaited | Confirm `asyncio_mode = "auto"`; declare the method `async def` |
| `database is locked` in later tests | A connection wasn't released in `finally` | Release pooled connections in `finally` (see writing-tests) |
| `Incorrect number of bindings` | SQL `?` placeholders ≠ bound params | Count placeholders, bind exactly that many |
| Backend test sees 500 instead of 200 | Mock raised bare `Exception`, prod catches specific types | Match the production `except` tuple exactly |
| `eslint ... warning` fails CI | `--max-warnings 0` gate | Fix the warning; warnings are errors here |
| `error TS####` during `npm run build` | `tsc` runs before `vite build` | Fix the type error; build won't pass without it |
| jsdom test can't open a Radix `Select` | Radix portals don't work in jsdom | Mock `@/components/ui/select` (see writing-tests / frontend-testing-gotchas) |
| `<Link>` test throws "useContext" | No router context | Wrap render in `MemoryRouter` |

---

## Before you push

1. Backend touched → `ruff check .` + the CI subset + your changed-area tests, from `backend/`.
2. Frontend touched → `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`, from `frontend/`.
3. Config/CI/contract surfaces touched → both `scripts/check_*.py` from repo root.
4. Run `ci-compatibility-audit` for the authoritative CI-mirror command list.
