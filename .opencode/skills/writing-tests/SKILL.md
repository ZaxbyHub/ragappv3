---
name: writing-tests
description: >
  RAGAPPv3 testing policy and conventions. Load before writing or modifying any
  test, fixing a test/CI failure, or adding coverage. Covers backend pytest +
  unittest (SimpleConnectionPool dependency-override harness, FK cascades, the
  Python 3.11-vs-local event-loop trap) and frontend Vitest + React Testing
  Library + jsdom (MemoryRouter, Radix Select, react-virtual mock patterns).
  This repo's frontend uses Vitest, NOT bun:test.
---

# RAGAPPv3 Testing

The authoritative testing policy and conventions live in
**`docs/engineering/testing.md`**. Read it before touching tests.

## Policy (summary)

- New behavior ships with tests. For a bug fix, add a failing reproduction test, then make it pass.
- **Feature gate / optional dependency removal:** when removing a gate, scan for old assertions. See `docs/engineering/testing.md` → "Feature gate / optional dependency removal checklist".
- Assert real behavior: backend → status **+** body **+** DB state change; frontend → callback args / DOM, not just "it rendered".
- Cover negative paths (403/422, cross-vault isolation, cascade deletes, error branches). Security-sensitive code has `*_adversarial` companion tests.
- No test theater — a test must exercise what its name claims. See "Test must exercise what its name claims" below.
- **Verify regression tests are non-vacuous:** before committing, stash or revert ONLY the source fix (leave the new test in place), run it, and confirm it fails with the original bug. Restore the fix and confirm it passes. A test that passes on both fixed and unfixed code is not a regression guard — it is theater.
- **Test must exercise what its name claims.** A test named `test_step_back_*` that accesses `call_args[0][0]` (positional) but the production code calls with kwargs (`messages=messages`) is testing the LAST recorded call (which may be from a different code path) — and silently passing. Use `call_args_list` and find the right call by signature, not just `call_args`.

## Backend (pytest + unittest)

- `unittest.TestCase` / `IsolatedAsyncioTestCase` run under pytest; `asyncio_mode = "auto"` (no marker needed). `conftest.py` sets test env and clears `app.*` modules.
- Route tests use the **`SimpleConnectionPool` + `app.dependency_overrides`** harness — canonical example `backend/tests/test_tags_routes.py`: tempdir → `init_db` → `run_migrations`; override `get_db` / `get_vector_store` (AsyncMock) / `get_current_active_user` / `csrf_protect`; restore in teardown.
  - When the endpoint uses `Depends(get_evaluate_policy)`, you MUST also override `get_evaluate_policy` in setUp (FastAPI resolves all dependencies even if not called, so the real one acquires a real DB connection). Pattern in `test_api_routes.py::TestDocumentsEndpoints.setUp` (line 486-489).
- Seed rows in FK order; verify cascades by deleting the parent (FKs are ON).
- **CSRF on mutating endpoints:** most state-mutating routes now depend on
  `csrf_protect`. Route tests don't reconstruct the cookie/header double-submit —
  `conftest.py` auto-bypasses CSRF via the pytest-only `RAGAPP_CSRF_TEST_BYPASS`
  env flag (honoured by `security.csrf_protect` only when `PYTEST_CURRENT_TEST`
  is also set). Classification is automatic: a test **module whose source
  mentions "csrf"** is left to exercise the *real* validator (don't rely on the
  bypass there); every other module gets the bypass, which works for both the
  shared `app.main` app and tests that build their own `FastAPI()`. Filename
  alone is not used for classification. To test real CSRF enforcement, ensure
  the module source references csrf; to just hit a protected endpoint, do
  nothing.
- **Per-file `lancedb`/`pyarrow`/`unstructured` stubs are load-bearing for CI**,
  not redundant boilerplate — CI installs `requirements-ci.txt`, which omits
  those packages (see `ci-compatibility-audit`). Removing a stub can break
  collection in CI even though it passes locally with the full deps installed.
- **CI pins Python 3.11.** Local 3.14+ fails some tests with `RuntimeError: There is no current event loop` — a local artifact, not a regression. Avoid manual `asyncio.get_event_loop()` in new tests.

