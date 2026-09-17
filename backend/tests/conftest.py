"""Test environment configuration.

Uses pytest_configure hook to set test-compatible environment variables
and clear all app.* modules from sys.modules before test collection.
This ensures settings are initialized with test-compatible values every time.

Test modules may declare an explicit CSRF test policy with a module-level
``CSRF_TEST_POLICY = "naive" | "manages"`` assignment — see
``_module_manages_csrf`` below.
"""

import os
import re
import sys

import pytest

_CSRF_AWARE_MODULES: dict = {}

# Explicit per-module policy declaration (issue #202 / ENH-008). A test module
# may declare, at module level:
#     CSRF_TEST_POLICY = "naive"    # CSRF bypass allowed for this module
#     CSRF_TEST_POLICY = "manages"  # module exercises real CSRF enforcement
# The declaration wins over the lexical scan below. An unrecognized value (or
# no declaration at all) falls back to the legacy scan, so existing modules
# that never declare keep their current classification.
_CSRF_POLICY_DECLARATION = re.compile(
    r"""^\s*CSRF_TEST_POLICY\s*=\s*["'](naive|manages)["']\s*(?:#.*)?$"""
)


def _module_manages_csrf(path: str) -> bool:
    """True if a test module opts into real CSRF enforcement or references CSRF.

    An explicit module-level ``CSRF_TEST_POLICY`` declaration ("naive" or
    "manages") takes precedence: "naive" lets the autouse CSRF bypass apply
    even when the source mentions "csrf" incidentally (comment, docstring, or
    a spelled-out parameter name), and "manages" forces real enforcement.
    Without a recognized declaration, the legacy default applies: the module
    is treated as CSRF-managing when its source text mentions "csrf" at all.
    Such modules either install their own ``csrf_protect`` override or assert
    real CSRF enforcement, so the autouse bypass must leave them untouched.
    Cached per file path.
    """
    cached = _CSRF_AWARE_MODULES.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError:
        _CSRF_AWARE_MODULES[path] = False
        return False
    declared: str | None = None
    for line in source.splitlines():
        match = _CSRF_POLICY_DECLARATION.match(line)
        if match:
            declared = match.group(1)
            break
    if declared == "naive":
        manages = False
    elif declared == "manages":
        manages = True
    else:
        manages = "csrf" in source.lower()
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

    Tests that share the FastAPI app instance share the same storage bucket.
    Without a reset, a test file that issues many requests to a rate-limited
    endpoint can exhaust the quota and cause 429 errors in subsequent test
    files, producing spurious failures.

    Similarly, tests that trip the embeddings circuit breaker leave it open for
    subsequent tests.

    The reset is best-effort: in CI the limiter uses in-memory storage
    (``REDIS_URL=""``), whose reset is a harmless clear. When a developer runs
    the suite with a non-empty ``REDIS_URL`` but no reachable Redis, the limiter
    is wired to Redis and ``reset()`` raises a connection error — that is an
    environment artifact, not a test failure, so it is swallowed alongside the
    import/attribute guards.
    """
    try:
        from app.limiter import limiter
        limiter.reset()
    except Exception:
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
    except Exception:
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
    bcrypt call, subsequent tests get the cached value.

    IMPORTANT: the cache is deliberately scoped to ONLY the documented common
    password 'pass123'. Every other password is hashed/verified through the
    real CryptContext so (a) fresh-salt-per-hash is exercised for non-fixture
    passwords and (b) a real regression in the Argon2/bcrypt verify path is
    not short-circuited. Tests that use other passwords are unaffected and
    go through the real implementation.
    """
    # Lazy import — auth_service may not be available during early collection
    try:
        from app.services import auth_service as _auth_module
    except ImportError:
        return  # app.services.auth_service not importable; skip this fixture

    # The single password whose hash we memoize for suite speed.
    _CACHED_PASSWORD = "pass123"

    # Patch pwd_context.hash (the underlying method that hash_password calls), NOT
    # auth_service.hash_password. The latter is bypassed by tests that do
    # `from app.services.auth_service import hash_password` at module level.
    original_pwd_hash = _auth_module.pwd_context.hash
    original_pwd_verify = _auth_module.pwd_context.verify
    _cached_hash: dict = {}  # only ever holds the _CACHED_PASSWORD entry

    def _fast_pwd_hash(secret, *args, **kwargs):
        secret_str = str(secret) if not isinstance(secret, str) else secret
        # Only memoize the documented common password; everything else goes
        # through the real (fresh-salt) implementation.
        if secret_str != _CACHED_PASSWORD:
            return original_pwd_hash(secret, *args, **kwargs)
        if secret_str not in _cached_hash:
            _cached_hash[secret_str] = original_pwd_hash(secret, *args, **kwargs)
        return _cached_hash[secret_str]

    def _fast_pwd_verify(secret, hash_value, *args, **kwargs):
        secret_str = str(secret) if not isinstance(secret, str) else secret
        # Only short-circuit for the single memoized password; this is a
        # legitimate cache hit (the hash already matched), not a bypass, and
        # it never fires for any other password.
        cached_hash = _cached_hash.get(_CACHED_PASSWORD)
        if cached_hash is not None and hash_value == cached_hash:
            return secret_str == _CACHED_PASSWORD
        return original_pwd_verify(secret, hash_value, *args, **kwargs)

    _auth_module.pwd_context.hash = _fast_pwd_hash
    _auth_module.pwd_context.verify = _fast_pwd_verify

    yield

    # Restore the originals on session teardown (best-effort)
    try:
        _auth_module.pwd_context.hash = original_pwd_hash
        _auth_module.pwd_context.verify = original_pwd_verify
    except Exception:
        pass
