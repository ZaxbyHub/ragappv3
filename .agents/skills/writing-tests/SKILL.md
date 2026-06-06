---
name: writing-tests
description: >
  Apply when writing tests, modifying test files, fixing test failures, debugging CI failures,
  adding test coverage, creating adversarial tests, or reviewing any file under tests/.
  Also apply when implementing features or fixes that require corresponding test changes.
  Enforces framework-specific rules (RAGAPPv3 frontend uses Vitest/npm; bun:test
  guidance below applies only to opencode-swarm/similar projects), unittest/pytest
  patterns, mock isolation, cross-platform compatibility, and CI pipeline awareness.
  Load this skill before touching any test file.
effort: medium
---

# Writing Tests

This skill covers multiple test ecosystems found in this workspace:

- **TypeScript (bun:test)** — used by opencode-swarm and similar projects
- **Python (unittest/pytest)** — used by ragappv3 backend and similar projects

RAGAPPv3 frontend is an npm/Vite/Vitest project under `frontend/`, not Bun. For RAGAPPv3 frontend work, follow the repo's actual `frontend/package.json` scripts: `npm run typecheck`, `npm run lint`, `npm test`, and `npm run build`. Use Vitest conventions already present in `frontend/src/**/*.test.ts*`.

RAGAPPv3 CI also runs repository quality contract scripts from the repo root: `python scripts/check_config_contract.py` and `python scripts/check_pr_scope_drift.py`.

Load the relevant section based on the test file's framework. For this repo's
frontend, use Vitest/npm even if generic TypeScript guidance below mentions Bun.
Check for `import unittest` / `import pytest` (Python) vs the actual frontend
Vitest imports or opencode-swarm `bun:test` imports.

---

# Part 1: TypeScript — bun:test

All test files MUST import from `bun:test`:

```typescript
import { describe, test, expect, beforeEach, afterEach } from 'bun:test';
```

Bun provides a vitest compatibility layer (`vi.mock`, `vi.fn`, `vi.spyOn`) that works on Linux and macOS. However, `vi.mock()` has critical isolation bugs in Bun when multiple test directories run in the same process. Prefer `bun:test` native APIs:

| vitest API | bun:test equivalent | Notes |
|-----------|-------------------|-------|
| `vi.fn()` | `mock(() => ...)` | Import `mock` from `bun:test` |
| `vi.spyOn(obj, method)` | `spyOn(obj, method)` | Import `spyOn` from `bun:test` |
| `vi.mock('module', factory)` | `mock.module('module', factory)` | Import `mock` from `bun:test` |
| `vi.restoreAllMocks()` | `mock.restore()` | Call in `afterEach` |

## Mock Isolation Rules

**CRITICAL: Module-level mocks leak across test files within the same Bun process.**

The CI pipeline runs test directories in groups. All files in a group share one Bun process and one module cache. A `vi.mock()` or `mock.module()` call in file A replaces the module for file B if they run in the same group.

### Rules

1. **Never mock a module that another test file in the same CI group imports directly.** If `tests/unit/cli/run-dispatch.test.ts` mocks `../../src/commands/agents.js`, then `tests/unit/commands/agents.test.ts` (in the same group) will get the mock instead of the real module.

2. **If you must use module-level mocks, isolate the test in its own CI step** or use dependency injection instead of module replacement.

3. **Never create circular mock imports.** This pattern deadlocks Bun:
```typescript
// BROKEN — imports from the module it's about to mock
import { realFn } from '../../src/module.js';
vi.mock('../../src/module.js', () => ({
  realFn: (...args) => realFn(...args),  // circular!
  otherFn: vi.fn(),
}));
```
Instead, inline the function logic or extract the real functions into a separate utility module.

4. **Prefer constructor/parameter injection over module mocking.** The swarm's hook factories (`createScopeGuardHook`, `createDelegationLedgerHook`, etc.) accept injected dependencies — test them by passing mock callbacks, not by replacing modules.

## CI Pipeline Structure

The CI runs on three platforms (ubuntu, macos, windows). Tests are split into sequential steps within each platform's job.

**Per-file isolation:** Each test file runs in its own Bun process via `for f in dir/*.test.ts; do bun --smol test "$f"; done`. This prevents module cache poisoning between files within the same step.

