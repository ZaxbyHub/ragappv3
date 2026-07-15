# RAGAPPv3 Testing Policy

Authoritative testing conventions and policy for this repository. Referenced by
the `writing-tests` skill in every agent tree and by `AGENTS.md`. Pairs with
`docs/engineering/conventions.md` and the `ci-compatibility-audit` skill.

---

## 1. Policy (what's expected)

- **New behavior ships with tests.** Features and bug fixes get corresponding tests in the same change. For a bug fix, add a test that reproduces the bug, then make it pass.
- **Assert behavior, not just status.** Backend: assert HTTP status **and** response body **and** the resulting DB state change. Frontend: assert the callback was invoked with the expected arguments / the expected DOM appeared — not merely that the component rendered.
- **Test the negative paths.** Cross-vault isolation, permission denials (403), invalid input (422), cascade deletes, and error branches. Security-sensitive paths often have an `*_adversarial` companion test file — follow that precedent.
- **Pool size and connection validation**: When adding configurable pool sizes or connection limits (e.g. `memory_store_pool_size`), add adversarial tests that verify validation rejects values below the minimum (e.g. 0, negative) and accepts values at or above the minimum.
- **No test theater.** A test whose name claims a behavior must actually exercise it. (Example fixed in this repo: a TagFilter test named "emits the tag id on selection" that never fired a selection — see `frontend/src/tests/documents-organization.test.tsx`.)
- **Match the production exception type** in mocks — don't catch/raise bare `Exception` when the code under test catches something specific.

---

## 2. Backend testing (pytest + unittest)

Config: `backend/pyproject.toml` `[tool.pytest.ini_options]` — `testpaths=["tests"]`, `python_classes=["Test*"]`, `python_functions=["test_*"]`, **`asyncio_mode = "auto"`** (async tests need no explicit marker). `backend/tests/conftest.py` sets test env vars (`USERS_ENABLED=false`, test JWT/admin secrets) and clears `app.*` modules before collection so settings re-init cleanly.

### Patterns
- Style is **`unittest.TestCase`** classes (and `unittest.IsolatedAsyncioTestCase` for fully-async cases), run under pytest. `setUp`/`tearDown` or `@pytest.fixture` are both used.
- **Route tests use a `SimpleConnectionPool` + FastAPI dependency overrides** (canonical example: `backend/tests/test_tags_routes.py`). The shape:
  - `tempfile.mkdtemp()` → `init_db(db_path)` → `run_migrations(db_path)`.
  - Override `get_db` (yield a pooled connection with `PRAGMA foreign_keys = ON`), `get_vector_store` (a `MagicMock` whose async methods are `AsyncMock`), `get_current_active_user` (a dict), and `csrf_protect`.
  - Always restore: `app.dependency_overrides.pop(...)` / `.clear()` and close the pool in teardown.
- Seed rows respecting FK order (insert the parent vault before child files). Verify cascades by deleting the parent and asserting child rows are gone (FKs are ON in the pool).
- Use `AsyncMock` for async service methods, `MagicMock` for sync, `unittest.mock.patch` for targeted internals (e.g. `WikiStore.mark_claims_stale_by_file`).
- **CSRF on mutating endpoints:** most state-mutating routes depend on `csrf_protect`. Route tests don't reconstruct the cookie/header double-submit — `conftest.py` auto-bypasses it via the pytest-only `RAGAPP_CSRF_TEST_BYPASS` env flag (honoured by `security.csrf_protect` **only** when `PYTEST_CURRENT_TEST` is also set, so it can't leak into production). Classification is automatic: a test **module whose source mentions "csrf"** is left to exercise the *real* validator; every other module is bypassed, which works for both the shared `app.main` app and tests that build their own `FastAPI()`. Filename alone is not used for classification. To test real CSRF enforcement, ensure the module source references csrf; to just call a protected endpoint, do nothing.
- **Per-file `lancedb`/`pyarrow`/`unstructured` import stubs are load-bearing for CI**, not redundant boilerplate — CI installs the reduced `requirements-ci.txt` (see §4), which omits those packages. Removing a stub can break collection in CI even though the file passes locally with the full deps installed.

