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
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_ENCLOSING_DEF_RE = re.compile(r"(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")
# Guard predicates are matched against the statement's WHERE clause ONLY —
# never the SET clause (`SET title = ?` would self-satisfy a naive
# substring check over the whole statement, which is how this census was
# first written and caught by the implementation review as tautological).
_GUARD_PREDICATES = (
    "title IS NULL",
    "title = ?",
)


def _classify_title_update(source: str, match: "re.Match[str]"):
    """Classify one title-UPDATE match: returns (enclosing_fn, where_clause).

    Shared by the live census and the self-check so the self-check exercises
    the same parsing path the census runs (a self-check that re-implements
    the slicing inline cannot detect the census itself regressing — PR #832
    review F-002). The statement is bounded at the first ``;`` after the
    match so a WHERE-less UPDATE cannot bleed into a LATER statement's WHERE
    clause, and the guard predicate must appear in the WHERE clause's first
    OR-segment (``WHERE id = ? OR title IS NULL`` updates on the id alone
    and is NOT a guard).
    """
    fn_match = None
    for fn in _ENCLOSING_DEF_RE.finditer(source, 0, match.start()):
        fn_match = fn
    fn_name = fn_match.group(1) if fn_match else "<module>"
    statement = source[match.start() :]
    semi_at = statement.find(";")
    if semi_at >= 0:
        statement = statement[:semi_at]
    # A WHERE-less UPDATE must not inherit a LATER statement's WHERE: bound
    # at the first SELECT too (no title UPDATE in this repo contains a
    # subselect; if one ever does, the census fails loudly — a false alarm a
    # developer resolves — never a silent pass).
    select_at = _SELECT_RE.search(statement)
    if select_at:
        statement = statement[: select_at.start()]
    statement = statement[:400]
    where_at = statement.upper().find("WHERE")
    where_clause = statement[where_at:] if where_at >= 0 else ""
    first_or_segment = re.split(r"\sOR\s", where_clause, maxsplit=1, flags=re.IGNORECASE)[0]
    return fn_name, first_or_segment