**Cascade termination:** Each step uses `exit $failed` — the first failing step terminates the entire platform's job. This means failures in later steps are hidden until earlier steps pass. When fixing Windows issues, expect to peel back layers: fixing Step 4 may reveal a failure in Step 5 that was previously hidden.

```
Step 1: hooks - guardrails            (Linux/macOS only, skipped on Windows)
Step 2: hooks - knowledge             (Linux/macOS only, skipped on Windows)
Step 3: hooks - system-enhancer       (Linux/macOS only, skipped on Windows)
Step 4: hooks - delegation + others   (Linux/macOS only, skipped on Windows)
Step 5: commands + config             (all platforms)
Step 6: cli                           (all platforms)
Step 7: tools                         (all platforms)
Step 8: services + build + quality + sast + sbom + scripts  (all platforms)
Step 9: state + agents + knowledge + evidence + plan + misc (all platforms)
```

When writing a test, know which step your file will run in. Do not assume isolation from other files in the same step.

**Job timeout: 15 minutes.** A single hanging test will kill the entire platform's test run.

## File Placement

### Convention

| Test type | Location | When to use |
|-----------|----------|-------------|
| Unit tests for `src/hooks/*.ts` | `tests/unit/hooks/` | Testing hook factories and hook behavior |
| Unit tests for `src/tools/*.ts` | `tests/unit/tools/` | Testing tool execute functions |
| Unit tests for `src/commands/*.ts` | `tests/unit/commands/` | Testing CLI command handlers |
| Unit tests for `src/config/*.ts` | `tests/unit/config/` | Testing schema validation, config loading |
| Unit tests for `src/agents/*.ts` | `tests/unit/agents/` | Testing agent prompt generation, factory logic |
| Colocated tests | `src/**/*.test.ts` | Integration-style tests tightly coupled to the source module |
| Integration tests | `tests/integration/` | Cross-module workflows, plugin initialization |
| Security tests | `tests/security/` | Adversarial input handling, injection resistance |
| Smoke tests | `tests/smoke/` | Built package validation |

### Naming

- Base test: `<module>.test.ts`
- Adversarial variant: `<module>.adversarial.test.ts`

Only create an adversarial variant if it tests **distinct attack vectors** not covered by the base test. Do not duplicate base test assertions with different inputs — that's redundancy, not security coverage.

### Regression tests (review-surfaced bugs)

When fixing a bug surfaced by code review, swarm review, or post-merge audit, **always add a regression test** with the following shape so the test's purpose survives future cleanup:

```typescript
describe('<feature> — regression: <one-line description> (F#)', () => {
  it('<exact behavior the bug violated>', () => {
    // Previous code did <bad thing>: e.g. the regex `/^\.\/+/` only stripped
    // a single leading `./`, so `././util.ts` survived as `./util.ts`.
    expect(normalizeGraphPath('././util.ts')).toBe('util.ts');
  });
});
```

Rules:
- The describe label includes the original finding ID (e.g. `F8`, `F9`, `F1.1`) so future readers can map back to the review.
- The leading comment in the body explains the **prior buggy behavior** in concrete terms — what the code did before, not what it does now.
- One regression test per finding. Do not pile unrelated assertions into a single regression block.

Examples in-tree: `tests/unit/graph/graph-query.test.ts` (`normalizeGraphPath — regression (F8)`, `getBlastRadius — regression: depthReached (F9)`), `tests/unit/graph/import-extractor.test.ts` (`paren-preceded strings (F1)`, `member-expression require/import (F1.1)`).

## Test Quality Standards

### DO

- Test real behavior: call the actual function with real inputs, assert on real outputs.
- Test error paths: what happens with `null`, `undefined`, empty string, oversized input?
- Use temp directories (`fs.mkdtemp`) for file I/O tests. Clean up in `afterEach`.
- Assert on specific values, not just truthiness: `expect(result.status).toBe('pending')` not `expect(result).toBeTruthy()`.
- **Verify regression tests are non-vacuous:** before committing, stash or revert ONLY the source fix (leave the new test in place), run it, and confirm it fails with the original bug. Restore the fix and confirm it passes. A test that passes on both fixed and unfixed code is not a regression guard.

### DO NOT