### Avoid the local event-loop trap
CI pins **Python 3.11**. On a newer local interpreter (e.g. 3.14), tests that call `asyncio.get_event_loop()` / `loop.run_until_complete(...)` fail with **`RuntimeError: There is no current event loop`** — this is a local-interpreter artifact, **not a regression**. Known-affected files include `test_embeddings_pooling_adversarial.py`, `test_exact_match_promote_adversarial.py`, `test_vault_query_limit.py`. Prefer `IsolatedAsyncioTestCase` / `asyncio.run(...)` over manual `get_event_loop()` in new tests; use a 3.11 venv locally when you can. The `ruff` lint gate and the CI-targeted tests are the reliable local signals.

### Active-user cache tests (`test_active_user_cache.py`)
Tests for `get_current_active_user` caching live in `backend/tests/test_active_user_cache.py`. Key patterns:
- **`SimpleConnectionPool` + `app.dependency_overrides`** harness (same as other route tests).
- Cache state is module-level in `deps.py` (`_ACTIVE_USER_CACHE` dict + `_ACTIVE_USER_CACHE_LOCK`). Tests must reset it in `setUp`/`tearDown` by calling `deps._ACTIVE_USER_CACHE.clear()` — patching `time.monotonic` to control TTL expiry is also necessary for expiry-edge cases.
- `invalidate_active_user_cache` is tested by inserting a cached entry, calling the function, and asserting the dict is empty.

### Parallel vault permission tests (`test_effective_vault_permissions_parallel.py`)
Tests for concurrent permission queries live in `backend/tests/test_effective_vault_permissions_parallel.py`. Key patterns:
- Uses `IsolatedAsyncioTestCase` to test the `asyncio.gather` path in `get_effective_vault_permissions`.
- The `_SQLITE_SERIALIZED` flag in `deps.py` controls whether the concurrent or sequential branch runs; tests may monkey-patch `deps._SQLITE_SERIALIZED` to force either path.
- Seeds multiple vaults, groups, and memberships; asserts each vault returns the correct effective permission string.