def _iter_title_updates():
    """Yield (file, enclosing_function, where_clause) for every title UPDATE."""
    for path in sorted(BACKEND_APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in _TITLE_UPDATE_RE.finditer(source):
            fn_name, where_clause = _classify_title_update(source, match)
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

    def test_census_classifies_synthetic_shapes(self):
        """Self-check driving the REAL census classifier over synthetic
        sources (PR #832 review F-002): the base defect shape must classify
        unguarded even though its SET clause contains `title = ?`; the fixed
        shape must classify guarded; an OR-composed guard must NOT count
        (it updates on the id alone); and a WHERE-less UPDATE must not bleed
        into a following statement's WHERE."""
        base_defect = (
            "def _auto_name(session_id):\n"
            "    UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?\n"
        )
        _, where_clause = _classify_title_update(
            base_defect, next(_TITLE_UPDATE_RE.finditer(base_defect))
        )
        assert not any(p in where_clause for p in _GUARD_PREDICATES), (
            "the census must not accept `WHERE id = ?` as a title guard"
        )

        fixed = (
            "def _auto_name(session_id):\n"
            "    UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND (title IS NULL OR title = '')\n"
        )
        _, where_clause = _classify_title_update(
            fixed, next(_TITLE_UPDATE_RE.finditer(fixed))
        )
        assert any(p in where_clause for p in _GUARD_PREDICATES), (
            "the census must accept the shipped AND-composed guard"
        )

        or_guard = (
            "def _auto_name(session_id):\n"
            "    UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? OR title IS NULL\n"
        )
        _, where_clause = _classify_title_update(
            or_guard, next(_TITLE_UPDATE_RE.finditer(or_guard))
        )
        assert not any(p in where_clause for p in _GUARD_PREDICATES), (
            "the census must not accept an OR-composed predicate as a guard "
            "(it updates on the id alone)"
        )

        bleed = (
            "def _auto_name(session_id):\n"
            "    UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP\n"
            "    other = SELECT title FROM chat_sessions WHERE title IS NULL\n"
        )
        _, where_clause = _classify_title_update(
            bleed, next(_TITLE_UPDATE_RE.finditer(bleed))
        )
        assert not any(p in where_clause for p in _GUARD_PREDICATES), (
            "a WHERE-less UPDATE must not inherit a later statement's WHERE"
        )


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

    @pytest.mark.asyncio
    async def test_stream_done_supplies_no_unmirrored_contract_keys(self):
        """The parity guard's reverse direction (PR #832 review F-004): a NEW
        content-bearing done key that ChatResponse does not mirror is exactly
        the one-sided drift this class exists to catch — the forward check
        alone cannot see it."""
        from app.api.routes.chat import ChatResponse

        protocol_keys = {
            "type",
            "turn_id",
            "sources",
            "memories_used",
            "wiki_used",
            "kms_used",
            "score_type",
        }
        done = await _collect_done(_AllKeysEngine())
        extras = set(done) - set(ChatResponse.model_fields) - protocol_keys
        assert not extras, (
            "Done payload carries contract keys ChatResponse does not mirror "
            f"(add them to ChatResponse or the protocol set): {sorted(extras)}"
        )


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

    @pytest.mark.asyncio
    async def test_trace_not_synthesized_when_engine_does_not_attach(self):
        """The iff-contract's other direction (PR #832 review F-004): flag on
        but the engine attached no trace — the route must not emit a
        `"trace": null` key; weakening the gate to the flag alone must fail
        here."""
        done = await _collect_done(_AllKeysEngine(), flag_override=True)
        assert done is not None
        assert "trace" not in done, (
            "trace must stay absent when the engine did not attach one, even "
            "with rag_trace_in_response on"
        )


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


class TestFindMemoryByContentLookup:
    """Negative-path pins for the remember dedupe lookup (PR #832 review,
    external F-001/F-007): only a LIVE, non-expiring, same-vault, same-source
    row may confirm a re-sent directive."""

    @staticmethod
    def _make_store(tmp_path):
        from app.models.database import SQLiteConnectionPool, init_db, run_migrations
        from app.services.memory_store import MemoryStore

        db_path = tmp_path / "find-lookup.db"
        init_db(str(db_path))
        run_migrations(str(db_path))
        pool = SQLiteConnectionPool(str(db_path), max_size=5)
        return MemoryStore(pool=pool, embedding_service=None), pool

    def _content(self, store, content, expires_at=None, source="chat", vault_id=1):
        return store.add_memory(
            content, source=source, vault_id=vault_id, expires_at=expires_at
        )

    def test_expired_chat_row_does_not_dedupe(self, tmp_path):
        store, pool = self._make_store(tmp_path)
        try:
            self._content(store, "the pump code is 7", expires_at="2000-01-01T00:00:00")
            assert store.find_memory_by_content("the pump code is 7", "chat", 1) is None
            # The insert path therefore runs and stores a LIVE row.
            record = store.add_memory("the pump code is 7", source="chat", vault_id=1)
            assert record.id is not None
            found = store.find_memory_by_content("the pump code is 7", "chat", 1)
            assert found is not None and found.id == record.id
        finally:
            pool.close_all()

    def test_future_expiry_row_does_not_dedupe(self, tmp_path):
        store, pool = self._make_store(tmp_path)
        try:
            self._content(store, "the pump code is 7", expires_at="2099-01-01T00:00:00")
            assert store.find_memory_by_content("the pump code is 7", "chat", 1) is None, (
                "an expiring row is invisible to retrieval and swept by eviction; "
                "confirming it would claim a memory the user can never retrieve"
            )
        finally:
            pool.close_all()

    def test_other_vault_and_null_vault_do_not_cross_dedupe(self, tmp_path):
        store, pool = self._make_store(tmp_path)
        try:
            record = self._content(store, "the pump code is 7", vault_id=1)
            assert store.find_memory_by_content("the pump code is 7", "chat", 2) is None
            assert store.find_memory_by_content("the pump code is 7", "chat", None) is None
            global_record = self._content(store, "the pump code is 7", vault_id=None)
            found = store.find_memory_by_content("the pump code is 7", "chat", None)
            assert found is not None and found.id == global_record.id
            assert store.find_memory_by_content("the pump code is 7", "chat", 1).id == record.id
        finally:
            pool.close_all()

    def test_other_source_does_not_dedupe(self, tmp_path):
        store, pool = self._make_store(tmp_path)
        try:
            self._content(store, "the pump code is 7", source="upload")
            assert store.find_memory_by_content("the pump code is 7", "chat", 1) is None
        finally:
            pool.close_all()


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
        # Pin last_metrics to a real empty dict: a bare MagicMock attribute
        # would be truthy and `.get("finish_reason")` a Mock, so the
        # truncation branch would pass "by accident" instead of by contract
        # (PR #832 review, external F-007).
        llm.last_metrics = {}
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

    def test_whitespace_only_reply_returns_canvas_empty_model_reply(self):
        """The empty-reply predicate is strip-based: a whitespace-only reply
        must 422 exactly like an empty one (PR #832 review F-003 — mutating
        `.strip()` away must fail here)."""
        app, client = self._minimal_canvas_app()
        store = MagicMock()
        store.get_version.return_value = {"content": "alpha\nbeta\ngamma"}
        store.append_version.return_value = {"version_no": 2}
        llm = MagicMock()
        llm.last_metrics = {}
        llm.chat_completion = AsyncMock(return_value="   \n  ")
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

    def test_blank_selection_with_empty_reply_records_noop_version(self):
        """A whitespace-only selection with an empty reply is at most a
        whitespace collapse — it keeps the pre-#688 no-op-version behavior
        instead of 422ing (PR #832 review, external F-002)."""
        app, client = self._minimal_canvas_app()
        store = MagicMock()
        store.get_version.return_value = {"content": "alpha\n\n\ngamma"}
        store.append_version.return_value = {
            "id": 55,
            "version_no": 2,
            "name": None,
            "origin": "model_edit",
            "content": "alpha\n\ngamma",
            "content_sha256": "sha-blank",
            "model_edit_json": None,
            "created_by": 1,
            "created_at": "2026-01-01T00:00:00Z",
        }
        llm = MagicMock()
        llm.last_metrics = {}
        llm.chat_completion = AsyncMock(return_value="")
        app.state.llm_client = llm
        with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
            response = client.post(
                "/api/canvas/artifacts/uid/edit-range",
                json={
                    "start_line": 2,
                    "end_line": 3,
                    "instruction": "rewrite",
                    "base_version_no": 1,
                },
            )
        assert response.status_code == 200
        store.append_version.assert_called_once()

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
