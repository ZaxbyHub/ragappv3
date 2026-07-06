---
name: ci-compatibility-audit
description: Lightweight PR-time audit for whether changes are compatible with the actual RAGAPPv3 GitHub Actions workflow, dependency lockfiles, scripts, and cross-platform local validation.
effort: medium
---

# CI Compatibility Audit

Use this skill before pushing workflow, dependency, test, build, lint, Docker, or tooling changes.

## Current CI Map

Primary workflow: `.github/workflows/ci.yml`

Frontend job:

- `cd frontend`
- `npm ci --engine-strict`
- frontend toolchain graph check with `node --version`, `npm --version`, `npm ls vite vitest @vitejs/plugin-react jsdom`, `npm exec vite -- --version`, and `npm exec vitest -- --version`
- `npm run typecheck`
- `npm run lint`
- API smoke tests for shared API, CSRF/SSE streaming, wiki SSE UL, and auth API-base behavior
- `npm test`
- `npm run build`
- subpath build with `VITE_APP_BASENAME=/knowledgevault` and `VITE_API_URL=/knowledgevault/api`

Backend job:

- `cd backend`
- `pip install -r requirements-ci.txt` (a **reduced** set — it deliberately
  excludes `lancedb`, `pyarrow`, `unstructured[all-docs]`, and
  `sentence-transformers`; those are stubbed at test time, see caveats below)