### Auth override async-path tests (`test_auth_override_async_path.py`)
Tests for the `get_current_user_or_service_account` dependency-override branch (issue #312 / `authz-bridging-exceptions` skill) live in `backend/tests/test_auth_override_async_path.py`. Key patterns and policy:
- The override branch in `deps.py` (`get_current_user_or_service_account`) reads `app.dependency_overrides[get_current_active_user]` and must `await` the result only when it is a coroutine: `user = await _result if inspect.iscoroutine(_result) else _result`. The check must use `inspect.iscoroutine` — never `inspect.isawaitable` (the historical anti-pattern the `authz-bridging-exceptions` skill documents).
- The test overrides `get_current_active_user` with a **module-scope `async def`** (not a sync lambda) that calls another `async def`, matching AC-2 of the skill, then drives a real route (`GET /api/tags/documents/{id}`) through `TestClient`.
- **Two bug classes are guarded, and the distinction matters:** (1) *drop-the-await* is falsified **behaviorally** by a 200 (authorized) / 403 (denied) pair — mutating the branch to skip the await makes both fail because the raw coroutine reaches `_evaluate_policy`. (2) *`isawaitable`-vs-`iscoroutine`* is **not** behaviorally falsifiable here (the override is *called* before the check, so `_result` is a coroutine on which both predicates return True); it is pinned only by a **source-string tripwire** (`test_override_branch_uses_iscoroutine_not_isawaitable`). A purely behavioral falsifier for class 2 would require an `isawaitable` check on an uncalled `async def`, which does not exist in `deps.py`.
- `get_evaluate_policy` has no `dependency_overrides` read of its own; FastAPI resolves overrides of it directly. So a test overriding `get_evaluate_policy` with an `async def` would be vacuous — the override branch under test is on `get_current_active_user`.

---

## 3. Frontend testing (Vitest + React Testing Library + jsdom)

> This repo's frontend uses **Vitest**, not `bun:test`. If a generic skill mentions `bun:test`, that section does not apply here.

Config: `frontend/vite.config.ts` `test` block — `globals: true`, `environment: "jsdom"`, `setupFiles: ./src/test/setup.ts`. Test files are named `*.test.tsx`. `setup.ts` mocks `localStorage`, `window.confirm`, and `Element.prototype.scrollTo` (jsdom omits it).

### Established jsdom mock patterns
These cost real debugging cycles to discover — reuse them. Full worked examples and copy-paste snippets are in the `ci-compatibility-audit` skill's `references/frontend-testing-gotchas.md`.

1. **Router context** — components rendering `<Link>` / using `useNavigate`/`useParams` must be wrapped in `MemoryRouter`. Alias `render` to inject the wrapper file-wide.
2. **Radix `Select` (shadcn `ui/select`)** cannot be opened in jsdom (no pointer-capture). Either mock `@/components/ui/select` with a context that lets `SelectItem` clicks call `onValueChange` (when asserting selection wiring), or render-stub it (when you only need it present). Only module-mock it in files where the component under test is the sole `ui/select` consumer.
3. **Virtualized lists (`@tanstack/react-virtual`)** only render the visible window. Mock `useVirtualizer` to return all items so off-screen rows are assertable.
4. `vi.mock(path, factory)` is hoisted — the factory can't close over outer variables; `await import("react")` inside it.

Canonical examples: `frontend/src/tests/documents-organization.test.tsx`, `frontend/src/pages/DocumentsPage.test.tsx`, and the DocumentsPage virtualization suites.

---

### Feature gate / optional dependency removal checklist

When you remove or change a feature gate, optional dependency, or
install-presence check, scan all test files for assertions that codify the
old gate behavior:

- `grep -rl "<gate-symbol>" backend/tests/` — e.g. `ragas`, the feature-flag name
- `grep -rl "<endpoint-path>" backend/tests/` — e.g. `/eval/ragas`
- `grep -rl "<feature-flag-env>" backend/tests/` — e.g. `EVAL_ENABLED`

Update or remove tests that assert the removed gate; add tests that verify
the new behavior.

**Canonical anti-pattern:** `backend/tests/test_eval_feature_gate.py` asserted
`import ragas` behavior that was removed in issue #283.

---

## 4. What CI runs vs. what you should run

CI (`.github/workflows/ci.yml`) runs the full suite:

- **Backend job:** `ruff check .` + the full pytest suite (`pytest --tb=short -v --timeout=300 tests/`).
- **Frontend job:** `npm run typecheck`, `npm run lint`, API smoke tests, full `npm test`, `npm run build`, and a subpath build.
- **Quality contracts:** `check_config_contract.py`, `check_pr_scope_drift.py`.

**The backend CI dependency set is reduced — "locally green" ≠ "CI green".** CI installs only `requirements-ci.txt` + `requirements-dev.txt`, which omit `lancedb`, `pyarrow`, `unstructured`, and `sentence-transformers` (stubbed per-file at test time). A dev machine usually has the full `requirements.txt`, so a backend test can pass locally yet fail in CI at import (`ModuleNotFoundError`). To validate a backend **test-scope** change (e.g. adding a file to the CI pytest list) faithfully — and faster, with no multi-GB model/db loads — reproduce the CI env instead of trusting the local run:

```bash
python -m venv /tmp/civenv
/tmp/civenv/bin/pip install -r backend/requirements-ci.txt -r backend/requirements-dev.txt
cd backend && /tmp/civenv/bin/python -m pytest -q tests/<candidate_file>.py
```
