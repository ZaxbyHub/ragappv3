"""
Regression test for the auth dependency override branch in
``get_current_user_or_service_account`` (``backend/app/api/deps.py``).

History / why this file exists
------------------------------
Issue #312 asked for a regression test that sets
``app.dependency_overrides[get_evaluate_policy]`` to an ``async def`` to guard
against the historical anti-pattern where an override branch used
``inspect.isawaitable()`` instead of ``inspect.iscoroutine()`` (documented in
``.agents/skills/authz-bridging-exceptions/SKILL.md``).

Verification of the current code (2026-07-15) shows:

1. ``inspect.isawaitable`` appears NOWHERE in ``backend/`` — the bug is absent.
2. There is NO override branch keyed on ``get_evaluate_policy`` anywhere in
   application code. ``get_evaluate_policy`` is a plain factory returning a
   closure; FastAPI's DI resolver handles overrides of it directly, with no
   ``iscoroutine``/``isawaitable`` decision point. A test overriding
   ``get_evaluate_policy`` with an ``async def`` would therefore be *vacuous*:
   it would pass on both the buggy and the fixed code because there is no
   branch to exercise.

3. The ONE real override branch is in ``get_current_user_or_service_account``
   (deps.py:550-557), keyed on ``get_current_active_user``::

       _override = request.app.dependency_overrides.get(get_current_active_user)
       if _override is not None:
           _result = _override()
           user = await _result if inspect.iscoroutine(_result) else _result
           return user

   It already uses the correct ``inspect.iscoroutine``.

This file therefore ships the *meaningful* variant of #312: it exercises the
REAL override branch with a module-scope ``async def`` override (not a sync
lambda) that calls another ``async def`` (matching AC-2 of the skill), and it
pins the literal ``isawaitable``-absence invariant via source inspection.

What is falsified, and what is NOT
----------------------------------
Two distinct bug classes touch this branch; the tests below split them so each
is guarded by the right kind of assertion:

1. **Drop-the-await bug** (the coroutine is never awaited, so ``user`` is a raw
   coroutine object). Falsified *behaviorally* by the 200/403 pair below: a raw
   coroutine handed to ``_evaluate_policy`` raises ``AttributeError`` on
   ``principal.get("id")`` and the route 500s instead of returning 200/403.
   Verified by mutation: changing deps.py:556 to ``user = _result`` makes both
   behavioral tests fail.

2. **``isawaitable``-vs-``iscoroutine`` bug** (the literal #312 concern).
   IMPORTANT: these two predicates return the SAME value on a coroutine object
   (both True), because the override is *called* at deps.py:555 before the
   check. So swapping ``iscoroutine`` → ``isawaitable`` leaves the behavioral
   tests GREEN — it is NOT falsified by the 200/403 pair. This invariant is
   guarded only by the source-inspection test
   ``test_override_branch_uses_iscoroutine_not_isawaitable`` (a source-string
   tripwire, not a behavioral test). That test catches a revert to
   ``isawaitable``; it would also fail on a benign rename, so treat it as a
   tripwire, not proof of behavior.

A purely behavioral falsifier for bug class 2 would require a code path where
``inspect.isawaitable`` is evaluated on an *uncalled* ``async def`` function
(where the two predicates diverge) — such a path does not exist in current
deps.py. So bug class 2 is pinned by source inspection; bug class 1 is pinned
behaviorally. Both are documented honestly here.
"""

import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (mirrors test_service_account_authenticates_to_routes.py)
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_current_active_user,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_evaluate_policy,
    get_secret_manager,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db, run_migrations

# ---------------------------------------------------------------------------
# Module-scope async def overrides (the point of #312).
#
# These are `async def` functions defined at MODULE scope — NOT sync lambdas.
# AC-2 of the authz-bridging-exceptions skill: "sets it to an `async def`
# function that calls another `async def` function". The override calls
# another async def (`_superadmin_user_coro`) to satisfy that wording.
# ---------------------------------------------------------------------------


async def _superadmin_user_coro():
    """Inner async def — the 'another async def' that the override calls."""
    return {
        "id": 1,
        "username": "superadmin",
        "role": "superadmin",
        "is_active": True,
        "is_service_account": False,
    }


async def _override_authorized_user():
    """Override value for get_current_active_user — an async def, not a lambda.

    Calling this yields a coroutine; the deps.py override branch must
    `inspect.iscoroutine`-detect it and await it to recover the dict below.
    """
    return await _superadmin_user_coro()


async def _forbidden_member_coro():
    """Inner async def returning a non-superadmin user with no vault access."""
    return {
        "id": 999,
        "username": "outsider",
        "role": "member",
        "is_active": True,
        "is_service_account": False,
    }


async def _override_unauthorized_user():
    """Override value: async def returning a member with no vault 1 membership.

    `_evaluate_policy` will look up vault_members for (user 999, vault 1),
    find nothing, and return False → 403. This proves the resolved principal
    flows into `evaluate` (it is not bypassed or replaced with None).
    """
    return await _forbidden_member_coro()