def pytest_configure(config):
    """Called before test collection.

    Sets environment variables and clears all app.* modules from the
    import cache so they re-import with test-compatible settings.
    """
    # Issue #565 warning-gate bookkeeping must start clean in every pytest
    # process (a re-configure in the same process must not inherit stale
    # records from an earlier session).
    _NEVER_AWAITED_RECORDED.clear()
    # config.Settings resolves data_dir (and thus sqlite_path) against the
    # CURRENT WORKING DIRECTORY. The sqlite pool creates connections without
    # mkdir(parents=True), so under xdist any worker that reaches a real
    # pool before another worker's tests have created ./data fails with
    # "sqlite3.OperationalError: unable to open database file" (PR #576 CI:
    # test_deps_auth flaked exactly this way once this PR added enough new
    # tests to shift worker scheduling). Create the directory up front, on
    # every worker, before collection hands out the first test.
    try:
        os.makedirs("data", exist_ok=True)
    except OSError:
        pass
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
    # Force the rate limiter to in-memory storage for the test suite. No test
    # exercises real-Redis limiting, and without this the module-global limiter
    # (now wired to settings.redis_url, which defaults to redis://localhost)
    # would make each autouse limiter.reset() block ~4s on a Redis connection
    # timeout. This matches CI, which sets REDIS_URL="" with no Redis service.
    os.environ["REDIS_URL"] = ""

    app_keys = [
        k for k in list(sys.modules.keys()) if k == "app" or k.startswith("app.")
    ]
    for key in app_keys:
        del sys.modules[key]


def pytest_collection_modifyitems(config, items):
    """Skip live-marked tests unless explicitly opted in (issue #563 / C10).

    Tests that perform real network I/O (the Tier-3 live-backend tests in
    test_csrf_adversarial.py) are marked ``@pytest.mark.live``. Without an
    explicit opt-in they are skipped with a visible reason instead of probing
    ``localhost:9090`` — the import-time probes this replaces silently skipped
    in CI and, worse, drove real register/token requests at whatever unrelated
    service answered on 9090 on a developer machine. Set ``RAGAPP_LIVE_TESTS=1``
    to run them; an opted-in run against a dead backend then fails loudly by
    design (an explicit opt-in that finds no backend is a real failure signal,
    not something to hide). Collection (and therefore this hook) runs once on
    the xdist controller, so workers inherit the skip markers.
    """
    if os.environ.get("RAGAPP_LIVE_TESTS", "") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live test (real network I/O); opt in with RAGAPP_LIVE_TESTS=1"
    )
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip_live)


# ── JWT_SECRET_KEY guard ──────────────────────────────────────────
# Some test files (e.g. test_service_account_authenticates_to_routes)
# may momentarily delete os.environ["JWT_SECRET_KEY"] in their
# tearDown, which can KeyError subsequent tests in the same worker
# that access it at module level. This autouse fixture re-establishes
# the env var before every test function so the worker process always
# has a valid value.


