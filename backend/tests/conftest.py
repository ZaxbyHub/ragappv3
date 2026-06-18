"""Test environment configuration.

Uses pytest_configure hook to set test-compatible environment variables
and clear all app.* modules from sys.modules before test collection.
This ensures settings are initialized with test-compatible values every time.
"""

import os
import sys

import pytest

_CSRF_AWARE_MODULES: dict = {}


def _module_manages_csrf(path: str) -> bool:
    """True if a test module's source references CSRF at all.

    Such modules either install their own ``csrf_protect`` override or assert
    real CSRF enforcement, so the autouse bypass must leave them untouched.
    Cached per file path.
    """
    cached = _CSRF_AWARE_MODULES.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manages = "csrf" in fh.read().lower()
    except OSError:
        manages = False
    _CSRF_AWARE_MODULES[path] = manages
    return manages


@pytest.fixture(autouse=True)
def _bypass_csrf_for_csrf_naive_tests(request):
    """Toggle the test-only CSRF bypass for CSRF-naive route tests.

    Many route tests call mutating endpoints (via the shared app or a
    standalone ``FastAPI()`` they build) without a CSRF cookie/header. Now that
    the chat/vault/group/org/member/memory/settings routers require
    ``csrf_protect``, those requests would otherwise 403/503. The production
    frontend always sends ``X-CSRF-Token`` (axios interceptor + explicit header
    on the raw chat-stream fetch), so bypassing CSRF for tests that don't
    concern themselves with it keeps coverage honest.

    We flip ``RAGAPP_CSRF_TEST_BYPASS`` (honoured by ``security.csrf_protect``
    only under pytest) rather than per-app dependency overrides so the bypass
    also reaches the many tests that build their own app instance. Modules that
    reference CSRF at all (the dedicated CSRF suites, or route suites that
    install their own override / assert enforcement) are left with the bypass
    OFF so they exercise the real validator.
    """
    env_key = "RAGAPP_CSRF_TEST_BYPASS"
    module_file = getattr(request.module, "__file__", None)
    csrf_aware = bool(module_file and _module_manages_csrf(module_file))
    prev = os.environ.get(env_key)
    os.environ[env_key] = "0" if csrf_aware else "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = prev


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the in-memory rate limiter and circuit breakers before every test.

    Tests that share the FastAPI app instance share the same MemoryStorage
    bucket. Without a reset, a test file that issues many requests to a
    rate-limited endpoint can exhaust the quota and cause 429 errors in
    subsequent test files, producing spurious failures.

    Similarly, tests that trip the embeddings circuit breaker leave it open for
    subsequent tests.
    """
    try:
        from app.limiter import limiter
        limiter.reset()
    except (ImportError, AttributeError):
        pass
    try:
        from app.services.circuit_breaker import embeddings_cb
        embeddings_cb.reset()
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from app.limiter import limiter
        limiter.reset()
    except (ImportError, AttributeError):
        pass
    try:
        from app.services.circuit_breaker import embeddings_cb
        embeddings_cb.reset()
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def _reset_db_pool():
    """Reset the singleton SQLite connection pool between every test.

    The pool is a module-level global (``app.models.database._pool_cache``)
    that persists across the full test run. Without a reset, prior tests can
    leave open WAL journals / file locks / leaked connections where subsequent
    tests' ``sqlite3.connect() + PRAGMA journal_mode=WAL`` calls block on
    filesystem contention for 1h+, producing multi-hour CI hangs.

    This is a test-infra-only change that mirrors the existing
    ``_reset_rate_limiter`` pattern. ``close_all()`` sets ``_closed = True``
    but does not reset ``_created_count``; ``_pool_cache.clear()`` is
    mandatory so the next ``get_pool()`` creates a fresh pool.
    """
    try:
        from app.models.database import _pool_cache
        for pool in _pool_cache.values():
            try:
                pool.close_all()
            except Exception:
                pass
        _pool_cache.clear()
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from app.models.database import _pool_cache
        for pool in _pool_cache.values():
            try:
                pool.close_all()
            except Exception:
                pass
        _pool_cache.clear()
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def _reset_active_user_cache():
    """Clear the active-user cache before and after every test.

    The module-level ``_ACTIVE_USER_CACHE`` in ``app.api.deps`` persists
    across the full test run. Without a reset, a test that authenticates
    user_id=1 as 'superadmin' leaves a stale cache entry that causes
    subsequent tests expecting user_id=1 to be 'testuser' (or a different
    role) to receive 403 / wrong-user responses.

    We intentionally do NOT acquire ``_ACTIVE_USER_CACHE_LOCK`` here: that
    lock is a ``threading.Lock`` and holding it across the yield boundary
    deadlocks when the async test body also tries to acquire it. The cache
    is only accessed from the test's own event loop, so a plain dict clear
    is safe at fixture boundaries.
    """
    try:
        from app.api.deps import _ACTIVE_USER_CACHE
        _ACTIVE_USER_CACHE.clear()
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from app.api.deps import _ACTIVE_USER_CACHE
        _ACTIVE_USER_CACHE.clear()
    except (ImportError, AttributeError):
        pass


# Cache for bcrypt hash of common test passwords (saves ~1 sec per hash)
# Created lazily on first test invocation. Keyed by password string.
_bcrypt_hash_cache: dict = {}


@pytest.fixture(autouse=True, scope="session")
def _cache_bcrypt_hash_for_test_passwords():
    """Pre-compute and cache the bcrypt hash for the common test password 'pass123'.

    The test_users_routes_adversarial.py test class creates 50+ users in a loop
    with the same password 'pass123' in its setUp. Without this cache, each
    hash_password() call takes ~1 sec locally and ~2-3 sec on slow CI runners,
    pushing the test suite past the 30m job timeout.

    This fixture patches pwd_context.hash (the underlying CryptContext method
    that hash_password calls internally) with a fast version that returns
    the cached value for 'pass123' and delegates to the original for any
    other password. Patching pwd_context.hash (rather than hash_password) is
    necessary because some test modules do `from app.services.auth_service
    import hash_password` at module level, which captures a direct reference
    to the original function that won't see a later patch on the module-level
    name.

    The cache is lazy: the first test that needs 'pass123' triggers the real
    bcrypt call, subsequent tests get the cached value. Tests that use other
    passwords (rare) are unaffected.
    """
    # Lazy import — auth_service may not be available during early collection
    try:
        from app.services import auth_service as _auth_module
    except ImportError:
        return  # app.services.auth_service not importable; skip this fixture

    # Patch pwd_context.hash (the underlying method that hash_password calls), NOT
    # auth_service.hash_password. The latter is bypassed by tests that do
    # `from app.services.auth_service import hash_password` at module level.
    original_pwd_hash = _auth_module.pwd_context.hash
    common_password = "pass123"

    def _get_cached_hash(password: str) -> str:
        if password not in _bcrypt_hash_cache:
            _bcrypt_hash_cache[password] = original_pwd_hash(password)
        return _bcrypt_hash_cache[password]

    def _fast_pwd_hash(secret, *args, **kwargs):
        if isinstance(secret, str) and secret == common_password:
            return _get_cached_hash(secret)
        return original_pwd_hash(secret, *args, **kwargs)

    # Monkey-patch pwd_context.hash (the underlying method that hash_password calls)
    _auth_module.pwd_context.hash = _fast_pwd_hash

    yield

    # Restore the original on session teardown (best-effort)
    try:
        _auth_module.pwd_context.hash = original_pwd_hash
    except Exception:
        pass


def pytest_configure(config):
    """Called before test collection.

    Sets environment variables and clears all app.* modules from the
    import cache so they re-import with test-compatible settings.
    """
    # pandas' is_pyarrow_array() looks up pyarrow.Array from sys.modules at
    # call time. Many test files stub sys.modules['pyarrow'] with a bare
    # types.ModuleType that has no Array attribute, causing AttributeError.
    # Fix: import pandas first (while pyarrow is absent), THEN install a rich
    # stub with __getattr__ so pandas can import cleanly and later calls to
    # is_pyarrow_array() return False gracefully.
    import types as _types

    # Import pandas before any pyarrow stub so it initialises without errors.
    try:
        import pandas  # noqa: F401
    except ImportError:
        pass

    try:
        import pyarrow  # noqa: F401
    except ImportError:
        # pyarrow not installed — install a stub whose __getattr__ returns a
        # dummy class with __instancecheck__ = False so isinstance() checks pass.
        class _StubMeta(type):
            def __instancecheck__(cls, instance):
                return False

        _stub_cls = _StubMeta('_PyArrowStub', (), {})

        class _PyArrowModule(_types.ModuleType):
            def __getattr__(self, name):
                return _stub_cls

        _pa = _PyArrowModule('pyarrow')
        sys.modules['pyarrow'] = _pa

    os.environ["ADMIN_SECRET_TOKEN"] = "test-admin-key"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"

    app_keys = [
        k for k in list(sys.modules.keys()) if k == "app" or k.startswith("app.")
    ]
    for key in app_keys:
        del sys.modules[key]


# Also clean up diagnostic file if it was created by a previous version
import pathlib

diag_file = pathlib.Path(__file__).parent / "_conftest_diag.txt"
if diag_file.exists():
    diag_file.unlink(missing_ok=True)
