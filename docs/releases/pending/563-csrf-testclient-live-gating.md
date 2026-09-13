# CSRF suite runs in CI; live tests opt-in; root test files collected (Issue #563)

## What changed

### Backend tests
- `backend/tests/test_csrf_integration.py` is rewritten as an in-process
  `TestClient` suite. The four CSRF behaviors that previously had zero CI
  coverage — cookie security attributes (`SameSite`, `Max-Age`), cookie-set-on-
  issue, per-request token freshness, and token-reuse tolerance — now execute
  in every CI build against the real app and the real CSRF validator. The old
  module probed `http://localhost:9090` at import time and gated all twelve
  tests on the answer: silent skips in CI, and on a dev machine with an
  unrelated listener on 9090 it fired real register/token requests at that
  foreign service. That scaffolding (`check_backend_available`, `BASE_URL`,
  `skipUnless` gates) is deleted; the other eight behaviors remain covered by
  `tests/test_csrf_auth.py`.
- The two Tier-3 live-backend tests in `tests/test_csrf_adversarial.py` keep
  their bodies byte-identical but are now marked `@pytest.mark.live` instead of
  being gated by the same import-time probe (also deleted there).
- **Operator-visible behavior change:** running the live tests now requires an
  explicit opt-in, `RAGAPP_LIVE_TESTS=1`. Opting in without a backend listening
  fails loudly (connection error) instead of silently skipping — deliberate, so
  a "pass" can never again mean "an unrelated process on 9090 answered". In CI
  the live tests are skipped with a visible reason, itemized by the `-rs` flag
  the backend Test step now carries.
- The two orphaned root-level files `backend/test_singleton_processor.py` and
  `backend/test_background_tasks_unit.py` move under `backend/tests/` as real,
  collected pytest tests (bare `async def` per the pytest-asyncio auto-mode
  convention; the singleton fixture resets global state; no import-time
  environment mutation). They had not been collected by `pytest tests/` since
  they were written, and had already rotted against the current
  `BackgroundProcessor.enqueue(file_path, vault_id, ...)` signature.
- `backend/pyproject.toml` registers the `live` marker and enables
  `--strict-markers`, so an unregistered marker is now a hard collection error.

### CI / quality contracts
- New `scripts/check_test_collection_scope.py` (Quality contracts job) fails
  when a pytest test file exists outside `backend/tests/` — the collection-scope
  guard for the orphaned-file defect class.

### No runtime changes
No application code, schema, config default, or dependency surface is touched.
Rollback is a plain revert of the test-file and CI changes.
