"""Standalone server launcher for the issue #555 kill/restart drill.

Spawned as a subprocess by test_chat_stream_process_kill.py (opt-in via
RAGAPP_KILL_DRILL=1). Builds the real app with a scripted engine and
stream-auth override so no model or credentials are needed, seeds session 1
against the DATA_DIR the drill points both processes at, and serves on the
DRILL_PORT. The kill/restart semantics live in the test; this file only
boots a deterministic server.
"""
import asyncio
import os
import sys

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("ADMIN_SECRET_TOKEN", "drill-admin-secret-token-0123456789")
os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault("JWT_SECRET_KEY", "drill-jwt-secret-key-for-testing-only-0123")


class _ScriptedEngine:
    """Slow multi-chunk engine so the drill can hard-kill mid-answer."""

    llm_client = None

    def query(self, *args, **kwargs):
        return self._stream()

    async def _stream(self):
        for index in range(4):
            await asyncio.sleep(0.4)
            yield {"type": "content", "content": f"chunk {index} "}
        yield {"type": "done", "sources": [], "memories_used": []}


from app.api.deps import get_rag_engine, get_stream_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402  (imports after env vars are set)
from app.models.database import get_pool, run_migrations  # noqa: E402

app.dependency_overrides[get_rag_engine] = lambda: _ScriptedEngine()
app.dependency_overrides[get_stream_auth] = lambda: {
    "id": 1, "username": "drill", "role": "admin",
}

run_migrations(str(settings.sqlite_path))
_pool = get_pool(str(settings.sqlite_path))
with _pool.connection() as _conn:
    _conn.execute(
        "INSERT OR IGNORE INTO chat_sessions (id, vault_id, user_id) VALUES (1, 1, 1)"
    )
    _conn.commit()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["DRILL_PORT"]), log_level="warning")
