"""Issue #301 regression: the pooled SQLite connection used for /chat/stream auth
must be RELEASED before the SSE stream begins (the LLM generation), not held
across it.

Pre-fix, chat_stream resolved auth via Depends(get_current_active_user) +
Depends(get_evaluate_policy), both carrying the request-scoped yield-dependency
get_db. FastAPI deferred get_db's teardown until the StreamingResponse body
finished — pinning one pooled connection across the entire LLM generation and
capping concurrency at the pool size (~10).

These tests assert the REAL behavior (not a tautology):
  - mid-stream: outstanding connections == 0 DURING generation (the connection
    acquired for auth is already back in the pool when the first chunk streams).
  - concurrency: max_size concurrent streams (with simulated LLM latency) all
    succeed with no pool-exhaustion RuntimeError / 500, and the peak outstanding
    connection count during generation is 0. (Uses max_size, not max_size+1:
    the legacy sync get_connection() blocks the single-threaded event loop when
    over-subscribed — a pre-existing limit separate from #301.)

The stream-route auth tests (must_change_password -> 403, refresh-token -> 401)
exercise the REAL get_stream_auth dependency (no override), minting real tokens
against a seeded DB so the auth path runs end-to-end.
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.api.deps import get_rag_engine
from app.main import app
from app.models.database import (
    SQLiteConnectionPool,
    _pool_cache,
    _pool_cache_lock,
    init_db,
    run_migrations,
)
from app.services.auth_service import create_access_token

# ---------------------------------------------------------------------------
# Instrumented pool: counts how many connections are currently checked out.
# ---------------------------------------------------------------------------


class CountingPool:
    """Wraps a real SQLiteConnectionPool, tracking outstanding connections.

    The streaming auth boundaries (get_stream_auth, wiki_events_stream) acquire
    connections via ``pool.connection()`` (a context manager), so counting is
    done in the CM enter/exit — NOT by wrapping get_connection/release_connection
    (the real CM delegates to the real pool's own get/release and would bypass
    those wrappers). ``max_outstanding`` captures the peak across the request,
    which the concurrency test asserts stays 0 during generation.
    """

    def __init__(self, real_pool: SQLiteConnectionPool):
        self._real = real_pool
        self.outstanding = 0
        self._lock = threading.Lock()
        self.max_outstanding = 0

    def _acquired(self):
        with self._lock:
            self.outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self.outstanding)

    def _released(self):
        with self._lock:
            self.outstanding -= 1

    @property
    def max_size(self):
        return self._real.max_size

    # The context manager the streaming auth boundaries use.
    def connection(self):
        real_cm = self._real.connection()

        class _CountingCM:
            def __init__(self, outer):
                self._outer = outer
                self._real_cm = real_cm

            def __enter__(self):
                c = self._real_cm.__enter__()
                self._outer._acquired()
                return c

            def __exit__(self, *exc):
                try:
                    return self._real_cm.__exit__(*exc)
                finally:
                    self._outer._released()

        return _CountingCM(self)

    def close_all(self):
        self._real.close_all()


@pytest.fixture
def authed_stream_env(monkeypatch):
    """Seed a temp DB with an admin user + vault, mint a real access token,
    install a CountingPool as app.state.db_pool, and mock the RAG engine.

    Returns (token, counting_pool, snapshot_holder). The mock engine's first
    chunk captures the pool's outstanding count into snapshot_holder so the test
    can assert the connection was already released mid-stream.
    """
    temp_dir = tempfile.mkdtemp(prefix="streamrelease-")
    db_path = str(Path(temp_dir) / "app.db")
    original_data_dir = app.state.db_pool if hasattr(app.state, "db_pool") else None

    # Point settings at the temp DB.
    from app.config import settings

    original_jwt_secret = settings.jwt_secret_key
    original_users_enabled = settings.users_enabled
    original_data_dir_setting = settings.data_dir
    settings.data_dir = Path(temp_dir)
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True

    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            try:
                p.close_all()
            except Exception:
                pass
        _pool_cache.clear()

    init_db(db_path)
    run_migrations(db_path)

    # Seed an admin user (password_changed_at column may exist post-migration).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1, 'admin', 'x', 'Admin', 'superadmin', 1)"
        )
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.commit()
    finally:
        conn.close()

    real_pool = SQLiteConnectionPool(db_path, max_size=10)
    counting = CountingPool(real_pool)
    app.state.db_pool = counting

    token = create_access_token(1, "admin", "superadmin")

    snapshot = {"during_stream": None}

    mock_engine = MagicMock()

    def make_query():
        async def mock_query(*args, **kwargs):
            # On the very first chunk, snapshot how many pool connections are
            # outstanding. If #301 is fixed this is 0 (auth conn released).
            if snapshot["during_stream"] is None:
                snapshot["during_stream"] = counting.outstanding
            yield {"type": "content", "content": "hi"}
            yield {"type": "done", "sources": [], "memories_used": []}

        return mock_query

    mock_engine.query = make_query()
    app.dependency_overrides[get_rag_engine] = lambda: mock_engine

    # Bypass CSRF (POST endpoint) and model-ready check so the request reaches auth.
    from app.security import csrf_protect

    app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
    from app.api.deps import require_model_ready

    app.dependency_overrides[require_model_ready] = lambda: True

    client = TestClient(app)

    yield {
        "token": token,
        "pool": counting,
        "snapshot": snapshot,
        "client": client,
    }

    # Teardown
    app.dependency_overrides.pop(get_rag_engine, None)
    app.dependency_overrides.pop(csrf_protect, None)
    app.dependency_overrides.pop(require_model_ready, None)
    settings.jwt_secret_key = original_jwt_secret
    settings.users_enabled = original_users_enabled
    settings.data_dir = original_data_dir_setting
    if original_data_dir is not None:
        app.state.db_pool = original_data_dir
    counting.close_all()
    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            try:
                p.close_all()
            except Exception:
                pass
        _pool_cache.clear()
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Mid-stream release assertion
# ---------------------------------------------------------------------------


def test_stream_releases_connection_before_first_chunk(authed_stream_env):
    """The auth connection is back in the pool by the time the first SSE chunk
    is produced (i.e. during the LLM generation, zero pool connections are held).

    This is the gold-standard #301 assertion: it catches the bug where the
    yield-dependency's teardown was deferred to the end of the stream.
    """
    env = authed_stream_env
    response = env["client"].post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "test"}], "vault_id": 1},
        headers={"Authorization": f"Bearer {env['token']}"},
    )
    assert response.status_code == 200, response.text
    # During streaming, no pool connection was held.
    assert env["snapshot"]["during_stream"] == 0, (
        f"Expected 0 outstanding connections during streaming, got "
        f"{env['snapshot']['during_stream']}. The auth connection was NOT "
        f"released before the stream began (issue #301 regression)."
    )


# ---------------------------------------------------------------------------
# Concurrency: with realistic LLM latency, max_size parallel streams must NOT
# exhaust the pool (the #301 bug would deadlock these for the full latency).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_concurrent_streams_with_llm_latency_do_not_exhaust_pool(
    monkeypatch, authed_stream_env
):
    """max_size concurrent streams, each simulating LLM generation latency, all
    succeed and the pool is NOT held during generation.

    The discriminating assertion is `during_stream == 0` (the snapshot captured
    inside the first stream chunk): under the OLD code the auth connection was
    held across the simulated generation, so outstanding would be >0 during the
    stream; under the fix it is 0. The "all return 200" status check is a
    secondary sanity check — on its own it would NOT distinguish fixed vs buggy
    at exactly max_size concurrency (10 requests on a size-10 pool do not
    exhaust even under the old holding behavior); the snapshot is what proves
    #301 is resolved.

    NOTE: the legacy pool's get_connection() is a synchronous blocking call
    (Queue.get(timeout=5)); over-subscribing beyond max_size with truly
    simultaneous arrivals deadlocks the single-threaded event loop regardless
    of #301. That is a separate, pre-existing sync-in-async characteristic, so
    this test uses max_size (not max_size+1) concurrency — exactly the ceiling
    #301 describes.
    """
    import httpx

    env = authed_stream_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    payload = {"messages": [{"role": "user", "content": "test"}], "vault_id": 1}
    num = env["pool"].max_size  # 10 — the ceiling #301 imposed

    # Simulate LLM generation latency in the mock engine. Under the OLD code the
    # auth connection would be held across this sleep, saturating the pool.
    generation_peak = {"value": 0}

    async def slow_query(*args, **kwargs):
        await asyncio.sleep(0.5)  # simulated generation latency
        # Snapshot the outstanding count DURING generation. Captured by every
        # concurrent generator (not just the first), and the max is tracked —
        # this is the generation-phase peak, which is the real #301
        # discriminator (the auth-phase peak legitimately reaches num).
        cur = env["pool"].outstanding
        if env["snapshot"]["during_stream"] is None:
            env["snapshot"]["during_stream"] = cur
        if cur > generation_peak["value"]:
            generation_peak["value"] = cur
        yield {"type": "content", "content": "hi"}
        yield {"type": "done", "sources": [], "memories_used": []}

    mock_engine = MagicMock()
    mock_engine.query = slow_query
    app.dependency_overrides[get_rag_engine] = lambda: mock_engine

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:

        async def one_request():
            return await client.post("/api/chat/stream", json=payload, headers=headers)

        results = await asyncio.gather(
            *(one_request() for _ in range(num)), return_exceptions=True
        )

    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == [], f"Concurrent stream requests raised: {errors}"

    statuses = [r.status_code for r in results]
    non_200 = [s for s in statuses if s != 200]
    assert non_200 == [], (
        f"Expected all {num} concurrent streams to return 200, got non-200: {non_200}. "
        f"A 500 here means pool exhaustion (issue #301 regression)."
    )
    # During generation the pool was not saturated by held auth connections.
    assert env["snapshot"]["during_stream"] == 0, (
        f"Expected 0 outstanding connections during generation, got "
        f"{env['snapshot']['during_stream']}."
    )
    # Stronger property: the PEAK outstanding count observed DURING generation
    # (across all concurrent generators, not just the first to hit the mock) was
    # 0. This removes the first-write-only limitation of the `during_stream`
    # snapshot. Under the OLD code each stream held its auth connection across
    # the simulated latency, so generation_peak would have been num (10); under
    # the fix the auth connections are released before generation, so it is 0.
    # (The overall max_outstanding can legitimately reach num during the auth
    # phase — that's the pool being used as designed, transiently.)
    assert generation_peak["value"] == 0, (
        f"Peak outstanding connections observed during generation was "
        f"{generation_peak['value']}; expected 0. A non-zero value means auth "
        f"connections were held across the LLM generation (issue #301 regression)."
    )


# ---------------------------------------------------------------------------
# Stream-route auth enforcement (PRR-007, PRR-008): the refactor moved the
# invocation point of _resolve_active_user (now called by get_stream_auth).
# These exercise the REAL get_stream_auth (no override) for two security-
# relevant rejection paths that previously had no stream-route coverage.
# ---------------------------------------------------------------------------


def _build_authed_client(user_row_sql: str, jwt_payload_overrides: dict | None = None):
    """Build a TestClient whose app.state.db_pool backs a temp DB seeded with a
    user, and return (client, access_token). Exercises the REAL get_stream_auth
    (no override) so _resolve_active_user runs end-to-end."""
    from datetime import datetime, timedelta, timezone

    import jwt as pyjwt

    from app.api.deps import get_rag_engine, require_model_ready
    from app.config import settings
    from app.security import csrf_protect
    from app.services.auth_service import get_jwt_config

    temp_dir = tempfile.mkdtemp(prefix="streamauth-")
    db_path = str(Path(temp_dir) / "app.db")

    saved = {
        "data_dir": settings.data_dir,
        "jwt": settings.jwt_secret_key,
        "users_enabled": settings.users_enabled,
        "db_pool": getattr(app.state, "db_pool", None),
    }
    settings.data_dir = Path(temp_dir)
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True

    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            try:
                p.close_all()
            except Exception:
                pass
        _pool_cache.clear()

    init_db(db_path)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(user_row_sql)
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
        conn.commit()
    finally:
        conn.close()

    app.state.db_pool = SQLiteConnectionPool(db_path, max_size=5)

    secret, algorithm = get_jwt_config()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "1",
        "username": "u",
        "role": "member",
        "exp": now + timedelta(minutes=30),
        "type": "access",
        "iat": now,
        "jti": "test-jti",
    }
    if jwt_payload_overrides:
        payload.update(jwt_payload_overrides)
    token = pyjwt.encode(payload, secret, algorithm=algorithm)

    mock_engine = MagicMock()

    async def _q(*a, **k):
        yield {"type": "done", "sources": [], "memories_used": []}

    mock_engine.query = _q
    app.dependency_overrides[get_rag_engine] = lambda: mock_engine
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
    app.dependency_overrides[require_model_ready] = lambda: True

    def _cleanup():
        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(require_model_ready, None)
        settings.data_dir = saved["data_dir"]
        settings.jwt_secret_key = saved["jwt"]
        settings.users_enabled = saved["users_enabled"]
        app.state.db_pool.close_all()
        with _pool_cache_lock:
            for p in list(_pool_cache.values()):
                try:
                    p.close_all()
                except Exception:
                    pass
            _pool_cache.clear()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return TestClient(app), token, _cleanup


def test_stream_rejects_must_change_password_user():
    """A must_change_password user hitting /chat/stream must get 403.

    The path-exemption (deps.py) runs inside _resolve_active_user, now called by
    get_stream_auth. /api/chat/stream is not in the exempt set, so the check
    must fire on the stream route (PRR-007).
    """
    client, token, cleanup = _build_authed_client(
        "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active, must_change_password) "
        "VALUES (1, 'u', 'x', 'U', 'member', 1, 1)"
    )
    try:
        resp = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}], "vault_id": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, (
            f"must_change_password user must get 403 on /chat/stream, got "
            f"{resp.status_code}: {resp.text}"
        )
        assert resp.json().get("detail") == "must_change_password"
    finally:
        cleanup()


def test_stream_rejects_refresh_token():
    """A refresh-token (type != access) must be rejected on /chat/stream with 401.

    _resolve_active_user enforces token type; this covers the stream-route
    invocation of that check end-to-end (PRR-008).
    """
    client, token, cleanup = _build_authed_client(
        "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (1, 'u', 'x', 'U', 'member', 1)",
        jwt_payload_overrides={"type": "refresh"},
    )
    try:
        resp = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}], "vault_id": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401, (
            f"refresh token must be rejected (401) on /chat/stream, got "
            f"{resp.status_code}: {resp.text}"
        )
        assert resp.json().get("detail") == "token_invalid"
    finally:
        cleanup()
