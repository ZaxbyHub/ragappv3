"""Acceptance check AC4 (issue #1199, organizations leg): one pooled
checkout/release per request.

Every ``organizations.py`` handler runs the auth chain (``require_role`` ->
``get_current_active_user`` -> ``Depends(get_db)``) — one pooled SQLite
connection held for the whole request — and then ALSO checks out a SECOND
connection via ``conn = await pool.get_connection_async()`` in its own
try/finally. The ``users.py`` fix pattern (PR #1198,
``admin_reset_password``) is the precedent: the handler declares
``db: sqlite3.Connection = Depends(get_db)`` and FastAPI's per-request
dependency cache makes that the SAME connection the auth chain already
holds, so the pool sees exactly one checkout and one release per request —
on success, on auth denial (the auth dependency's own connection is
acquired and released as the request unwinds), and on a handler exception
(the dependency teardown still releases).

This file is the organizations sibling of
``test_p02c_users_pool_single_connection.py`` (the #1198 users-route
companion coverage, green at base): the users route already shares one
connection, the organizations route still holds two at base.

A counting wrapper over the live ``SQLiteConnectionPool`` records every
``get_connection``/``get_connection_async`` checkout and every
``release_connection``. Note the funnel: ``get_connection_async`` itself
submits exactly one ``get_connection`` call, so the ``checkout`` counter
counts each checkout once regardless of its sync/async entry point, while
``checkout_async`` only attributes the off-loop entry (diagnostic: after
the fix it must be zero, because every checkout rides the request-scoped
dependency).

- Success leg (GET /api/organizations, 200): RED at base (2 checkouts /
  2 releases), GREEN after the fix (1/1). Killed by re-migrating the
  ``list_organizations`` handler to ``pool.get_connection_async()`` in its
  own try/finally.
- Denial leg (GET with an invalid bearer, 401 before the handler body
  runs): GREEN at base and after the fix — the handler body never runs, so
  the re-migration mutation does not touch this leg; it is killed by
  removing the ``finally: pool.release_connection(conn)`` from ``get_db``
  in ``app/api/deps.py`` (release count 0), i.e. an auth-chain connection
  leak on the failure path.
- Exception leg (POST /api/organizations with the slug helper patched to
  raise, handler aborts inside its own try): RED at base (2 checkouts /
  2 releases — the handler's finally releases its second connection, but
  the count is still 2), GREEN after the fix (1/1). Killed by re-migrating
  the ``create_organization`` handler to ``pool.get_connection_async()``.
- In-handler 403 leg (GET /api/organizations/{id} as an authenticated
  NON-member, denied by the handler after entry): RED at base (the handler
  checks out its second connection BEFORE the membership check denies),
  GREEN after the fix (1/1). Killed by re-migrating the
  ``get_organization`` handler to ``pool.get_connection_async()``.
- Post-BEGIN rollback leg (POST /api/organizations with BOTH candidate
  slugs pre-seeded, so the INSERT raises IntegrityError, the handler runs
  its explicit ``conn.rollback()``, and the retry also collides → 409):
  RED at base (2 checkouts / 2 releases), GREEN after the fix (1/1 — the
  rollback executes on the single request-scoped connection). Killed by
  re-migrating the ``create_organization`` handler to
  ``pool.get_connection_async()``.
"""

import os
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes.organizations import router as organizations_router
from app.config import settings
from app.models.database import get_pool, run_migrations
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture()
def counting_pool(monkeypatch, tmp_path):
    """Migrated temp DB plus a counting wrapper over the live pool.

    Mirrors the test_b01_org_slug.py harness: the real migration pipeline
    builds the database, one admin user is seeded, and the pool instance
    that both ``get_db`` and the route handlers resolve through
    ``get_pool(str(settings.sqlite_path))`` has its checkout/release
    methods wrapped so every checkout and release is counted.
    """
    from app.models.database import _pool_cache, _pool_cache_lock

    # Clear pool cache BEFORE setting up the new database.
    with _pool_cache_lock:
        for _path, pool_obj in list(_pool_cache.items()):
            pool_obj.close_all()
        _pool_cache.clear()

    db_path = str(tmp_path / "app.db")

    # Patch settings first so settings.vaults_dir points at the (absent)
    # temp tree and the vault-path step of run_migrations is a no-op.
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role,"
        " is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role,"
        " is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (3, "member1", pw, "Member One", "member"),
    )
    conn.execute(
        "INSERT INTO organizations (id, name, description, slug, created_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (1, "Seeded Org", "", "seeded-org", 2),
    )
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role)"
        " VALUES (?, ?, 'owner')",
        (1, 2),
    )
    conn.commit()
    conn.close()

    pool = get_pool(str(settings.sqlite_path))
    counts = {"checkout": 0, "checkout_async": 0, "release": 0, "db_path": db_path}
    real_get = pool.get_connection
    real_get_async = pool.get_connection_async
    real_release = pool.release_connection

    def counting_get(*args, **kwargs):
        counts["checkout"] += 1
        return real_get(*args, **kwargs)

    async def counting_get_async(*args, **kwargs):
        # get_connection_async funnels into exactly one get_connection call,
        # which counts the checkout; this counter only attributes the entry.
        counts["checkout_async"] += 1
        return await real_get_async(*args, **kwargs)

    def counting_release(released, *args, **kwargs):
        counts["release"] += 1
        return real_release(released, *args, **kwargs)

    monkeypatch.setattr(pool, "get_connection", counting_get)
    monkeypatch.setattr(pool, "get_connection_async", counting_get_async)
    monkeypatch.setattr(pool, "release_connection", counting_release)

    yield counts

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]