class TestAuthOverrideAsyncPath(unittest.TestCase):
    """Exercise the async-def override path on get_current_user_or_service_account."""

    def setUp(self):
        self._original_jwt_env = os.environ.get("JWT_SECRET_KEY")
        os.environ["JWT_SECRET_KEY"] = "test-auth-override-async-key-32chars!"

        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.users_enabled = True
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = None
        self._mock_embedding_service = MagicMock()
        self._mock_embedding_service.embed_single = AsyncMock(return_value=[0.0] * 384)
        self._mock_db_pool = self._connection_pool
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.is_running = True
        self._mock_background_processor.enqueue = AsyncMock()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: self._mock_embedding_service
        app.dependency_overrides[get_db_pool] = lambda: self._mock_db_pool
        app.dependency_overrides[get_background_processor] = lambda: self._mock_background_processor
        _mock_sm = MagicMock()
        _mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")
        app.dependency_overrides[get_secret_manager] = lambda: _mock_sm

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM users WHERE id != 0")
            conn.execute("DELETE FROM service_accounts")

            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", "x", "Super Admin", "superadmin"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (1, "Test Vault", "A test vault"),
            )
            # Document in vault 1 so list_document_tags can reach its body after the
            # permission check passes.
            conn.execute(
                "INSERT OR IGNORE INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "doc.txt", "/uploads/doc.txt", 100, "indexed", 0, 1),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

    def tearDown(self):
        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)
        app.dependency_overrides.pop(get_secret_manager, None)
        app.dependency_overrides.pop(get_current_active_user, None)

        self.client.close()
        self._connection_pool.close_all()

        if self._original_jwt_env is None:
            os.environ.pop("JWT_SECRET_KEY", None)
        else:
            os.environ["JWT_SECRET_KEY"] = self._original_jwt_env

        try:
            shutil.rmtree(self._temp_dir)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Primary behavioral falsifier: the 200 / 403 pair.
    # ------------------------------------------------------------------

    def test_async_override_resolves_and_allows(self):
        """An `async def` override (not a lambda) resolves to the dict and the
        route returns 200.

        This requires the deps.py override branch to detect the coroutine and
        await it. If the await is dropped or `iscoroutine` no longer matches,
        `user` is a coroutine and `_require_vault_read` breaks.
        """
        app.dependency_overrides[get_current_active_user] = _override_authorized_user

        resp = self.client.get("/api/tags/documents/1", params={"vault_id": 1})
        self.assertEqual(
            resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.text}"
        )
        self.assertEqual(resp.json(), {"tags": []})

    def test_async_override_unauthorized_denies(self):
        """An `async def` override returning an unauthorized member yields 403.

        Proves the RESOLVED principal dict flows into `evaluate` (which checks
        vault_members and denies) — the auth path is not bypassed.
        """
        app.dependency_overrides[get_current_active_user] = _override_unauthorized_user

        resp = self.client.get("/api/tags/documents/1", params={"vault_id": 1})
        self.assertEqual(
            resp.status_code, 403, f"expected 403, got {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # Source-inspection tripwires: pin the invariants the literal #312 names.
    #
    # NOTE: these are source-STRING guards, NOT behavioral tests. They catch a
    # revert to `isawaitable`, but they also fail on a benign rename (e.g.
    # `from inspect import iscoroutine`). They exist because the
    # isawaitable-vs-iscoroutine bug is NOT behaviorally falsifiable here — see
    # the module docstring "What is falsified, and what is NOT".
    # ------------------------------------------------------------------

    def test_override_branch_uses_iscoroutine_not_isawaitable(self):
        """Source-string tripwire: the override branch source contains
        `iscoroutine` and the string `isawaitable` appears nowhere in deps.py.

        This is the ONLY test that catches an `iscoroutine`→`isawaitable` revert
        (verified by mutation: swapping the predicate leaves the behavioral
        200/403 tests green because both predicates return True on a coroutine
        object). It also fails on benign renames — treat as a tripwire.
        """
        from app.api import deps

        src = inspect.getsource(deps.get_current_user_or_service_account)
        self.assertIn("iscoroutine", src)
        self.assertNotIn("isawaitable", src)

        deps_file = Path(deps.__file__).read_text()
        self.assertNotIn("isawaitable", deps_file)

    def test_get_evaluate_policy_has_no_override_branch(self):
        """Document that `get_evaluate_policy` has no `dependency_overrides`
        read — i.e. #312's literal `dependency_overrides[get_evaluate_policy]`
        surface does not exist in application code. FastAPI resolves such
        overrides directly, with no iscoroutine/isawaitable decision point.

        If a future change adds an override branch to get_evaluate_policy,
        this test should be updated to cover it (and to assert it uses
        iscoroutine).
        """
        from app.api import deps

        src = inspect.getsource(deps.get_evaluate_policy)
        self.assertNotIn("dependency_overrides", src)
        self.assertNotIn("isawaitable", src)
        self.assertNotIn("iscoroutine", src)


if __name__ == "__main__":
    unittest.main()
