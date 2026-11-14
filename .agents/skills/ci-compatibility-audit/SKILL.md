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
- subpath build with `VITE_APP_BASENAME=/knowledgevault` and `VITE_API_URL=/knowledgevault/api` (under Windows Git Bash, prefix with `MSYS_NO_PATHCONV=1` — see the note in Local Mirror Commands)

Backend job:

- `cd backend`
- `pip install uv==0.12.15` (the lock compiler CI pins)
- `Verify lockfiles (universal regeneration)`: for each of
  `requirements.txt→requirements-lock.txt` and
  `requirements-ci.txt→requirements-lock-ci.txt`, seed a scratch copy of the
  committed lock, re-resolve with
  `uv pip compile --universal --generate-hashes --no-strip-extras --no-header --python-version 3.11`,
  and byte-compare (`git diff --no-index --exit-code`) — run from the REPO
  ROOT so the `# via` annotations match (see `just backend-verify-locks`)
- `pip install --require-hashes -r requirements-lock-ci.txt` (universal lock;
  excludes the stubbed heavy packages — `lancedb`, `pyarrow` come from the
  lock, `unstructured[all-docs]`/`sentence-transformers` are stubbed at test
  time, see caveats below)
- `pip install -r requirements-dev.txt`
- `pip install pytest-xdist`
- `ruff check .`
- `pytest --tb=short -v --timeout=300 --timeout-method=thread -rs -n auto --cov --cov-report=term-missing tests/` — **full backend suite (9,866 tests in the recorded 2026-10-28 Backend run; FR-4 made it the full suite)**.
  Job timeout is **60m** (raised from 30m in PR #215 to accommodate the 3918-test full suite + coverage step on slow CI Linux runners; local baseline is ~3-5m, CI is ~18m, plus coverage runs another ~18m).
  `--timeout=300` caps per-test hangs at 5 min. `--timeout-method=thread`
  (issue #1021) makes that cap actually fire on the demonstrated hang classes:
  the thread method covers both committed canary wedges — the old POSIX-default
  signal method cannot fire during a GIL-releasing C-block call, while it DOES
  interrupt starved-pool joins; a GIL-held hang starves both methods and is the
  disclosed residual class. The dedicated canary step re-proves this every CI
  run. The conftest.py has 4 fixtures (CSRF bypass, rate-limiter reset, SQLite pool reset, bcrypt cache for 'pass123' test password) that the test suite relies on.

Repository contract job:

- `python scripts/check_runtime_contract.py` — fails if any runtime surface
  (Dockerfiles, `ci.yml` setup steps, `frontend/package.json` engines,
  `CONTRIBUTING.md`, the devcontainer, and the `docs/engineering/` docs
  surfaces) disagrees with the `ALLOWED_RUNTIME` table, or if a gated doc's
  CI job/script inventory names fewer jobs or scripts than `ci.yml` defines
  (issue #655).
- `python scripts/check_config_contract.py`
- `python scripts/check_g06_setup_docs.py --check all` — `.env.example`/setup-doc
  drift gate (issue #738): no active localhost `REDIS_URL`, `cp` sources tracked,
  template `ALLOWED_EXTENSIONS` covers the code default, `DATA_DIR` consistent with
  compose, `DOCUMENT_PARSING_STRATEGY` matches or names the code default, and
  CONTRIBUTING's env step names `backend/.env`.
- `python scripts/check_pr_scope_drift.py`
- `python scripts/check_sast_baseline.py` — fails if the SAST baseline grew on
  the PR (suppresses new findings; intentional growth needs
  `SAST_ALLOW_BASELINE_EXPANSION=1` with per-ID justification), fails if the
  `sast` CI job no longer runs `run_bandit.py`, or if the scan scope
  (`TARGET`) drifted from `backend/app`.
- `python scripts/check_secretscan.py` — fails if `.secretscanignore` has
  unparseable globs, adversarial positive samples are not ignored, or
  adversarial negative samples are over-matched. Stale and overly-broad globs
  emit advisory (non-fatal) warnings.
- `python scripts/check_test_collection_scope.py` — fails if a pytest test
  file (`test_*.py` / `*_test.py`) exists outside `backend/tests/`, where
  `pytest tests/` would never collect it (issue #563 / C11).
- `python scripts/check_settings_consumers.py` — fails if a settings field
  loses its last consumer (an unwired setting), a consumer reads an unknown
  field, or the documented settings-consumer contract drifts (issue #662).
- `python scripts/check_batch_mock_seq.py` — fails if a batch-save mock
  sequence drifts from the recorded call-shape contract.
- `python scripts/check_a04_http500_detail_hygiene.py` — fails if any audited
  route module (memories, vaults, admin, draft_room) builds a **5xx-capable**
  `detail` from a raw exception (f-string interpolation or `str(exc)`; issues
  #686 / #917 / #1119). A literal status >= 500 flags directly (whole-tree
  literal pass: class bodies, decorators, default args, module level, and
  function bodies — sync and async); a status computed at runtime flags when
  inline, time-ordered per-scope analysis can resolve it to a value-set where
  ANY value is >= 500 (int literals, plain and annotated assignments, mapping
  dicts and `.get(key, DEFAULT)` defaults — module-level AND function-local;
  if/else branches merge by union; with/async-with assignments bind; loop
  bodies are merged as possible bindings via a state fixpoint that resolves
  var-to-var chains; try-body bindings are visible to except-handler echoes) —
  a dominating `if NAME >= 500: return/raise` guard proves the fall-through
  < 500 until rebinding (strict `>` guards do not refine: 500 stays
  reachable). Unresolvable statuses (function parameters and other
  unmodeled expressions) stay outside the detection class; the guarded real
  Draft Room mapper arms are pinned by
  `backend/tests/test_a04_draftroom_map_helpers.py`, and the analyzer's
  mutation shapes by `backend/tests/test_a04_variable_status_echo_impl.py`.
- `python scripts/check_b03_upload_migration_timeout.py` — fails if the
  upload-migration timeout contract drifts from the documented wiring.
- `python scripts/check_l05_raw_palette.py` — fails if the frontend exceeds
  its raw-palette **budget** (10 occurrences, plus an allowlist); the ESLint
  `local/no-raw-palette` rule is the zero-tolerance companion (issue L05).
- `python scripts/check_l05_page_headers.py` — fails if a page owns its own
  `<h1>` header instead of leaving the page header to PageShell (issue #776 /
  L05).
- `python scripts/check_l07_a11y_gate_wired.py` — fails if the a11y-matrix CI
  job stops running the a11y spec or waives an axe rule silently (issue #778).
- `python scripts/check_l09_skill_trigger_paths.py` — fails if a skill's
  trigger globs drift from the generated skill tree contract (issue L09).
- `python scripts/check_l09_index_assets.py` — fails if a `%BASE_URL%` asset
  link in `frontend/index.html` has no matching file under `frontend/public/`
  (issue #780 / L09).
- `python scripts/check_l09_memory_response_fields.py` — fails if the
  TypeScript `AddMemoryResponse` declares fields the backend
  `MemoryResponse` never sends (issue #780 / L09).
- `python scripts/check_n05_draftclaim_nullable.py` — fails if the backend
  DraftClaim/DraftClaimSource `Optional` fields and the hand-maintained
  TypeScript `| null` mirror drift apart in either direction (issue #789 /
  #912).

SAST job:

- `pip install -r backend/requirements-dev.txt` (gets `bandit`)
- `python scripts/run_bandit.py` — runs bandit against the committed baseline at
  `backend/security/bandit-baseline.json` and **fails only on NEW findings**
  (pre-existing findings are suppressed by the baseline). See
  `backend/security/README.md` for the suppressed-finding counts and the
  baseline-regeneration workflow (`python scripts/run_bandit.py --update-baseline`).

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
- SAST: if `backend/app` changed, run `python scripts/run_bandit.py` locally.
  If it reports NEW findings, either fix them or (if acceptable pre-existing
  debt) regenerate the baseline with `--update-baseline` and justify the
  newly-suppressed finding IDs in the PR.
- Regression falsifiability: if the change adds a regression test, was it
  verified falsifiable (revert the fix, confirm the test fails, restore)?
- For the 60m job timeout: tests with `pytest-timeout=300` per-test are bounded, but the cumulative suite (~36m with coverage) MUST fit. If you add tests that take cumulatively >20m, the job will fail. Profile slow tests with `pytest --durations=20`.

## Local Mirror Commands

The contract-script block below mirrors the `ci.yml` **quality-contracts**
job — every `python scripts/check_*.py` line in the fence is one of that
job's steps and every one of the job's steps appears here, in both
directions: adding a `scripts/check_*.py` step to `ci.yml` (or removing one)
fails `backend/tests/test_ci_compatibility_audit_mirror.py` until this list
matches, so a local pre-push audit can never silently under-run Quality
contracts (issue #914). The fence also carries the frontend/backend build and
test commands and `run_bandit.py`; the parity contract covers the
`check_*.py` contract scripts, not those other commands.

```bash
cd frontend && npm ci --engine-strict && npm run typecheck && npm run lint
cd frontend && npm test -- src/lib/api.test.ts src/lib/api.csrf.test.ts src/lib/api.sse.test.ts src/pages/WikiPage.sse.test.tsx src/stores/useAuthStore.api-base.test.ts
cd frontend && npm test && npm run build
cd frontend && VITE_APP_BASENAME=/knowledgevault VITE_API_URL=/knowledgevault/api npm run build
# ^ On Windows/Git Bash, prefix subpath builds with MSYS_NO_PATHCONV=1 —
#   MSYS rewrites the leading-slash env values into Windows paths before
#   Node sees them, and the build fails with
#   "Base path contains unsafe characters: <the rewritten value>" (issue #567).
#   On PowerShell and on Linux no prefix is needed. The repo justfile handles
#   this automatically via `just frontend-build-subpath`.
cd backend && ruff check . && pytest --tb=short -v --timeout=300 --timeout-method=thread tests/
python scripts/check_runtime_contract.py
python scripts/check_config_contract.py
python scripts/check_g06_setup_docs.py --check all
python scripts/check_settings_consumers.py
python scripts/check_batch_mock_seq.py
python scripts/check_pr_scope_drift.py
python scripts/check_sast_baseline.py
python scripts/check_secretscan.py
python scripts/check_test_collection_scope.py
python scripts/check_a04_http500_detail_hygiene.py
python scripts/check_b03_upload_migration_timeout.py
python scripts/check_l05_raw_palette.py
python scripts/check_l05_page_headers.py
python scripts/check_l07_a11y_gate_wired.py
python scripts/check_l09_skill_trigger_paths.py
python scripts/check_l09_index_assets.py
python scripts/check_l09_memory_response_fields.py
python scripts/check_n05_draftclaim_nullable.py
python scripts/run_bandit.py
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

## Backend CI flake signature table (issue #1101)

A red Backend job is not always a regression. Before touching code, match the
failing log against the five documented ragappv3 Backend flake families below
(signatures quoted from the documented incidents — PR #642, the C32
occurrences, issue #943, the 2026-10-23 freeze, and issue #1021 / PR #1095
run 42448434901 — so classification no longer depends on out-of-repo memory).

| Family | Log signature | Diff-foreign discriminator |
|---|---|---|
| xdist sqlite moving-set | `sqlite3.OperationalError: unable to open database file` raised via `get_connection` in `backend/app/models/database.py` (e.g. under `require_health_probe_auth` in `backend/app/api/deps.py`); a MOVING subset of health-auth/retry tests fails, all on ONE xdist worker (PR #642: 5 failures on gw1, rerun 1 had 2 DIFFERENT failures on gw2, rerun 2 green — zero code change). | The failed-test SET differs between reruns and never repeats on one worker, and the diff touches no `backend/app` runtime surface (PR #642 was skills/docs-only). |
| C32 embedding-reuse `main()` exit | `backend/tests/issue513_checks/test_c32_embedding_reuse_enrichment_retry.py` fails at `assert main() == 0` with `C32 CHECK: FAIL: enrichment retry re-embedded N unchanged chunk text(s) ...` — a hermetic-check exit failure with no app-code traceback (mechanism: an inherited closed embedding-cache connection, documented by the file's own `test_c32_resets_inherited_closed_embedding_cache_connection`). | The byte-identical failure reproduces on a master commit whose diff touches no application code (3rd documented occurrence reproduced on a lockfile-only master commit). |
| secretscan rglob-during-coverage-walk (#943) | `scripts/check_secretscan.py` crashes with a `FileNotFoundError` traceback from inside its repo-wide `Path.rglob` walk — a transient directory (coverage artifacts written under the repo root mid-suite) vanished between the parent listing and the recursion. | The diff touches no secretscan/coverage wiring, and the crash coincides with a coverage step writing transient artifacts under the repo root. Known producers are already closed: vitest coverage confined via `--coverage.reportsDirectory` (`backend/tests/test_issue258_coverage_gate.py`) and the walks made vanish-tolerant (`backend/tests/test_943_secretscan_walk_robustness.py`). |
| Backend whole-job freeze, empty live log | The job dies at exactly its `timeout-minutes: 60` with ALL xdist workers silent mid-suite (~14 min of green progress at 94-98% passed) and NO pytest-timeout markers in the log. Absent TIMEOUT markers are the tell: the wait is C-level (socket/DNS/pipe), uninterruptible by pytest-timeout. 2026-10-23 incident: master froze identically 4 minutes later on a tree without the PR. | `gh run view <master-run> --job <backend-job> --log` shows the same silent cutoff at the same wall-clock on master (compare last-PASSED timestamps and percentages across trees), and the local full suite with `--timeout-method=thread --no-cov` completes. |
| #1021 hang-class canary timeout | The env-gated canary step (`RUN_CI_HANG_CANARY=1`) fails `backend/tests/test_ci_hang_canary.py::test_wedge_starved_pool_join_aborted_by_configured_timeout` with `Failed: Timeout (>30.0s) from pytest-timeout` and a crashed worker (PR #1095 feedback push, run 42448434901; cleared by `gh run rerun --failed` with zero code change). | The diff touches neither `backend/tests/test_ci_hang_canary.py` nor its canary step in `ci.yml`; the canary is a deliberately committed wedge whose timely abort depends on runner scheduling. |

### Classification protocol (runs before the rerun decision)

A red Backend job may be reclassified as diff-foreign — and rerun — ONLY after
BOTH discriminator checks pass:

1. **Compare the failed-test SET across reruns** (or against master's run at
   the same wall-clock). A non-repeating, moving set of failures — often
   localized to one xdist worker — is infra, not a regression. A set that
   repeats identically on rerun is a real failure: diagnose it (see the
   `ci-fix-monitor` skill).
2. **Confirm the diff touches none of the failing surface.** Check the PR diff
   against every failing test's file and the code it exercises: a
   skills/docs-only diff cannot regress `backend/app` runtime, and a
   byte-identical failure on an application-code-free master commit proves
   pre-existence.

Only then run `gh run rerun <run-id> --failed` (a fresh runner) and expect
green. Never chase the named tests of a moving-set family, and never rerun a
failure whose set repeated identically — that is a regression wearing a flake
costume.

## Output

Classify each risk as:

- `BLOCKER`: likely CI failure or invalid workflow.
- `RISK`: plausible CI instability requiring targeted validation.
- `NOTE`: useful context, not blocking.

Include the exact workflow step or command for every item.

## Nightly quality gates (issue #565 / E06) and strict pytest flags

Second scheduled workflow: `.github/workflows/nightly-quality-gates.yml`
(name "Nightly quality gates"; distinct from the parser-fidelity workflow
"Nightly" / job `full-deps` / artifact `parser-bakeoff` in nightly.yml — issue
#258 ENH-007 owns that tier; keep the names disjoint). Jobs:

- `mutation-backend`: mutmut 3.7 over the eight guard modules
  (`backend/app/security/__init__.py`,
  `backend/app/security/recovery_routes.py`, `backend/app/api/deps.py`,
  `backend/app/middleware/{logging,maintenance,telemetry_span}.py`,
  `backend/app/models/database.py`, `backend/app/services/maintenance.py`),
  per-module stats via `mutmut export-cicd-stats`, and a per-module score
  report enforced by `scripts/mutation_score.py` against the
  `MUTATION_SCORE_FLOOR` literal in the workflow. `workflow_dispatch` with a
  `pr_number` input scopes the run to guard modules touched by that PR's diff
  (GLM EP1 PR-gated shape). mutmut 3.x needs fork support — it cannot run on
  native Windows (upstream boxed/mutmut#397); reproduce locally only under
  WSL.
- `schemathesis-api`: boots a live `app.main:app` test instance (uvicorn,
  dummy secrets, `REDIS_URL=""`, `ALLOW_LOCAL_SERVICES=1`, provider endpoints
  overridden to an unresolvable-but-allowed local port, `RAGAPP_CSRF_TEST_BYPASS=1`
  + `PYTEST_CURRENT_TEST=nightly-schemathesis` sentinel narrowing CSRF on the
  disposable instance) and runs Schemathesis 4.x against `/openapi.json` with
  `--phases fuzzing,stateful` and an admin-token bearer header.
- `stryker-frontend`: `npm run test:mutation` (StrykerJS + Vitest runner,
  `frontend/stryker.config.json` scoped to `src/lib/api/**` and
  `src/hooks/**`, fail-below `thresholds.break`).

Strict pytest flags (issue #565) apply to EVERY pytest invocation, local and
CI, because they live in `backend/pyproject.toml`:

- `addopts = "--strict-markers -W error::RuntimeWarning"` — an in-band
  RuntimeWarning now fails the suite.
- `filterwarnings = ["error::RuntimeWarning"]` — ini mirror of the flag.
- `backend/tests/conftest.py` issue-#565 guards: (1) an AST collection guard
  fails collection on `async def test_*` methods inside plain
  `unittest.TestCase` subclasses (asyncio auto mode silently drops those —
  IsolatedAsyncioTestCase subclasses are exempt); (2) a session-finish guard
  fails the run on any uncaptured "coroutine ... was never awaited" warning
  (direct RuntimeWarning or the PytestUnraisableExceptionWarning wrapper that
  `-W error::RuntimeWarning` produces via the unraisable path). Warnings
  captured inside a test's own `catch_warnings(record=True)` block never
  reach the guard.
- `pytest-randomly` (in `requirements-dev.txt`) shuffles test order every
  run; a failure that appears/disappears with order is a real pollution bug —
  fix the test, never bypass the plugin. Reproduce with a fixed
  `--randomly-seed=<n>` (the seed is printed in the pytest header).

Local mirror additions:

```bash
cd frontend && npm run test:mutation   # stryker (minutes; scoped to lib/api + hooks)
cd backend && python -m pytest tests/  # inherits strict flags + random order
```
