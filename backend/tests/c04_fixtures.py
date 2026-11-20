"""Shared fixtures for the c04 acceptance-check family (issues #709/#890/#946/#900).

One home for the three fixtures the c04 modules previously duplicated
near-verbatim (fresh_db, seeded_env, prompts_client). Teardown is guarded on
every failure path: ``conn`` is pre-bound to ``None`` so a ``run_migrations``
or ``sqlite3.connect`` failure surfaces its OWN traceback and still removes
the temp database, and ``seeded_env`` wraps from ``mkdtemp`` through ``yield``
in try/finally so a pre-yield raise still drains the pool entry and removes
the temp directory.
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.prompts import router as prompts_router
from app.models.database import run_migrations

_ADMIN_TOKEN = "test-admin-secret"


@pytest.fixture
def fresh_db():
    """Temp DB with the full schema, org 10 seeded (FK target for A/B).

    Guarded teardown (issue #946): ``conn`` is bound to None before the try
    so a schema or connect failure reports the original error and the temp
    database is still removed (the c03b pattern).
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = None
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        yield conn
    finally:
        # conn is bound only after run_migrations succeeds; guard so a schema
        # failure surfaces its own traceback instead of an UnboundLocalError.
        if conn is not None:
            conn.close()
        os.unlink(path)


@pytest.fixture()
def seeded_env(monkeypatch):
    """Temp DB (full schema) + patched settings, prompts-route flavor.

    ``settings.data_dir`` drives ``settings.sqlite_path`` (a property), so
    the route handlers' ``get_pool(str(settings.sqlite_path))`` checkouts
    land on the real pool over the temp database.

    Guarded teardown (issues #946/#900): the try/finally starts right after
    mkdtemp, so ANY pre-yield raise (pool-cache drain, run_migrations,
    monkeypatching) still drains this run's pool entry and removes the temp
    directory.
    """
    temp_dir = tempfile.mkdtemp(prefix="c04_shared_")
    db_path = str(Path(temp_dir) / "app.db")
    try:
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        run_migrations(db_path)

        monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
        # require_scope("admin:config") for the prompts router: a static admin
        # token mapped to the scope server-side (test_prompt_ab_variants pattern).
        monkeypatch.setattr(
            "app.config.settings.admin_secret_token", _ADMIN_TOKEN
        )
        monkeypatch.setattr(
            "app.config.settings.admin_token_scopes",
            {_ADMIN_TOKEN: ["admin:config"]},
        )

        env = {"db_path": db_path, "temp_dir": temp_dir}
        yield env
    finally:
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            if db_path in _pool_cache:
                _pool_cache[db_path].close_all()
                del _pool_cache[db_path]
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture()
def prompts_client(seeded_env):
    """App exposing the prompts router with the module-managed CSRF override."""
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc
