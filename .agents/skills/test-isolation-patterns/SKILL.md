---
name: test-isolation-patterns
description: Document pytest pollution patterns observed in this repo where tests pass in isolation but fail in combined sessions due to shared global state. Recommend autouse fixture scoping or explicit teardown patterns to make tests reliable in any combination.
disable-model-invocation: true
generated_at: 2026-07-02T22:30:00Z
---

# test-isolation-patterns

Tests pass in isolation but fail in combined pytest sessions when they share
untracked global state. This is the pattern observed in this repo's `test_vaults.py`,
`test_wiki_events_connection_lifetime.py`, and other files that re-init the same
SQLite connection pool, the active-user cache, the rate limiter, and CSRF tokens
without cleaning up between tests.

## Observed pollution paths

1. **Connection pool** — `_pool_cache` in `app/models/database.py` is a module-level
   dict keyed by path. If a test creates a pool at `tmp_path/test.db` and
   teardown doesn't `close_all()`, the next test finds the same cache entry.

2. **Active-user cache** — `_ACTIVE_USER_CACHE` in `app/api/deps.py` is a module-level
   dict keyed by `user_id`. A test that authenticates `user_id=1` as `superadmin`
   leaves a stale entry. The next test expecting `user_id=1` to be `member` fails
   because the cached entry is returned.

3. **Rate limiter** — `limiter` in `app/limiter.py` is a module-level object with
   in-memory storage. A burst in one test can exhaust the quota for the next.

4. **CSRF tokens** — `csrf_protect` reads from the DB. A test that seeds CSRF state
   doesn't always clean up; the next test inherits it.

## Mitigation patterns

### Pattern 1: Autouse fixture reset hook

```python
# conftest.py
import pytest
from app.models.database import _pool_cache
from app.api.deps import _ACTIVE_USER_CACHE


@pytest.fixture(autouse=True)
def reset_module_state():
    yield
    # teardown
    for pool in _pool_cache.values():
        try:
            pool.close_all()
        except Exception:
            pass
    _pool_cache.clear()
    _ACTIVE_USER_CACHE.clear()
```

### Pattern 2: Per-test pool creation

```python
def test_something(self):
    pool = SimpleConnectionPool(self.db_path, max_size=2)
    # override the get_pool factory
    app.dependency_overrides[get_pool] = lambda: pool
    try:
        # ... test body ...
    finally:
        pool.close_all()
```

### Pattern 3: Function-level fixture scope

```python
@pytest.fixture(scope="function")  # default but explicit
def clean_pool():
    pool = create_pool()
    yield pool
    pool.close_all()


@pytest.fixture(scope="function")
def clean_user_cache():
    from app.api.deps import _ACTIVE_USER_CACHE
    _ACTIVE_USER_CACHE.clear()
    yield
    _ACTIVE_USER_CACHE.clear()
```

### Pattern 4: Test isolation for the `limiter` singleton

```python
@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.limiter import limiter
    limiter.reset()
    yield
    limiter.reset()
```

## Diagnostic workflow

When a test fails in combined mode but passes in isolation:

```bash
# Run in isolation
python -m pytest tests/test_x.py -v

# Run combined with the failing test plus a likely polluting test
python -m pytest tests/test_y.py tests/test_x.py -v

# Identify global state shared
grep -n "_pool_cache\|_ACTIVE_USER_CACHE\|limiter" app/
```

## Anti-patterns to avoid

- **Mutable module-level dicts without a reset hook** — they always leak.
- **Singletons without explicit reset in teardown** — they leak.
- **Class-level state** — Python class attributes are shared across all instances.
- **Global registries** — `app.models.database._pool_cache`, dependency_overrides, app state.

## Connection to other skills

- `writing-tests` — foundational patterns for test design.
- `qa-sweep` — broad test suite audits that surface these issues.
- `codebase-review-swarm` — review process that catches module-level state.