- `pip install -r requirements-dev.txt`
- `ruff check .`
- `pytest --tb=short -v --timeout=300 tests/` — **full backend suite (3918 tests since PR #215 / FR-4)**.
  Job timeout is **60m** (raised from 30m in PR #215 to accommodate the 3918-test full suite + coverage step on slow CI Linux runners; local baseline is ~3-5m, CI is ~18m, plus coverage runs another ~18m).
  `--timeout=300` caps per-test hangs at 5 min. The conftest.py has 4 fixtures (CSRF bypass, rate-limiter reset, SQLite pool reset, bcrypt cache for 'pass123' test password) that the test suite relies on.
- informational coverage (`continue-on-error: true`) over the same suite, also with `--timeout=300`

Repository contract job:

- `python scripts/check_config_contract.py`
- `python scripts/check_pr_scope_drift.py`

> **Note (post-PR #215):** Earlier versions of this skill documented a
> "narrow pytest subset" (8 test files). That subset was the pre-FR-4 state.
> PR #215 (issue #209) expanded CI to the full `pytest tests/` suite as part
> of the defense-in-depth hardening. Adding files to the suite is now
> automatic — just add the test file. The legacy "narrow subset" concept is
> no longer applicable. The local mirror command below reflects this.

## Checks

- Lockfiles exist and match the package manager used by CI.
- CI commands exist in package manifests or requirements files.
- Cache paths point at real lockfiles.
- Scripts do not depend on local-only absolute paths.
- Workflow shell syntax is valid on the configured runner.
- Pull request diff checks have enough fetch depth.
- Local validation commands mirror CI when possible.
- Truncated CI output does not hide the command exit status.
- For the 60m job timeout: tests with `pytest-timeout=300` per-test are bounded, but the cumulative suite (~36m with coverage) MUST fit. If you add tests that take cumulatively >20m, the job will fail. Profile slow tests with `pytest --durations=20`.

## Local Mirror Commands

```bash
cd frontend && npm ci --engine-strict && npm run typecheck && npm run lint
cd frontend && npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts src/lib/api.sse.test.ts src/pages/WikiPage.sse.test.tsx src/stores/useAuthStore.api-base.test.ts
cd frontend && npm test && npm run build
cd frontend && VITE_APP_BASENAME=/knowledgevault VITE_API_URL=/knowledgevault/api npm run build
cd backend && ruff check . && pytest --tb=short -v --timeout=300 tests/
python scripts/check_config_contract.py
python scripts/check_pr_scope_drift.py
```

Run these before pushing so a CI-only lint/type failure doesn't cost a
push → fail → fixup-commit round trip. If `frontend/node_modules` is absent,
run `npm ci --engine-strict` first.

For the backend test step, the full `pytest tests/` run takes ~3-5m locally and ~18m on CI Linux. Run your changed-area tests first for fast feedback:
```bash
cd backend && pytest -q tests/<file>::<Class>::<test>
```
Then run the full suite before pushing.

## Environment caveats (so local results aren't misread)

- **CI's dependency set is reduced — "locally green" ≠ "CI green".** CI installs
  only `requirements-ci.txt` + `requirements-dev.txt`, which omit `lancedb`,
  `pyarrow`, `unstructured`, and `sentence-transformers`. A dev machine usually
  has the *full* `requirements.txt` installed, so a backend test can pass locally
  yet fail in CI at import (`ModuleNotFoundError`) or behave differently. To
  validate a backend **test-scope** change (e.g. adding a new test file) faithfully,
  reproduce the CI env instead of trusting the local run:
  ```bash
  python -m venv /tmp/civenv
  /tmp/civenv/bin/pip install -r backend/requirements-ci.txt -r backend/requirements-dev.txt
  cd backend && /tmp/civenv/bin/python -m pytest -q tests/<candidate_file>.py
  ```
  This is also *faster* than the local suite (no multi-GB model/db loads).
  Corollary: a test only passes under the reduced set because something stubs
  the missing packages — those per-file `lancedb`/`pyarrow`/`unstructured` stubs
  are **load-bearing for CI, not dead boilerplate**. Do not "clean them up"
  without confirming the file still collects under the CI venv.
- **`assert_url_safe` (SSRF guard) does real DNS + blocks loopback/private.** It
  calls `socket.getaddrinfo` and rejects loopback/private/link-local hosts unless
  `ALLOW_LOCAL_SERVICES=1`. Putting it on a hot path or in a Pydantic validator
  makes tests that use fake hostnames (`*.example`) or `localhost` URLs fail or
  stall. Validate URL changes at change-time, not on every read. (`.example`
  fails fast with `gaierror`, so a *hang* is heavy-dep loading, not DNS.)
- **Python: CI pins 3.11.** On a newer local interpreter (e.g. 3.14) some
  backend tests fail with `RuntimeError: There is no current event loop` — the
  test harness uses the removed implicit-event-loop pattern. These are **false
  failures from the local interpreter, not regressions**. Prefer a 3.11 venv;
  the `ruff check .` lint gate and CI-targeted tests are what matter.
- **Backend conftest.py fixtures (post-PR #215):** 4 autouse fixtures now run for
  every test — CSRF bypass (CSRF-naive modules), rate-limiter reset, SQLite
  pool reset (clears the singleton pool between tests), and bcrypt cache
  (pre-computes the bcrypt hash for 'pass123' once per session). If a new test
  hangs in CI on what looks like a pool or bcrypt issue, check whether the test
  relies on the pool or auth_service in a way that the fixtures don't handle.
  Pattern reference: `tests/conftest.py`.
- **Frontend jsdom gotchas** (router context for `<Link>`, driving Radix
  `Select`, virtualized lists): see `references/frontend-testing-gotchas.md`
  for the repo's established mock patterns before improvising.

### Cross-platform evidence-write fallback

Some automated QA gates write evidence to `.swarm/evidence/`; on Windows these
writes can fail with "parent directory already contains a .swarm/ folder" or
similar path errors. When this happens, the local run is not a valid CI signal;
do not treat the gate failure as a code defect.

Fallback protocol:
1. Run `ruff check .` (backend) or `npm run lint` (frontend) manually.
2. Run the targeted pytest / vitest commands manually.
3. If those pass, the code is likely CI-compatible; note the evidence-write
   failure in the PR description or a comment.

Keep using path-safe operations (e.g., `pathlib.Path`) in any code you write;
this guidance is about handling pre-existing tooling path issues, not excusing
sloppy paths.

## Output

Classify each risk as:

- `BLOCKER`: likely CI failure or invalid workflow.
- `RISK`: plausible CI instability requiring targeted validation.
- `NOTE`: useful context, not blocking.

Include the exact workflow step or command for every item.