- **Do not test type definitions.** `expect(event.type === 'foo').toBe(true)` tests TypeScript, not your code.
- **Do not test framework behavior.** "Zod schema parses valid input" tests Zod, not your schema.
- **Do not test test utilities.** If it only exists to support other tests, it doesn't need its own test.
- **Do not mock everything.** If every dependency is mocked, you're testing the mock setup. Prefer real dependencies for pure functions and only mock I/O boundaries (filesystem, network, timers).
- **Do not hardcode version numbers.** Version bumps are automated — a test asserting `version === '6.31.3'` breaks on every release.
- **Do not use `sleep` or `setTimeout` for synchronization.** Use explicit signals, resolved promises, or `Bun.sleep()` with tight bounds.
- **Do not spawn `cat /dev/zero`, `yes`, or other infinite-output commands.** Use `sleep 30` for "blocking command" tests.

## Cross-Entry Invariants (config maps)

When you modify any entry of a "map of agents/tools/roles" in `src/config/constants.ts` (`AGENT_TOOL_MAP`, `DEFAULT_MODELS`, `QA_AGENTS`, `PIPELINE_AGENTS`, etc.), there are tests that assert **parity across sibling entries**, not just shape of one entry.

Known parity assertions:

| Test | Invariant |
|---|---|
| `tests/unit/config/critic-registration.test.ts:67` | `AGENT_TOOL_MAP.critic_sounding_board.length === AGENT_TOOL_MAP.critic.length` |
| `tests/unit/config/agent-tool-map.test.ts:26` | `AGENT_TOOL_MAP.architect.length` is strictly greater than every other agent's |
| `tests/unit/config/agent-tool-map.test.ts:34` | every subagent's tool list `<= 20` entries |
| `tests/unit/config/constants.test.ts:48` | `ALL_SUBAGENT_NAMES.length === 13` |
| `tests/unit/config/constants.test.ts:137` | `Object.keys(DEFAULT_MODELS).length === 14` |

