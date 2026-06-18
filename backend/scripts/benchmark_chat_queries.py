"""Benchmark chat query latency against the FastAPI app.

Targets FR-007. Uses mocks/no live LLM and the same dependency-override /
SimpleConnectionPool pattern as the backend tests so it is CI-stable.

Run:
    python scripts/benchmark_chat_queries.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import httpx
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit("httpx is required for the benchmark script") from exc

from _db_pool import SimpleConnectionPool  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
    get_vector_store,
)
from app.config import settings  # noqa: E402
from app.limiter import limiter  # noqa: E402
from app.main import app  # noqa: E402
from app.security import csrf_protect  # noqa: E402
from app.services.auth_service import create_access_token  # noqa: E402

DEFAULT_CONCURRENCY_LEVELS = [5, 10, 20]
DEFAULT_ITERATIONS = 1
DEFAULT_WARMUP = 1


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    index = (len(sorted_values) - 1) * (percentile / 100.0)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _report(label: str, latencies: list[float]) -> None:
    sorted_latencies = sorted(latencies)
    print(f"Concurrent users: {label}")
    print(f"  p50: {_percentile(sorted_latencies, 50):.3f}s")
    print(f"  p95: {_percentile(sorted_latencies, 95):.3f}s")
    print(f"  p99: {_percentile(sorted_latencies, 99):.3f}s")


async def _send_chat_request(
    client: httpx.AsyncClient,
    token: str,
    message: str,
) -> float:
    start = time.perf_counter()
    response = await client.post(
        "/api/chat",
        json={"message": message, "history": [], "vault_id": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    elapsed = time.perf_counter() - start
    response.raise_for_status()
    return elapsed


async def _warmup(client: httpx.AsyncClient, token: str, count: int) -> None:
    for index in range(count):
        await _send_chat_request(client, token, f"warmup-{index}")


async def _run_concurrent(
    client: httpx.AsyncClient,
    token: str,
    concurrency: int,
    iterations: int,
) -> list[float]:
    latencies: list[float] = []
    for iteration in range(iterations):
        results = await asyncio.gather(
            *(
                _send_chat_request(client, token, f"concurrent-{concurrency}-{iteration}-{index}")
                for index in range(concurrency)
            ),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            print(f"  WARNING: {len(errors)}/{concurrency} request(s) failed: {errors[0]}")
        latencies.extend(result for result in results if isinstance(result, float))
    return latencies


def _setup_app() -> tuple[str, SimpleConnectionPool, Path, Callable[[], None]]:
    temp_dir = Path(tempfile.mkdtemp())
    original_jwt_secret = settings.jwt_secret_key
    original_users_enabled = settings.users_enabled
    original_data_dir = settings.data_dir

    settings.data_dir = temp_dir
    settings.jwt_secret_key = os.urandom(32).hex()
    settings.users_enabled = False

    db_path = temp_dir / "app.db"

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    from app.models.database import init_db, run_migrations

    init_db(str(db_path))
    run_migrations(str(db_path))
    connection_pool = SimpleConnectionPool(str(db_path))

    def override_get_db():
        conn = connection_pool.get_connection()
        try:
            yield conn
        finally:
            connection_pool.release_connection(conn)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[csrf_protect] = lambda: "benchmark-csrf"
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "username": "benchmark-user",
        "full_name": "Benchmark User",
        "role": "superadmin",
        "is_active": True,
        "must_change_password": False,
    }

    async def _allow_policy(principal, resource_type, resource_id, action):
        return True

    app.dependency_overrides[get_evaluate_policy] = lambda: _allow_policy

    mock_vector_store = MagicMock()
    mock_vector_store.db = MagicMock()
    mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
    mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
    app.dependency_overrides[get_vector_store] = lambda: mock_vector_store

    mock_rag_engine = MagicMock()

    async def _fake_query(*args, **kwargs):
        yield {"type": "done", "content": "ok", "sources": [], "memories_used": [], "wiki_used": [], "kms_used": []}

    mock_rag_engine.query = _fake_query
    app.dependency_overrides[get_rag_engine] = lambda: mock_rag_engine

    conn = connection_pool.get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1,'benchmark-user','benchmark-hash','Benchmark User','superadmin',1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (10,'Benchmark Vault','benchmark')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
            "VALUES (10,1,'admin',1)"
        )
        conn.commit()
    finally:
        connection_pool.release_connection(conn)

    token = create_access_token(1, "benchmark-user", "superadmin")

    def _noop_check(request, *args, **kwargs):
        request.state.view_rate_limit = []
        return None

    _original_check = limiter._check_request_limit
    limiter._check_request_limit = _noop_check

    def _teardown() -> None:
        limiter._check_request_limit = _original_check

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = original_jwt_secret
        settings.users_enabled = original_users_enabled
        settings.data_dir = original_data_dir
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_rag_engine, None)
        connection_pool.close_all()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return token, connection_pool, temp_dir, _teardown


async def _run_benchmark() -> None:
    token, connection_pool, temp_dir, teardown = _setup_app()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await _warmup(client, token, DEFAULT_WARMUP)
            for concurrency in DEFAULT_CONCURRENCY_LEVELS:
                latencies = await _run_concurrent(client, token, concurrency, DEFAULT_ITERATIONS)
                _report(str(concurrency), latencies)
    finally:
        teardown()


def main() -> None:
    asyncio.run(_run_benchmark())


if __name__ == "__main__":
    main()