@pytest.fixture(autouse=True)
def _ensure_jwt_secret_key():
    """Ensure JWT_SECRET_KEY is always set before each test."""
    if "JWT_SECRET_KEY" not in os.environ:
        os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"


# Also clean up diagnostic file if it was created by a previous version
import pathlib

diag_file = pathlib.Path(__file__).parent / "_conftest_diag.txt"
if diag_file.exists():
    diag_file.unlink(missing_ok=True)


_MULTIMODAL_VISION_FLAG = "multimodal_query_vision_enabled"


def multimodal_vision_flag_leaked(before, after) -> bool:
    """Shared leak-detection predicate for the query-vision flag guard.

    Single source of truth used BOTH by the autouse fixture below and by
    ``tests/test_settings_leak_guardrail.py`` (which imports this module), so
    the guard's detection logic is itself under test — see PRR-003.
    """
    return before != after


@pytest.fixture(autouse=True)
def _guard_multimodal_vision_flag():
    """Issue #462 (TEST-004 defect class): fail any test that leaves the
    query-vision feature flag mutated on the shared settings singleton.

    The historical defect: raw ``settings.multimodal_query_vision_enabled = X``
    assignments in test bodies leak feature state across tests in the same
    worker/process (xdist workers start from the config default). The sanctioned
    forms are ``patch.object(settings, ...)`` scopes and the save/tearDown
    restore pair in the owning test class — all of which restore the value
    before this fixture's after-check runs.
    """
    from app.config import settings

    before = getattr(settings, _MULTIMODAL_VISION_FLAG)
    yield
    after = getattr(settings, _MULTIMODAL_VISION_FLAG)
    if multimodal_vision_flag_leaked(before, after):
        pytest.fail(
            f"test leaked settings.{_MULTIMODAL_VISION_FLAG}: "
            f"{before!r} -> {after!r} (use patch.object(settings, ...) for "
            "scoped flag mutations — see tests/test_settings_leak_guardrail.py)"
        )


@pytest.fixture(autouse=True)
def _reset_admission_and_telemetry_singletons():
    """Reset the admission-controller and telemetry singletons between tests.

    Both are module-level globals (``app.services.admission._controller``,
    ``app.services.telemetry._singleton``) that persist across the full test
    run. Tests that patch Settings or construct custom controllers/telemetry
    instances would otherwise leak their wiring into later tests (the same
    class of cross-test pollution the rate-limiter and pool resets above
    guard against — swarm review LOW finding, issue #518 round 2).
    """
    for teardown in (False, True):
        try:
            from app.services.admission import reset_admission_controller

            reset_admission_controller()
        except (ImportError, AttributeError):
            pass
        try:
            from app.services.telemetry import reset_telemetry

            reset_telemetry()
        except (ImportError, AttributeError):
            pass
        if not teardown:
            yield


# ---------------------------------------------------------------------------
# Issue #565 quality gates that can fail (E06).
#
# Two guards close the async-defect classes the pre-#565 suite swallowed:
#
# 1. AST collection guard (pytest_pycollect_makemodule): an `async def test_*`
#    method inside a plain unittest.TestCase subclass is silently dropped by
#    pytest-asyncio's auto mode (zero assertions run, zero failures reported).
#    IsolatedAsyncioTestCase subclasses legitimately hold async tests and are
#    exempt. The guard fails collection with file:line instead.
#
# 2. Never-awaited warning guard (pytest_warning_recorded): CPython destroys a
#    never-awaited coroutine at statement end, so nothing survives to a
#    session-finish gc scan. Every uncaptured "coroutine ... was never awaited"
#    warning instead transits pytest's warning recorder — directly as
#    RuntimeWarning under default filters, or via sys.unraisablehook as a
#    pytest.PytestUnraisableExceptionWarning when `-W error::RuntimeWarning`
#    (addopts) makes the warn inside coroutine.__del__ raise. Warnings captured
#    inside a test's own `warnings.catch_warnings(record=True)` block never
#    reach this hook, so warning-capturing tests are structurally exempt.
#    sessionfinish fails the session when anything was recorded.
# ---------------------------------------------------------------------------

_NEVER_AWAITED_RECORDED: list = []

_ISOLATED_ASYNCIO_BASE = "IsolatedAsyncioTestCase"