Workflow when adding a tool to a single agent:
1. Add the entry.
2. Run `bun --smol test tests/unit/config --timeout 60000` **before pushing**.
3. If a parity test fails, decide: mirror the change to sibling agents (most common — see this PR's `repo_map` mirrored to `critic_sounding_board` + `critic_drift_verifier`), or update the invariant test if the design intent has actually changed.
4. To inspect runtime shape quickly: `bun -e "import { AGENT_TOOL_MAP } from './src/config/constants.ts'; for (const [k,v] of Object.entries(AGENT_TOOL_MAP)) console.log(k, v.length);"`

Do **not** push a constants change to CI without running the config test directory locally — these failures cascade through the per-OS unit jobs and waste minutes per push.

## Cross-Platform Requirements

All tests must pass on Linux, macOS, and Windows unless explicitly gated.

### Skipping tests on specific platforms

Use the `skipIf` chaining pattern:
```typescript
// Skip on Windows only
test.skipIf(process.platform === 'win32')('test name', async () => { ... });

// Skip on non-Linux (use when test relies on Linux-specific behavior)
test.skipIf(process.platform !== 'linux')('test name', async () => { ... });

// Skip entire describe block
describe.skipIf(process.platform === 'win32')('group name', () => { ... });
```

### Temp directories and path handling
- Use `path.join()` or `path.resolve()`, never string concatenation with `/`.
- Temp directories: use `os.tmpdir()`, never hardcoded `/tmp`.
- **CRITICAL: Wrap `mkdtempSync` with `realpathSync` when using `process.chdir`:**
  ```typescript
  // WRONG — on macOS, /tmp is a symlink to /private/tmp.
  // mkdtempSync returns /tmp/... but process.cwd() resolves to /private/tmp/...
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-'));
  process.chdir(tempDir);
  // process.cwd() !== tempDir on macOS!

  // CORRECT — resolve symlinks first
  const tempDir = fs.realpathSync(
    fs.mkdtempSync(path.join(os.tmpdir(), 'test-')),
  );
  process.chdir(tempDir);
  ```
- File comparisons: normalize paths before comparing (`path.resolve(a) === path.resolve(b)`).

### Permissions (`fs.chmodSync`)
- `chmodSync` is a **no-op for directories** on Windows and unreliable for files.
- Tests that rely on chmod to simulate permission errors should guard with platform checks:
  ```typescript
  if (process.platform !== 'win32') {
    fs.chmodSync(filePath, 0o000);
    // ... test permission error behavior ...
    fs.chmodSync(filePath, 0o644); // restore
  } else {
    // On Windows, skip or use a mock to throw EPERM
  }
  ```
- If the test asserts that the tool handles permission errors **gracefully** (returns success despite write failure), the test may pass on Windows even without chmod — the write just succeeds. Verify this before adding guards.

### Symlinks
- `fs.symlinkSync` requires **administrator or developer mode** on Windows.
- Use a runtime capability check:
  ```typescript
  let canCreateSymlinks = false;
  try {
    const testLink = path.join(tempDir, '.symlink-test');
    fs.symlinkSync(tempDir, testLink);
    fs.unlinkSync(testLink);
    canCreateSymlinks = true;
  } catch {}

  test.skipIf(!canCreateSymlinks)('symlink test', async () => { ... });
  ```

### Process spawning
- Use `.cmd` extension on Windows for npm/bun binaries: `process.platform === 'win32' ? 'bun.cmd' : 'bun'`.
- Use array-form `spawn`/`spawnSync`, never shell string commands.
- **`npx` in empty temp dirs hangs on Windows.** If a test creates a temp directory with a `package.json` (for framework detection) and then calls a tool that spawns `npx vitest run` or similar, the spawn will hang until the test timeout fires. Skip these tests on non-Linux:
  ```typescript
  // Flaky on macOS/Windows: spawns vitest in temp dir without node_modules
  test.skipIf(process.platform !== 'linux')(
    'test that triggers process execution',
    async () => { ... },
    15000,
  );
  ```

### Timestamps
- Avoid comparing strings that embed `new Date().toISOString()`. Two sequential calls can span a millisecond boundary, especially on Windows CI. Strip or normalize volatile timestamps before comparison:
  ```typescript
  const stripTimestamp = (s: string) =>
    s.replace(/Updated: \d{4}-\d{2}-\d{2}T[\d:.]+Z/, 'Updated: <FROZEN>');
  expect(stripTimestamp(output1)).toBe(stripTimestamp(output2));
  ```

## Running TypeScript Tests

> **⚠️ Do NOT use the OpenCode `test_runner` tool to validate the full repo.** It is for targeted agent validation with explicit `files: [...]` or small targeted scopes. `scope: 'all'` requires `allow_full_suite: true` and is intended for opt-in CI mirrors only. Broad scopes can stall or kill OpenCode before the `MAX_SAFE_TEST_FILES = 50` (`src/tools/test-runner.ts:26`) guard fires. For repo validation, use the shell commands below — per-file isolation loops match CI behavior. `allow_full_suite` should be used only when intentional and justified in the PR description. See [`AGENTS.md`](../../../AGENTS.md) invariant 6 for the full contract.

```bash
# Full suite (all platforms)
bun test

# Single file
bun test tests/unit/hooks/scope-guard.test.ts

# Single directory
bun --smol test tests/unit/hooks --timeout 30000

# CI-equivalent run (per-file isolation, matches actual CI behavior)
for f in tests/unit/tools/*.test.ts; do bun --smol test "$f" --timeout 120000; done

# Quick directory run (faster but may have cross-file cache pollution)
bun --smol test tests/unit/cli --timeout 120000
bun --smol test tests/unit/commands tests/unit/config --timeout 120000
```

The `--smol` flag reduces Bun's memory footprint. Use it when running large directories (50+ files).

The `--timeout 120000` flag sets per-test timeout to 120 seconds. Individual tests should complete in under 5 seconds. If a test needs more than 10 seconds, it's doing too much — split it or mock the slow dependency.

**Note:** CI runs each file in its own Bun process (`for f in dir/*.test.ts; do bun --smol test "$f"; done`). Running an entire directory at once (`bun --smol test tests/unit/tools/`) can mask cache-poisoning issues that only appear in CI. When debugging CI failures, test files individually.

## Debugging CI failures

When CI reports a `unit (ubuntu-latest|macos-latest|windows-latest)` failure:

1. **Identify the actual failing test from the job log first.** Do not assume it's a pre-existing failure based on a local repro of a different test. Open the failing job's URL (`https://github.com/<owner>/<repo>/actions/runs/<run-id>/job/<job-id>`) and find the `<file>:<line>` in the Bun output. WebFetch can scrape this if the `gh` CLI isn't available.
2. **Reproduce that exact file locally** with the per-file CI command:
   ```bash
   bun --smol test tests/unit/<dir>/<file>.test.ts --timeout 30000
   ```
3. **Then check if the same failure reproduces on `main`.** If yes, document as pre-existing in the PR description and continue with your branch's work; do not silently inherit the failure.
4. **For dist-check failures:** any change under `src/` that the bundler picks up requires `bun run build` + commit of `dist/` in the same PR. The job compares committed `dist/` against a fresh build.
5. **For matrix-OS-only failures:** check `process.platform` guards, `mkdtempSync` realpath wrapping, chmod guards, symlink capability checks, and `npx`-spawn skips (sections above).

## Before Submitting

1. Run the tests for your changed files: `bun test path/to/your.test.ts`
2. Run the full CI group your tests belong to (see pipeline structure above)
3. Verify no `process.cwd()` usage — use the `directory` parameter from `createSwarmTool` or hook constructor
4. Verify no hardcoded paths (`/tmp/...`, `C:\...`) — use `os.tmpdir()` + `path.join()`
5. Verify mocks are restored in `afterEach` if using `spyOn` or `mock.module`
6. Verify `mkdtempSync` is wrapped with `realpathSync` if you use `process.chdir` on the result
7. Verify `chmodSync` calls are guarded with `process.platform !== 'win32'`
8. Verify symlink creation is guarded or uses a `canCreateSymlinks` capability check
9. Verify no `new Date().toISOString()` in equality assertions — strip volatile timestamps
10. Verify tests that spawn `npx`/`vitest`/`jest` in temp dirs are skipped on non-Linux

---

# Part 2: Python — unittest / pytest

## Framework

This project uses `unittest.TestCase` classes with `pytest` as the runner. All backend test files live under `backend/tests/`.

```python
import unittest

class TestMyFeature(unittest.TestCase):
    def test_something(self):
        self.assertEqual(result, expected)
```

Run with:
```bash
# From project root
python -m pytest backend/tests/test_module.py -v --tb=short

# Specific test class
python -m pytest backend/tests/test_module.py::TestMyClass -v

# Specific test
python -m pytest backend/tests/test_module.py::TestMyClass::test_something -v
```

### Async Test Methods

Test methods that call async functions must be declared `async` and decorated with `@pytest.mark.asyncio`:

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_async_operation(self):
    result = await some_async_function()
    self.assertEqual(result, expected)
```

Without the decorator, pytest will not await the coroutine and the test will fail with "coroutine was never awaited".

Use `AsyncMock` for mocking async functions:
```python
mock_service = AsyncMock()
mock_service.fetch_data.return_value = {"id": 1}
result = await mock_service.fetch_data()
```

This project uses `asyncio_mode = "auto"` in pytest configuration (pyproject.toml), so the decorator is not strictly required — pytest auto-detects async tests. However, explicit decoration is preferred for clarity and for compatibility with older pytest versions.

### Pytest Fixtures

Use `@pytest.fixture` for reusable setup/teardown logic:

```python
@pytest.fixture
def db_connection(tmp_path):
    """Fixture that provides a test database connection."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    yield conn  # test runs here
    conn.close()  # cleanup
```

Fixture scopes:
- `scope="function"` (default): fresh fixture for each test
- `scope="class"`: shared across all tests in a class
- `scope="module"`: shared across all tests in a file
- `scope="session"`: shared across entire test run

### Monkeypatch

Use the `monkeypatch` fixture to mock attributes and environment variables without module-level mocking:

```python
def test_with_monkeypatch(monkeypatch):
    monkeypatch.setattr(some_object, "method", AsyncMock(return_value=42))
    monkeypatch.setenv("API_KEY", "test-key")
    result = some_object.method()
    assert result == 42
```

Monkeypatch automatically restores original values after the test — no manual cleanup needed.

### FastAPI TestClient with Dependency Overrides

To test FastAPI endpoints with custom dependencies (e.g., test database):

```python
from fastapi.testclient import TestClient

def override_get_db():
    conn = sqlite3.connect("test.db")
    try:
        yield conn
    finally:
        conn.close()

app.dependency_overrides[deps.get_db] = override_get_db
client = TestClient(app)
# ... run tests ...
app.dependency_overrides.clear()
```

Always clear `dependency_overrides` after the test to prevent leaks.

### RAGAPPv3: CSRF on mutating endpoints

Most state-mutating RAGAPPv3 routes depend on `csrf_protect`. Route tests don't
reconstruct the cookie/header double-submit — `backend/tests/conftest.py`
auto-bypasses CSRF via the **pytest-only** `RAGAPP_CSRF_TEST_BYPASS` env flag
(honoured by `security.csrf_protect` only when `PYTEST_CURRENT_TEST` is also set,
so it cannot disable CSRF in production). Classification is automatic: a test
**module whose source mentions "csrf"** is left to exercise the *real* validator;
every other module is bypassed, which works for both the shared `app.main` app
and tests that build their own `FastAPI()`. Filename alone is not used for
classification. To test real CSRF enforcement, ensure the module source
references csrf; to just call a protected endpoint, do nothing.

### RAGAPPv3: the CI dependency set is reduced

CI installs only `backend/requirements-ci.txt` + `requirements-dev.txt`, which
**omit** `lancedb`, `pyarrow`, `unstructured`, and `sentence-transformers`
(stubbed per-file at test time). A dev machine with the full `requirements.txt`
can pass a backend test locally that fails in CI at import (`ModuleNotFoundError`).
To validate a backend **test-scope** change (e.g. adding a file to the CI pytest
list) faithfully — and faster, with no multi-GB model loads — reproduce the CI
env rather than trusting the local run:

```bash
python -m venv /tmp/civenv
/tmp/civenv/bin/pip install -r backend/requirements-ci.txt -r backend/requirements-dev.txt
cd backend && /tmp/civenv/bin/python -m pytest -q tests/<candidate_file>.py
```

Corollary: the per-file `lancedb`/`pyarrow`/`unstructured` import stubs are
**load-bearing for CI, not dead boilerplate** — don't remove them without
confirming the file still collects under the reduced set.

## Mock Exception Types MUST Match Production Catch Clauses

**CRITICAL: This is the #1 cause of silent test failures in Python test fixes.**

If production code catches specific exception types:
```python
try:
    risky_operation()
except (OSError, RuntimeError, ValueError):
    handle_failure()
```

The test mock MUST raise one of those types, NOT bare `Exception`:
```python
# WRONG — Exception is NOT caught by the specific handler
mock_obj.method.side_effect = Exception("something failed")
# Result: exception propagates, test sees 500 instead of 200

# CORRECT — OSError IS caught by the handler
mock_obj.method.side_effect = OSError("something failed")
# Result: handler catches it, execution continues, test sees 200
```

When fixing a failing test, **always read the production catch clause first**. Match the mock exception type exactly. The production code's `except` tuple is the authoritative list of exception types the code handles.

## Test Class Organization

### Naming

- Base test: `Test<EndpointOrModule>` (e.g., `TestVaultEndpoints`, `TestAuthRoutes`)
- Adversarial variant: `Test<Feature>Adversarial` (e.g., `TestFetchAccessibleVaultsAdversarial`)
- Scoped tests: `Test<Feature><Scope>` (e.g., `TestVaultScopedRoutes`)

### Database Access

Tests that need direct database access use the pattern:
```python
conn = self._get_db_conn()
try:
    # insert test data
    self._insert_vault(conn, "TestVault")
finally:
    self._connection_pool.release_connection(conn)
```

Always release connections in `finally` — SQLite has a connection limit and unreleased connections can cause "database is locked" errors in subsequent tests.

### Unused Variables

Ruff flags unused variables with F841. If a helper returns a value you don't need, call it without assignment:
```python
# WRONG — triggers F841
vault_id = self._insert_vault(conn, "TestVault")

# CORRECT — bare call, no unused variable
self._insert_vault(conn, "TestVault")
```

## SQL Parameter Counting

When writing SQL queries with parameterized placeholders, count every `?` in the query and bind exactly that many values:
```python
# Count the placeholders: 4 question marks
query = """
    SELECT DISTINCT v.id FROM vaults v WHERE v.id IN (
        SELECT vm.vault_id FROM vault_members vm WHERE vm.user_id = ?  -- 1
        UNION
        SELECT vga.vault_id FROM vault_group_access vga
        JOIN group_members gm ON vga.group_id = gm.group_id
        WHERE gm.user_id = ?                                        -- 2
        UNION
        SELECT v.id FROM vaults v WHERE v.visibility = 'public' AND v.org_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM org_members om WHERE om.org_id = v.org_id AND om.user_id = ?  -- 3
        )
        UNION
        SELECT v.id FROM vaults v WHERE v.visibility = 'org' AND v.org_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM org_members om WHERE om.org_id = v.org_id AND om.user_id = ?  -- 4
        )
    )
