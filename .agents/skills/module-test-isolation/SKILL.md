---
name: module-test-isolation
description: Document the discipline of isolating module-level mutable state in tests. Use when adding tests that touch global registries, singletons, or class attributes. Prevents test pollution that surfaces only in combined pytest sessions.
disable-model-invocation: true
generated_at: 2026-07-02T22:30:00Z
---

# module-test-isolation

Module-level mutable state — module-level dicts, class attributes, global registries —
is shared across all test files. A test that modifies this state can leak into
unrelated tests if its teardown does not reset the state.

## When this matters

- A test patches a global registry and forgets to restore it.
- A test mutates a class attribute (e.g. `setattr(MyClass, 'flag', True)`).
- A test sets `settings.X = Y` and the teardown restores `settings.X` but other modules captured the old value.
- A test's `setUp` and `tearDown` touch shared global state differently than other tests.

## Observed in this repo

`app/models/database.py` exports `_pool_cache` — a module-level dict
keyed by SQLite path. If a test creates a pool at `tmp_path/test.db`,
the pool is stored in `_pool_cache`. The next test at the same path finds
the existing pool, which has different settings (e.g. different schema, different
journal mode). Pollution.

`app/api/deps.py` exports `_ACTIVE_USER_CACHE` — a module-level dict
keyed by user_id. Tests that authenticate as `user_id=1` leave a stale entry
that returns to the next test.

`app/limiter.py` exports `limiter` — a module-level rate-limit singleton
with in-memory storage. Bursts in one test can deplete the next test's quota.

`app/api/deps.py` exports `dependency_overrides` (FastAPI's global override dict).
Tests that override dependencies without clearing them leak state.

## Mitigation pattern: reset hook

```python
# conftest.py
import pytest
from app.models.database import _pool_cache
from app.api.deps import _ACTIVE_USER_CACHE


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level mutable state around every test."""
    yield
    # Teardown: clear caches, close pools
    for pool in _pool_cache.values():
        try:
            pool.close_all()
        except Exception:
            pass
    _pool_cache.clear()
    _ACTIVE_USER_CACHE.clear()
```

## Mitigation pattern: per-test pool

```python
def test_xyz(self):
    pool = SimpleConnectionPool(self.db_path)
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        # ... test body that uses the pool ...
    finally:
        app.dependency_overrides.clear()
        pool.close_all()
```

## Diagnostic commands

```bash
# Find module-level mutable state
grep -rn "^_[a-z_]\+\s*=\s*{" app/ --include="*.py" | head -20

# Find module-level singletons
grep -rn "^[a-zA-Z_]\+\s*=\s*[A-Z][a-zA-Z]\+(" app/ --include="*.py" | head -20

# Find class attributes
grep -rn "^class .*:" app/ --include="*.py" -A 3 | head -40
```

## Acceptance criteria for a clean test

A test passes both:

```bash
# In isolation
python -m pytest tests/test_x.py -v

# In combined with likely polluters
python -m pytest tests/test_y.py tests/test_x.py -v
```

If the test fails in combined mode but passes in isolation, it has a
module-test-isolation defect. Fix the test's setUp/tearDown to reset
module state.

tracked_by: no automated test; this is a pattern advisory applied per-failing-test. The active-user-cache example is tracked_by backend/tests/test_active_user_cache.py.

## Anti-patterns to avoid

- Global dict that grows during tests without a teardown.
- Singleton that tracks state across tests.
- Class attribute that one test sets and another reads.
- Module-level `from X import Y` that picks up a patched Y for all tests.

## Connection to other skills

- `test-isolation-patterns` — the parent skill that documents these patterns.
- `writing-tests` — foundational patterns for test design.
- `qa-sweep` — broad test suite audits that surface these issues.
- `codebase-review-swarm` — review process that catches module-level state.
