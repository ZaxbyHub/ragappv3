# Quality gates that can fail (issue #565 / Workstream K PR 3 of 7)

## What changed

- **Warnings-as-errors on every pytest run.** `backend/pyproject.toml` now
  carries `addopts = "--strict-markers -W error::RuntimeWarning"` and
  `filterwarnings = ["error::RuntimeWarning"]`. An in-band RuntimeWarning
  fails the suite instead of scrolling past. (`--strict-markers` and the
  `live` marker registration themselves already landed with #563.)
- **Async-defect-class guards** in `backend/tests/conftest.py`:
  - an AST collection guard fails collection when an `async def test_*`
    method sits inside a plain `unittest.TestCase` subclass — asyncio auto
    mode silently drops those (zero assertions run); `IsolatedAsyncioTestCase`
    subclasses stay exempt;
  - a session-finish guard fails the run when an uncaptured
    "coroutine ... was never awaited" warning was recorded (direct
    RuntimeWarning, or the `PytestUnraisableExceptionWarning` wrapper that
    `-W error::RuntimeWarning` produces on the unraisable path).
- **41 dead tests resurrected.** `backend/tests/test_vector_store_async.py`
  held 41 async test methods inside plain `TestCase` classes that had never
  executed (the guard caught them on its first run). The base class is now
  `unittest.IsolatedAsyncioTestCase`; the tests run for real, and 8 of them
  were repaired against the current VectorStore contracts (records now require
  `vault_id`; `validate_schema` reports identity mismatch in its result dict
  instead of raising; the metadata-only row fetch uses the synchronous
  `table.query()` builder; the basic-search test pins the dense flat scan with
  multi-scale indexing off, since the default multi-scale sizes do not include
  the test table's dimension).
- **Randomized test order.** `pytest-randomly` joins `requirements-dev.txt`;
  every pytest run shuffles collection order (seed printed in the pytest
  header; reproduce a failure with `--randomly-seed=<n>`).
- **Committed bite test** `backend/tests/test_issue565_gate_bites.py` proves
  the gates fail on the C13 defect classes and that the identical shapes pass
  without the gates.
- **Nightly quality-gates workflow** `.github/workflows/nightly-quality-gates.yml`
  (name and job/artifact names deliberately disjoint from the parser-fidelity
  "Nightly" workflow, #258 ENH-007):
  - `mutation-backend` — mutmut 3.7 over the seven guard modules with a
    per-module mutation-score report and a survival budget
    (`MUTATION_SCORE_FLOOR`, recorded in the workflow); `workflow_dispatch`
    accepts a PR number to scope the run to guard modules touched by that
    PR's diff;
  - `schemathesis-api` — Schemathesis 4.x against `/openapi.json` with
    stateful links, against a live test instance;
  - `stryker-frontend` — StrykerJS (Vitest runner) scoped to
    `frontend/src/lib/api` and `frontend/src/hooks`.
- `scripts/mutation_score.py` — per-module score table + fail-closed floor
  enforcement (an unparseable stats surface fails the job).
- `frontend/stryker.config.json` + `npm run test:mutation`.

## Baseline provenance

- Stryker baseline: partial local observation (node 22.14.0 / committed package-lock /
  committed Stryker 9.6.1, scope `src/lib/api` + `src/hooks`): 1024/3481 mutants tested with
  15 survived (>98% killed) before the run was abandoned — the full measurement (~3h on this
  box) is deferred to the first ubuntu nightly run; `thresholds.break` stays 0 (report-only)
  until that baseline lands, then is raised to baseline-minus-headroom. baseline: >98% killed
  on the first 1024 mutants (partial; full score from first nightly run).
- Schemathesis baseline: full-surface first run captured 211 known conformance signatures into
  `backend/schemathesis-baseline.json` (the pre-existing API conformance debt predates this
  gate); the nightly fails on any NEW signature.
- mutmut: 3.x requires fork support and cannot run on Windows dev boxes
  (upstream boxed/mutmut#397); the first ubuntu nightly run establishes the
  per-module baseline, and the workflow's initial conservative floor is raised
  to baseline-minus-headroom by a follow-up PR.

## Operator notes

- All three nightly jobs are additive and scheduled; per-PR CI is unaffected
  except that every pytest run (local, CI, nightly) now carries the strict
  warning flags and randomized order.
- Rollback: delete the workflow file, revert the `pyproject.toml` /
  `tests/conftest.py` / `requirements-dev.txt` hunks, and drop the Stryker
  devDependencies.