"""

# Bind EXACTLY 4 parameters — one per placeholder
cursor.execute(query, (user_id, user_id, user_id, user_id))
```

Mismatch causes `sqlite3.ProgrammingError: Incorrect number of bindings` at runtime.

## Defensive Null Checks in Test Assertions

When verifying that an operation succeeded despite a partial failure (e.g., vault deletion despite vector store error), add a **state verification** assertion beyond just checking the HTTP response:

```python
def test_delete_vault_vector_store_failure_continues(self):
    # ... setup ...
    mock_vector_store.delete_by_vault.side_effect = OSError("vector store down")
    resp = self.client.delete(f"/api/vaults/{vault_id}")

    # 1. HTTP response check
    self.assertEqual(resp.status_code, 200)
    self.assertIn("deleted successfully", resp.json()["message"])

    # 2. State verification — confirm the side effect actually occurred
    resp_check = self.client.get(f"/api/vaults/{vault_id}")
    self.assertEqual(resp_check.status_code, 404)
```

Without the 404 check, the test only verifies the response message — it doesn't verify the DB state changed.

## Running Python Tests

```bash
# From project root — single test file
python -m pytest backend/tests/test_vaults.py -v --tb=short -q

# Single test class
python -m pytest backend/tests/test_vaults.py::TestVaultEndpoints -v

# Single test method
python -m pytest backend/tests/test_vaults.py::TestVaultEndpoints::test_delete_vault -v

# Quick pass — stop on first failure
python -m pytest backend/tests/test_vaults.py -x --tb=line -q

# With coverage (informational)
python -m pytest backend/tests/test_vaults.py --cov=backend --cov-report=term-missing
```

The `-x` flag stops on first failure (useful during development). Omit it when checking the full test suite.

### Async Test Execution

This project uses `asyncio_mode = "auto"` in pyproject.toml (pytest-asyncio), so async tests are auto-detected. No special flags needed:

```bash
python -m pytest backend/tests/test_async_module.py -v
```

For projects without auto mode, add `-o asyncio_mode=auto` or use the `@pytest.mark.asyncio` decorator.

## Ruff Linting for Test Files

Ruff catches issues that block CI. Run locally before pushing:
```bash
ruff check backend/tests/test_vaults.py
```

Common ruff errors in test files:
- **F841**: Unused variable — remove the assignment, call the function bare
- **W293**: Trailing whitespace on blank lines — remove whitespace
- **F541**: f-string without placeholders — remove the `f` prefix
- **W291**: Trailing whitespace on comment lines — trim trailing spaces

Fix automatically where possible:
```bash
ruff check --fix backend/tests/test_vaults.py
```

## Source-inspection test pattern (RAGAPPv3)

Some backend tests open Python source files as strings and regex-match for
structural invariants — e.g., "every `StreamingResponse` call must include
`X-Accel-Buffering`", or "every route file exports a `router` object". This
pattern appears in `backend/tests/test_path_prefix.py` and similar files.

**When to use it:**
- Enforcing cross-cutting structural invariants that are hard to exercise
  behaviorally (e.g., "all streaming responses must set a header").
- Checking that boilerplate or security-sensitive patterns are not omitted.
- When a behavioral test would require an integration setup disproportionate
  to the risk being tested.

**When NOT to use it:**
- As a substitute for behavioral tests when a behavioral test is straightforward.
- For logic correctness — source inspection cannot catch a header present but
  set to the wrong value.
- Where refactoring (e.g., renaming a function) would silently break the test
  without breaking production behavior.

**Tradeoff:** Fast and easy to write; fragile to non-behavioral refactoring.
Always prefer behavioral unit tests when practical. When using source
inspection, add a comment explaining why a behavioral test is not used.

## Debugging CI Python Test Failures

1. **Check if the failure is pre-existing**: Run the same test on `master` first. If it fails there too, it's not introduced by your branch.
2. **Read the exact error message**: Python tracebacks include file, line number, and assertion values. The assertion diff (`expected X, got Y`) tells you everything.
3. **Check mock setup**: The most common Python test bug is a mock that doesn't match the actual method signature or production behavior. Read the production code's `except` clause before writing the mock.
4. **Check database state**: If a test creates data in a `try` block, verify the data exists before asserting behavior that depends on it. Missing `self._insert_*` calls are a common source of "expected 200, got 404" failures.
