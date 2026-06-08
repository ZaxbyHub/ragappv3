"""Test environment configuration.

Uses pytest_configure hook to set test-compatible environment variables
and clear all app.* modules from sys.modules before test collection.
This ensures settings are initialized with test-compatible values every time.
"""

import os

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
    except Exception:
        pass
    try:
        from app.services.circuit_breaker import embeddings_cb
        embeddings_cb.reset()
    except Exception:
        pass
    yield
    try:
        from app.limiter import limiter
        limiter.reset()
    except Exception:
        pass
    try:
        from app.services.circuit_breaker import embeddings_cb
        embeddings_cb.reset()
    except Exception:
        pass


def pytest_configure(config):
    """Called before test collection.

    Sets environment variables and clears all app.* modules from the
    import cache so they re-import with test-compatible settings.
    """
    # Pre-import pyarrow/pandas while real modules are available.
    # Many test files stub sys.modules["pyarrow"] at module-level, which
    # breaks pandas on subsequent imports. Caching them here first prevents
    # later stubs from affecting already-loaded submodules.
    try:
        import pyarrow  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        pass

    os.environ["ADMIN_SECRET_TOKEN"] = "test-admin-key"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"

    import sys

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