@pytest.fixture()
def client(counting_pool):
    """Test client with the organizations router against the counting pool.

    ``raise_server_exceptions=False`` (the proven pattern from the #1198
    exception-path companion test) so the handler-exception leg observes a
    500 response instead of an exception surfacing into the test, while the
    dependency teardown still runs and releases the request connection.
    """
    app = FastAPI()
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app, raise_server_exceptions=False)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


def admin_token():
    return create_access_token(
        2, "admin1", "admin", client_fingerprint=compute_client_fingerprint("")
    )


def admin_headers():
    return {"Authorization": f"Bearer {admin_token()}"}


def member_token():
    return create_access_token(
        3, "member1", "member", client_fingerprint=compute_client_fingerprint("")
    )


def _seed_extra_rows(db_path, rows_sql):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    for sql, params in rows_sql:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


def _assert_single_checkout_release(counts):
    assert counts["checkout"] == 1, (
        f"1199: expected exactly ONE pooled checkout per request; got "
        f"{counts['checkout']} (checkout_async={counts['checkout_async']}, "
        f"release={counts['release']}) — a handler-body "
        f"pool.get_connection_async() stacks a second connection on top of "
        f"the auth chain's Depends(get_db) connection"
    )
    assert counts["release"] == 1, (
        f"1199: expected exactly ONE pooled release per request; got "
        f"{counts['release']} (checkout={counts['checkout']}, "
        f"checkout_async={counts['checkout_async']})"
    )


def test_success_single_checkout_release(client, counting_pool):
    """AC4 success leg: a 200 request checks out and releases exactly once.

    RED at base (auth chain 1 + handler-body 1 = 2 checkouts / 2 releases),
    GREEN after the fix (the handler's Depends(get_db) reuses the cached
    auth-chain connection: 1/1). Killed by re-migrating the
    ``list_organizations`` handler to ``pool.get_connection_async()``.
    """
    response = client.get("/api/organizations", headers=admin_headers())
    assert response.status_code == 200
    _assert_single_checkout_release(counting_pool)


def test_denial_single_checkout_release(client, counting_pool):
    """AC4 denial leg: a 401 still releases the auth connection exactly once.

    The request fails the auth dependency before the handler body runs, so
    the counted checkout/release is the auth chain's own
    ``Depends(get_db)`` connection, acquired and released as the request
    unwinds. GREEN at base and GREEN after the fix (guard leg — the
    re-migration mutation does not reach it). Killed by removing the
    ``finally: pool.release_connection(conn)`` from ``get_db`` in
    ``app/api/deps.py`` (release count 0 instead of 1).
    """
    response = client.get(
        "/api/organizations",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert response.status_code == 401
    _assert_single_checkout_release(counting_pool)


def test_handler_exception_single_checkout_release(
    client, counting_pool, monkeypatch
):
    """AC4 exception leg: an aborting handler still releases exactly once.

    The slug helper is patched to raise after the handler body checked out
    its connection, so the request dies inside the handler's own
    try/finally. RED at base (2 checkouts / 2 releases), GREEN after the
    fix (1/1: the dependency teardown releases the single request-scoped
    connection). Killed by re-migrating the ``create_organization``
    handler to ``pool.get_connection_async()``.
    """

    def _boom(name):
        raise RuntimeError("1199AC4 injected slug failure")

    monkeypatch.setattr(
        "app.api.routes.organizations.generate_org_slug", _boom
    )
    response = client.post(
        "/api/organizations",
        json={"name": "boom org"},
        headers=admin_headers(),
    )
    assert response.status_code == 500
    _assert_single_checkout_release(counting_pool)


def test_inhandler_403_single_checkout_release(client, counting_pool):
    """AC4 in-handler 403 leg: denial AFTER entry still checks out once.

    The member-role caller authenticates (require_role("member") passes) and
    the handler body runs, checking out its second connection BEFORE the
    membership check denies with 403. RED at base (2 checkouts / 2 releases),
    GREEN after the fix (1/1 — the denial runs on the single request-scoped
    connection). Killed by re-migrating the ``get_organization`` handler to
    ``pool.get_connection_async()``.
    """
    response = client.get(
        "/api/organizations/1",
        headers={"Authorization": f"Bearer {member_token()}"},
    )
    assert response.status_code == 403
    _assert_single_checkout_release(counting_pool)


def test_postbegin_rollback_single_checkout_release(client, counting_pool):
    """AC4 post-BEGIN rollback leg: an in-transaction failure rolls back once.

    Both candidate slugs for the requested name are pre-seeded, so the
    handler's INSERT raises IntegrityError, its explicit
    ``conn.rollback()`` runs, the suffixed retry collides too, and the
    handler maps the re-raised IntegrityError to 409. RED at base (2
    checkouts / 2 releases), GREEN after the fix (1/1 — the BEGIN-less
    rollback executes on the single request-scoped connection). Killed by
    re-migrating the ``create_organization`` handler to
    ``pool.get_connection_async()``.
    """
    _seed_extra_rows(
        counting_pool["db_path"],
        [
            (
                "INSERT INTO organizations (id, name, description, slug,"
                " created_by) VALUES (?, ?, ?, ?, ?)",
                (10, "Taken Name", "", "taken-name", 2),
            ),
            (
                "INSERT INTO organizations (id, name, description, slug,"
                " created_by) VALUES (?, ?, ?, ?, ?)",
                (11, "Taken Name Two", "", "taken-name-2", 2),
            ),
        ],
    )
    # _generate_slug("Taken Name") yields "taken-name"; both it and the
    # "-2" retry are pre-seeded, forcing IntegrityError twice -> 409.
    response = client.post(
        "/api/organizations",
        json={"name": "Taken Name"},
        headers=admin_headers(),
    )
    assert response.status_code == 409
    _assert_single_checkout_release(counting_pool)
