# RAGAPPv3 Testing Policy

Authoritative testing conventions and policy for this repository. Referenced by
the `writing-tests` skill in every agent tree and by `AGENTS.md`. Pairs with
`docs/engineering/conventions.md` and the `ci-compatibility-audit` skill.

---

## 1. Policy (what's expected)

- **New behavior ships with tests.** Features and bug fixes get corresponding tests in the same change. For a bug fix, add a test that reproduces the bug, then make it pass.
- **Assert behavior, not just status.** Backend: assert HTTP status **and** response body **and** the resulting DB state change. Frontend: assert the callback was invoked with the expected arguments / the expected DOM appeared — not merely that the component rendered.
- **Test the negative paths.** Cross-vault isolation, permission denials (403), invalid input (422), cascade deletes, and error branches. Security-sensitive paths often have an `*_adversarial` companion test file — follow that precedent.
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

### Avoid the local event-loop trap
CI pins **Python 3.11**. On a newer local interpreter (e.g. 3.14), tests that call `asyncio.get_event_loop()` / `loop.run_until_complete(...)` fail with **`RuntimeError: There is no current event loop`** — this is a local-interpreter artifact, **not a regression**. Known-affected files include `test_document_actions_audit.py`, `test_embeddings_pooling*.py`, `test_exact_match_promote_adversarial.py`, `test_library_vault_id_config.py`, `test_vault_query_limit.py`. Prefer `IsolatedAsyncioTestCase` / `asyncio.run(...)` over manual `get_event_loop()` in new tests; use a 3.11 venv locally when you can. The `ruff` lint gate and the CI-targeted tests are the reliable local signals.

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

## 4. What CI runs vs. what you should run

CI (`.github/workflows/ci.yml`) is **not** the full suite:

- **Backend job:** `ruff check .` + a *targeted* pytest subset (`test_path_prefix.py`, `test_auth_routes.py`, `test_main_catchall.py`) + informational coverage.
- **Frontend job:** `npm run typecheck`, `npm run lint`, API smoke tests, full `npm test`, `npm run build`, and a subpath build.
- **Quality contracts:** `check_config_contract.py`, `check_pr_scope_drift.py`.

Because the backend CI subset is narrow, **also run the tests for the area you changed** locally (e.g. `pytest -q tests/test_tags_routes.py`). The `ci-compatibility-audit` skill lists the exact local mirror commands; run it before pushing.