### Authz-aware test fixture setup

When you modify endpoint authorization (adding caller-org intersection checks,
role-based access, assigned-org validation, etc.), the new authz preconditions
can break tests in **any file that exercises that endpoint** — not just the
files in your PR diff. CI runs the full `pytest tests/` suite (~3918 tests),
so a test file you never touched can fail if its fixture doesn't seed the data
the new check requires.

**Before pushing authz changes:**
1. Grep for ALL test files that call the modified endpoint:
   `grep -rl "users.*organizations\|users.*groups" backend/tests/`
2. For each file found, verify its fixture seeds the relational data the new
   check needs (e.g., `org_members` rows for caller-org intersection checks).
3. Run the full test suite locally (from `backend/`): `pytest tests/ -q --tb=short`

**Common fixture gaps when adding org-scoped authz:**
- Test creates users + orgs but no `org_members` entries → caller-org
  intersection check sees empty sets → 403 where test expects 200.
- Test fixture places data in a row that a per-test INSERT also targets →
  `UNIQUE(org_id, user_id)` constraint violation. Always check for per-test
  INSERTs in the same table before placing fixture data.

**Real example (PR #240):** `test_user_org_roles.py` had its own `setup_db`
fixture that created users and orgs but no `org_members`. The caller-org
intersection check added to `update_user_organizations` caused failures in
`TestPerOrgRoleMemberships` and `TestLegacyOrgIdsFormat` (the third class,
`TestDeleteUserOwnerGuard`, uses DELETE and was unaffected). Fix: seed
`ADMIN_ID` into all orgs and `TARGET_ID` into a non-conflicting org in `setup_db`.

### Conftest.py shared fixtures (post-PR #215)

The conftest.py now has 3 autouse fixtures plus 1 session-scoped fixture:

1. `_bypass_csrf_for_csrf_naive_tests` (autouse) — CSRF bypass for CSRF-naive modules
2. `_reset_rate_limiter` (autouse) — resets in-memory rate limiter
3. `_reset_db_pool` (autouse, since #215) — closes the singleton SQLite pool between tests
4. `_cache_bcrypt_hash_for_test_passwords` (session-scoped, since #215) — caches bcrypt hash for common test password 'pass123'

**Patterns to follow when adding a new autouse fixture in conftest.py:**
- Place it AFTER the existing fixtures, BEFORE `pytest_configure`
- Use `try/except (ImportError, AttributeError)` guards around imports (production modules may not be importable during early collection)
- Use `monkeypatch.setattr` for cleanup (or yield + restore in finally)
- Match the existing pattern of doing cleanup BOTH before and after yield (defensive on both ends)

**Critical pattern for monkey-patching:**
Patch the UNDERLYING method, not the wrapper function, when test modules do
`from app.services.auth_service import hash_password` at module level. Direct
imports capture the reference at import time, bypassing module-level patches.
The `pwd_context.hash` patch pattern in `_cache_bcrypt_hash_for_test_passwords`
is the canonical example.

## Frontend (Vitest + RTL + jsdom)

> Vitest, **not** `bun:test`. Ignore any bun guidance.

- Config in `frontend/vite.config.ts`; `*.test.tsx`; `src/test/setup.ts` mocks `localStorage`/`confirm`/`scrollTo`.
- jsdom mock patterns (full snippets in `ci-compatibility-audit/references/frontend-testing-gotchas.md`): wrap `<Link>` components in `MemoryRouter`; mock `@/components/ui/select` (Radix can't open in jsdom); mock `@tanstack/react-virtual`'s `useVirtualizer` to render all rows; `vi.mock` factories can't close over outer vars (`await import("react")`).

### Fake Timers & Async Loop Testing

Tests for polling loops, reconnect logic, and interval-driven behavior require
fake timers to avoid real wall-clock waits. This repo's Vitest patterns:

- **Use `vi.useFakeTimers()` + `vi.advanceTimersByTimeAsync()`** for async
  loop assertions. Fake timers let you advance time deterministically without
  waiting real seconds. Always call `vi.useRealTimers()` in `afterEach`.

- **Prefer `vi.waitFor` (Vitest) over `@testing-library/react` `waitFor`** when
  fake timers are active. RTL's `waitFor` uses real timers internally and may
  hang or timeout when fake timers are installed. Vitest's `vi.waitFor` is
  fake-timer-aware and integrates correctly with `vi.advanceTimersByTimeAsync`.

```ts
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});
```

- **Canonical pattern for testing an async reconnect loop** (e.g.,
  WebSocket/SSE with polling backoff):

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { MyReconnectingComponent } from "./MyReconnectingComponent";

const connectMock = vi.fn();

describe("reconnect loop with fake timers", () => {
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("retries connection on failure", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MyReconnectingComponent />);

    // Initial render triggers connection attempt
    // Advance past the retry interval to trigger the retry
    await vi.advanceTimersByTimeAsync(3000);
    // Assert the retry was attempted
    expect(connectMock).toHaveBeenCalledTimes(2);
  });
});
```

- **Mock sequencing for JWT refresh then reconnect:** When testing a hook or
  component that reconnects after a 401 token_expired response, sequence the
  fetch error, then the refresh mock, then the `getJwtAccessTokenMock` return
  values. Canonical pattern from `useWikiEventStream.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

it("reconnects with the refreshed token after a 401 token_expired response", async () => {
  vi.useFakeTimers();
  fetchMock
    .mockResolvedValueOnce(errorResponse(401, "token_expired"))
    .mockResolvedValue(controllableSse().response);
  refreshAccessTokenMock.mockResolvedValue("refreshed-jwt-token");
  getJwtAccessTokenMock
    .mockReturnValueOnce("test-jwt-token")
    .mockReturnValue("refreshed-jwt-token");

  renderHook(() => useWikiEventStream(42, vi.fn()));

  // Wait for the initial fetch and the refresh.
  await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));

  // Advance past RECONNECT_BASE_MS (1000 ms) so the backoff timer fires.
  await vi.advanceTimersByTimeAsync(1100);

  // The hook returns "error" after successful refresh, which triggers reconnect.
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

  // Verify the second fetch uses the refreshed token.
  const [, init] = fetchMock.mock.calls[1];
  expect(init.headers.Authorization).toBe("Bearer refreshed-jwt-token");
});
```

Key points about this pattern:
- `fetchMock` resolves the first call as 401 token_expired, the second call as success
- `refreshAccessTokenMock.mockResolvedValue("refreshed-jwt-token")` provides the new token
- `getJwtAccessTokenMock.mockReturnValueOnce(...).mockReturnValue(...)` sequences old→new token
- `vi.advanceTimersByTimeAsync(1100)` triggers the reconnect backoff timer
- The final assertion checks that the SECOND fetch used the refreshed token in the Authorization header

- **`vi.useRealTimers()` gotcha:** Between the test body and `afterEach`,
  there is a window where real `setTimeout`/`setInterval` can fire. If a
  component started an interval with fake timers that fires during teardown,
  wrap the interval-clearing in a try/finally or use `vi.useRealTimers()`
  BEFORE `cleanup()` in afterEach. The recommended `afterEach` template
  above handles this correctly by calling `vi.useRealTimers()` first.

- **Do not use `vi.advanceTimersByTime` (sync)** for async code paths.
  Always prefer the async variant `vi.advanceTimersByTimeAsync`. The sync
  variant cannot flush microtasks (Promises) and produces false test passes.

## Source-inspection test pattern

Some backend tests open Python source files as strings and regex-match for
structural invariants — e.g., "every `StreamingResponse` call must include
`X-Accel-Buffering`", or "every route file exports a `router` object". This
pattern appears in `backend/tests/test_path_prefix.py` and similar files.

**When to use it:**
- Enforcing crossutting structural invariants that are hard to exercise
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

## Running

Since PR #215 (issue #209), the Backend job runs the full `pytest tests/`
suite (3918 tests, ~18m on CI Linux, ~3-5m locally). The job timeout is
60m. The full suite is the source of truth — there's no separate "narrow
subset" anymore. Run your changed area's tests locally first for fast
feedback (`pytest -q tests/<file>`); then run the full suite before
pushing. Use `ci-compatibility-audit` for the exact CI-mirror commands.

See `docs/engineering/testing.md` for full detail.
