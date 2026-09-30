"""Issue #688 defect-class guardrails (Class C8 producer/consumer contract drift).

Guards the recurrence class, not just the fixed instances:
- G1: every auto-title UPDATE on chat_sessions outside the manual-rename
  endpoint carries a title-guard predicate (PRR-004 lineage).
- G2: stream-vs-non-stream done-payload parity — the SSE done event and
  ChatResponse agree on their shared contract keys.
- G3: trace flag iff-forwarding, the exact canvas edit-range failure codes,
  and the remember-directive idempotency key across turns.
"""

import json
import os
import re
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Per-file optional-dep stubs (docs/engineering/testing.md section 2)."""
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = REPO_ROOT / "backend" / "app"

# The manual-rename endpoint writes the title unconditionally BY DESIGN — a
# user-initiated rename is the winning write, not an auto-title.
MANUAL_RENAME_FUNCTIONS = {"update_session"}

_TITLE_UPDATE_RE = re.compile(r"UPDATE\s+chat_sessions\s+SET\s+title", re.IGNORECASE)
_ENCLOSING_DEF_RE = re.compile(r"(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")
# Guard predicates are matched against the statement's WHERE clause ONLY —
# never the SET clause (`SET title = ?` would self-satisfy a naive
# substring check over the whole statement, which is how this census was
# first written and caught by the implementation review as tautological).
_GUARD_PREDICATES = (
    "title IS NULL",
    "title = ?",
)


def _iter_title_updates():
    """Yield (file, enclosing_function, where_clause) for every title UPDATE."""
    for path in sorted(BACKEND_APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in _TITLE_UPDATE_RE.finditer(source):
            fn_match = None
            for fn in _ENCLOSING_DEF_RE.finditer(source, 0, match.start()):
                fn_match = fn
            fn_name = fn_match.group(1) if fn_match else "<module>"
            statement = source[match.start() : match.start() + 400]
            where_at = statement.upper().find("WHERE")
            where_clause = statement[where_at:] if where_at >= 0 else ""
            yield path, fn_name, where_clause


class TestTitleGuardCensus:
    """G1: no unguarded auto-title write ships again."""

    def test_every_auto_title_update_carries_a_title_guard(self):
        guarded = 0
        unguarded = []
        for path, fn_name, where_clause in _iter_title_updates():
            if fn_name in MANUAL_RENAME_FUNCTIONS:
                continue
            if where_clause and any(
                pred in where_clause for pred in _GUARD_PREDICATES
            ):
                guarded += 1
            else:
                unguarded.append(f"{path.relative_to(REPO_ROOT)}::{fn_name}")
        assert not unguarded, (
            "Auto-title UPDATE(s) without a title-guard predicate in the WHERE "
            "clause (a manual rename committed between read and write would be "
            f"overwritten): {unguarded}"
        )
        assert guarded >= 5, (
            "Expected at least five guarded auto-title writes after issue #688; "
            f"found {guarded}. If a write was removed, update this census."
        )

    def test_census_detects_the_base_defect(self):
        """The census must fail on the pre-fix shape: an unguarded
        `UPDATE chat_sessions SET title = ? ... WHERE id = ?` (issue #688's
        original defect) must classify as unguarded even though its SET
        clause contains `title = ?`."""
        # The WHERE-sliced predicate check must reject a bare `WHERE id = ?`.
        statement = (
            "UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?"
        )
        where_at = statement.upper().find("WHERE")
        synthetic_where = statement[where_at:]
        assert not any(p in synthetic_where for p in _GUARD_PREDICATES), (
            "the census must not accept `WHERE id = ?` as a title guard"
        )
        # And the fixed shape must be accepted.
        fixed = (
            "UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND (title IS NULL OR title = '')"
        )
        where_at = fixed.upper().find("WHERE")
        assert "title IS NULL" in fixed[where_at:]


class _RecController:
    """Permissive admission recorder (518 harness pattern)."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield


class _AllKeysEngine:
    """Stub engine whose done chunk carries every shared contract key."""

    async def query(self, *args, **kwargs):
        yield {"type": "content", "content": "answer"}
        yield {
            "type": "done",
            "sources": [{"id": 1}],
            "memories_used": [],
            "wiki_used": [],
            "kms_used": [],
            "score_type": "rerank",
            "answer_contract": {"contract": 1},
            "llm_metrics": {"total_tokens": 3},
            "currency_warnings": ["stale"],
            "citation_enforcement": {"mode": "strict"},
            "citation_confidence": {"S1": 0.9},
            "unverifiable_claims": ["u1"],
            "prompt_version": "pv-1",
            "ab_experiment_id": 7,
            "ab_variant": "b",
        }


async def _collect_done(engine, flag_override=None):
    from app.api.routes import chat as chat_routes
    from app.config import settings

    original_flag = settings.rag_trace_in_response
    if flag_override is not None:
        settings.rag_trace_in_response = flag_override
    try:
        response = chat_routes.stream_chat_response("q", [], engine)
        done = None
        async for frame in response.body_iterator:
            text = frame if isinstance(frame, str) else frame.decode("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if payload.get("type") == "done":
                        done = payload
        return done
    finally:
        settings.rag_trace_in_response = original_flag


class TestDonePayloadParity:
    """G2: the two response shapes agree on their shared contract keys."""

    @pytest.mark.asyncio
    async def test_stream_done_keys_cover_chat_response_fields(self):
        from app.api.routes.chat import ChatResponse

        done = await _collect_done(_AllKeysEngine())
        assert done is not None
        response_fields = set(ChatResponse.model_fields) - {"content"}
        missing = response_fields - set(done)
        assert not missing, (
            "ChatResponse fields absent from the streaming done payload even "
            f"though the engine supplied them (one-sided parity drift): {sorted(missing)}"
        )

    @pytest.mark.asyncio
    async def test_stream_done_values_match_chat_response_pass_through(self):
        from app.api.routes.chat import ChatResponse

        done = await _collect_done(_AllKeysEngine())
        model = ChatResponse(
            content="answer",
            citation_confidence=done["citation_confidence"],
            unverifiable_claims=done["unverifiable_claims"],
        )
        assert model.citation_confidence == {"S1": 0.9}
        assert model.unverifiable_claims == ["u1"]


class TestTraceFlagContract:
    """G3(a): `trace` reaches the client iff the flag is on."""

    @pytest.mark.asyncio
    async def test_trace_absent_when_flag_off_even_if_engine_attaches_it(self):
        class _TracingEngine(_AllKeysEngine):
            async def query(self, *args, **kwargs):
                async for chunk in super().query(*args, **kwargs):
                    if chunk.get("type") == "done":
                        chunk = {**chunk, "trace": {"stages": ["retrieve"]}}
                    yield chunk

        done = await _collect_done(_TracingEngine(), flag_override=False)
        assert done is not None
        assert "trace" not in done, (
            "trace must stay absent unless rag_trace_in_response is on, even "
            "for an engine that always attaches one"
        )

    @pytest.mark.asyncio
    async def test_trace_present_when_flag_on(self):
        class _TracingEngine(_AllKeysEngine):
            async def query(self, *args, **kwargs):
                async for chunk in super().query(*args, **kwargs):
                    if chunk.get("type") == "done":
                        chunk = {**chunk, "trace": {"stages": ["retrieve"]}}
                    yield chunk

        done = await _collect_done(_TracingEngine(), flag_override=True)
        assert done is not None
        assert done.get("trace") == {"stages": ["retrieve"]}


class TestRememberDirectiveIdempotencyKey:
    """G3(c): the remember key is content+source+vault, across turns."""

    @pytest.mark.asyncio
    async def test_repeat_directive_in_later_turn_stores_one_memory(self, tmp_path):
        import sqlite3

        from app.models.database import SQLiteConnectionPool, init_db, run_migrations
        from app.services import rag_engine as rag_engine_module
        from app.services.memory_store import MemoryStore
        from app.services.telemetry import set_current_turn

        db_path = tmp_path / "remember-guard.db"
        init_db(str(db_path))
        run_migrations(str(db_path))
        store_pool = SQLiteConnectionPool(str(db_path), max_size=5)
        store = MemoryStore(pool=store_pool, embedding_service=None)
        engine = rag_engine_module.RAGEngine(
            embedding_service=MagicMock(),
            vector_store=MagicMock(),
            memory_store=store,
            llm_client=MagicMock(),
            db_path=str(db_path),
        )
        try:
            with patch.object(
                rag_engine_module,
                "get_admission_controller",
                lambda: _RecController(),
            ):
                for i, turn_id in enumerate(("turn-a", "turn-b")):
                    set_current_turn(turn_id)
                    chunks = []
                    async for chunk in engine.query(
                        "remember that the pump code is 7",
                        [],
                        stream=True,
                        vault_id=1,
                        can_write_memory=True,
                    ):
                        chunks.append(chunk)
                    assert any(
                        "Memory stored" in c.get("content", "")
                        for c in chunks
                        if c.get("type") == "content"
                    ), f"turn {i}: the re-sent directive must still be confirmed"
        finally:
            store_pool.close_all()

        check = sqlite3.connect(str(db_path))
        try:
            count = check.execute(
                "SELECT COUNT(*) FROM memories WHERE content LIKE '%pump code is 7%'"
            ).fetchone()[0]
        finally:
            check.close()
        assert count == 1


class TestCanvasEditRangeFailureCodes:
    """G3(b): the exact detail codes for degenerate model replies."""

    def _minimal_canvas_app(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.deps import get_db, get_vector_store
        from app.api.routes import canvas as canvas_module
        from app.api.routes.canvas import get_current_active_user

        ready_vs = MagicMock()
        ready_vs._ready = True
        fake_conn = MagicMock()
        fake_conn.in_transaction = False

        app = FastAPI()
        app.include_router(canvas_module.router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "username": "u", "email": "u@e", "role": "admin",
        }
        app.dependency_overrides[get_vector_store] = lambda: ready_vs
        app.dependency_overrides[get_db] = lambda: fake_conn
        return app, TestClient(app)

    def test_empty_reply_returns_canvas_empty_model_reply(self):
        app, client = self._minimal_canvas_app()
        store = MagicMock()
        store.get_version.return_value = {"content": "alpha\nbeta\ngamma"}
        store.append_version.return_value = {"version_no": 2}
        llm = MagicMock()
        llm.chat_completion = AsyncMock(return_value="")
        app.state.llm_client = llm
        with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
            response = client.post(
                "/api/canvas/artifacts/uid/edit-range",
                json={
                    "start_line": 2,
                    "end_line": 2,
                    "instruction": "rewrite",
                    "base_version_no": 1,
                },
            )
        assert response.status_code == 422
        assert response.json()["detail"] == "canvas_empty_model_reply"
        store.append_version.assert_not_called()

    def test_length_truncated_reply_returns_canvas_model_truncated(self):
        import httpx

        from app.services.llm_client import LLMClient

        app, client = self._minimal_canvas_app()
        store = MagicMock()
        store.get_version.return_value = {"content": "alpha\nbeta\ngamma"}
        store.append_version.return_value = {"version_no": 2}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "partial"},
                            "finish_reason": "length",
                        }
                    ]
                },
                request=request,
            )

        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(base_url="http://llm-guard-test:11434", model="m")
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.llm_client = llm
        try:
            with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
                response = client.post(
                    "/api/canvas/artifacts/uid/edit-range",
                    json={
                        "start_line": 2,
                        "end_line": 2,
                        "instruction": "rewrite",
                        "base_version_no": 1,
                    },
                )
        finally:
            llm._client = None
        assert response.status_code == 502
        assert response.json()["detail"] == "canvas_model_truncated"
        store.append_version.assert_not_called()