def iter_async_testcase_methods(paths):
    """Yield (file, line, class, method) for async test methods in plain
    unittest.TestCase subclasses within the given paths (plan-critic finding 4:
    IsolatedAsyncioTestCase subclasses are excluded — they legitimately run
    async tests)."""
    import ast

    for path in paths:
        try:
            source = pathlib.Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        # Classify every class in the module to a fixpoint: isolated wins over
        # plain at every sweep, so a subclass of a local IsolatedAsyncioTestCase
        # base is exempt even when its base's *name* merely contains
        # "TestCase" (e.g. DraftResearchAsyncTestCase).
        classes = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node

        def _base_names(node):
            names = []
            for base in node.bases:
                if isinstance(base, ast.Attribute):
                    names.append(base.attr)
                elif isinstance(base, ast.Name):
                    names.append(base.id)
            return names

        plain: set = set()
        isolated: set = set()
        changed = True
        while changed:
            changed = False
            for name, node in classes.items():
                if name in isolated:
                    continue
                names = _base_names(node)
                if any(_ISOLATED_ASYNCIO_BASE in b for b in names) or any(b in isolated for b in names):
                    if name not in isolated:
                        isolated.add(name)
                        plain.discard(name)
                        changed = True
                elif name not in plain and (
                    any("TestCase" in b for b in names) or any(b in plain for b in names)
                ):
                    plain.add(name)
                    changed = True
        # Pass 2: async test methods inside plain TestCase classes.
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name not in plain:
                continue
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name.startswith("test"):
                    yield (str(path), item.lineno, node.name, item.name)


def pytest_pycollect_makemodule(module_path, parent):
    """Fail collection on async test methods inside plain TestCase subclasses
    (issue #565): pytest-asyncio auto mode drops them silently."""
    path = getattr(module_path, "path", None) or str(module_path)
    name = os.path.basename(str(path))
    if not (name.startswith("test_") and name.endswith(".py")):
        return None
    hits = list(iter_async_testcase_methods([str(path)]))
    if hits:
        details = "; ".join("%s:%s %s.%s" % (f, ln, cls, meth) for f, ln, cls, meth in hits)
        raise pytest.UsageError(
            "issue #565 gate: async test method(s) inside a sync unittest.TestCase are "
            "silently dropped by asyncio auto mode: %s" % details
        )
    return None


def never_awaited_warning_match(category, message):
    """True when a warning record is the un-awaited-coroutine class, on either
    capture channel: direct RuntimeWarning (default filters) or pytest's
    PytestUnraisableExceptionWarning wrapping the __del__-routed raise when
    `-W error::RuntimeWarning` is active (issue #565).

    Exemption: `AsyncMockMixin._execute_mock_call` is NOT matched. A blanket
    ``AsyncMock()`` test double leaks its internal call coroutine whenever a
    test tears down between the call and the await, the wrapper's name hides
    which child was dropped, and the GC-timed warning is routinely attributed
    to whichever test runs at deallocation time — an unreliable signal that
    would mis-blame unrelated tests. Real (non-mock) async methods keep the
    full gate: a production drop-the-await leaks the real method's coroutine
    and still fails the suite (proven by the frozen checks C6/C7).
    """
    text = str(message)
    if "never awaited" not in text:
        return False
    if "_execute_mock_call" in text:
        return False
    if category is RuntimeWarning:
        return True
    return "PytestUnraisableExceptionWarning" in str(category)


def pytest_warning_recorded(warning_message, when, nodeid, location):
    """Record uncaptured never-awaited coroutine warnings (issue #565)."""
    if never_awaited_warning_match(warning_message.category, warning_message.message):
        _NEVER_AWAITED_RECORDED.append(
            "%s @ %s: %s"
            % (
                type(warning_message.category).__name__,
                nodeid or when,
                warning_message.message,
            )
        )


def pytest_sessionfinish(session, exitstatus):
    """Fail the session if any never-awaited coroutine warning was recorded
    (issue #565): the suite must not pass while a coroutine was created and
    dropped anywhere outside a test's own warning-capture block."""
    if _NEVER_AWAITED_RECORDED:
        summary = "; ".join(_NEVER_AWAITED_RECORDED[:5])
        raise pytest.UsageError(
            "issue #565 gate: %d never-awaited coroutine warning(s) recorded: %s"
            % (len(_NEVER_AWAITED_RECORDED), summary)
        )
