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
- Assert real behavior: backend → status **+** body **+** DB state change; frontend → callback args / DOM, not just "it rendered".
- Cover negative paths (403/422, cross-vault isolation, cascade deletes, error branches). Security-sensitive code has `*_adversarial` companion tests.
- No test theater — a test must exercise what its name claims.
- **Verify regression tests are non-vacuous:** before committing, stash or revert ONLY the source fix (leave the new test in place), run it, and confirm it fails with the original bug. Restore the fix and confirm it passes. A test that passes on both fixed and unfixed code is not a regression guard — it is theater.

## Backend (pytest + unittest)

- `unittest.TestCase` / `IsolatedAsyncioTestCase` run under pytest; `asyncio_mode = "auto"` (no marker needed). `conftest.py` sets test env and clears `app.*` modules.
- Route tests use the **`SimpleConnectionPool` + `app.dependency_overrides`** harness — canonical example `backend/tests/test_tags_routes.py`: tempdir → `init_db` → `run_migrations`; override `get_db` / `get_vector_store` (AsyncMock) / `get_current_active_user` / `csrf_protect`; restore in teardown.
- Seed rows in FK order; verify cascades by deleting the parent (FKs are ON).
- **CSRF on mutating endpoints:** most state-mutating routes now depend on
  `csrf_protect`. Route tests don't reconstruct the cookie/header double-submit —
  `conftest.py` auto-bypasses CSRF via the pytest-only `RAGAPP_CSRF_TEST_BYPASS`
  env flag (honoured by `security.csrf_protect` only when `PYTEST_CURRENT_TEST`
  is also set). Classification is automatic: a test **module whose source
  mentions "csrf"** is left to exercise the *real* validator (don't rely on the
  bypass there); every other module gets the bypass, which works for both the
  shared `app.main` app and tests that build their own `FastAPI()`. So: to test
  real CSRF enforcement, put it in a `test_csrf*`-named file (or otherwise
  reference csrf); to just hit a protected endpoint, do nothing.
- **Per-file `lancedb`/`pyarrow`/`unstructured` stubs are load-bearing for CI**,
  not redundant boilerplate — CI installs `requirements-ci.txt`, which omits
  those packages (see `ci-compatibility-audit`). Removing a stub can break
  collection in CI even though it passes locally with the full deps installed.
- **CI pins Python 3.11.** Local 3.14+ fails some tests with `RuntimeError: There is no current event loop` — a local artifact, not a regression. Avoid manual `asyncio.get_event_loop()` in new tests.

## Frontend (Vitest + RTL + jsdom)

> Vitest, **not** `bun:test`. Ignore any bun guidance.

- Config in `frontend/vite.config.ts`; `*.test.tsx`; `src/test/setup.ts` mocks `localStorage`/`confirm`/`scrollTo`.
- jsdom mock patterns (full snippets in `ci-compatibility-audit/references/frontend-testing-gotchas.md`): wrap `<Link>` components in `MemoryRouter`; mock `@/components/ui/select` (Radix can't open in jsdom); mock `@tanstack/react-virtual`'s `useVirtualizer` to render all rows; `vi.mock` factories can't close over outer vars (`await import("react")`).

## Source-inspection test pattern

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

## Running

CI runs only a *narrow* backend pytest subset — also run your changed area's tests locally (`pytest -q tests/<file>`). Use `ci-compatibility-audit` for the exact CI-mirror commands before pushing.

See `docs/engineering/testing.md` for full detail.
