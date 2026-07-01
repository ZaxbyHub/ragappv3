import hmac
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.api.routes.health import _vector_reconciliation_status
from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.answer_contract import build_answer_contract
from app.services.llm_client import LLMError
from app.services.memory_store import MemoryStore
from app.services.rag_engine import RAGEngine
from app.services.security_audit import record_security_event


def test_security_audit_event_hmac_covers_canonical_event(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "audit-test-secret")
    monkeypatch.setattr(settings, "audit_hmac_key_version", "v-test")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE security_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor_user_id INTEGER,
            actor_username TEXT,
            target_user_id INTEGER,
            target_username TEXT,
            ip_address TEXT,
            user_agent TEXT,
            metadata_json TEXT,
            key_version TEXT NOT NULL,
            hmac_sha256 TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    record_security_event(
        conn,
        event_type="auth.login_success",
        actor_user_id=1,
        actor_username="admin",
        target_user_id=1,
        target_username="admin",
        ip_address="127.0.0.1",
        user_agent="pytest",
        metadata={"role": "superadmin"},
    )

    row = conn.execute(
        "SELECT event_type, actor_user_id, actor_username, target_user_id, target_username, ip_address, user_agent, metadata_json, key_version, hmac_sha256 FROM security_audit_log"
    ).fetchone()
    expected_message = "|".join(
        [
            "auth.login_success",
            "1",
            "admin",
            "1",
            "admin",
            "127.0.0.1",
            "pytest",
            json.dumps({"role": "superadmin"}, sort_keys=True, separators=(",", ":")),
            "v-test",
        ]
    )
    expected = hmac.new(
        b"audit-test-secret", expected_message.encode("utf-8"), "sha256"
    ).hexdigest()
    assert row[9] == expected


def test_memory_store_filters_and_evicts_expired_memories():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memories.db"
        init_db(str(db_path))
        run_migrations(str(db_path))
        pool = SQLiteConnectionPool(str(db_path), max_size=2)
        store = MemoryStore(pool=pool)
        try:
            store.add_memory(
                "expired fact about retention",
                importance=0.9,
                expires_at="2000-01-01T00:00:00",
            )
            store.add_memory("fresh fact about retention", importance=0.8)

            # search_memories no longer evicts expired rows inline (issue
            # #263 moved eviction to a periodic background task), but it
            # still filters them out of results at read time.
            results = store.search_memories("retention", limit=10)

            assert [record.content for record in results] == ["fresh fact about retention"]
            # Explicit eviction now removes the expired row that search
            # left behind.
            assert store.evict_expired_memories() == 1
            assert store.evict_expired_memories() == 0
        finally:
            pool.close_all()


def test_vector_reconciliation_status_reports_pending_queue():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE vector_delete_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            vault_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            attempts INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO vector_delete_pending(file_id, vault_id, created_at, attempts) VALUES (1, 2, '2026-06-01T00:00:00', 3)"
    )

    status = _vector_reconciliation_status(conn)

    assert status == {
        "ok": False,
        "pending_count": 1,
        "oldest_pending_created_at": "2026-06-01T00:00:00",
        "max_attempts": 3,
    }


def test_answer_contract_preserves_answer_and_typed_citations():
    contract = build_answer_contract(
        "Answer using [S1] and [M1].",
        sources=[{"source_label": "S1"}],
        memories_used=[{"memory_label": "M1"}],
        wiki_used=[],
        kms_used=[],
    )

    assert contract["answer"] == "Answer using [S1] and [M1]."
    assert contract["citations"] == [
        {"label": "S1", "evidence_type": "document"},
        {"label": "M1", "evidence_type": "memory"},
    ]
    assert contract["abstained"] is False


class DummyLLMClient:
    def __init__(self, name, *, fail=False, content=""):
        self.base_url = name
        self.model = name
        self.fail = fail
        self.content = content
        self.last_metrics = {}

    async def chat_completion(self, messages, temperature=0.7, max_tokens=32768, response_format=None):
        if self.fail:
            self.last_metrics = {"provider_url": self.base_url, "status": "request_error"}
            raise LLMError(f"{self.base_url} failed")
        self.last_metrics = {"provider_url": self.base_url, "status": "ok"}
        return self.content


@pytest.mark.asyncio
async def test_rag_engine_non_stream_llm_falls_back_to_next_client():
    primary = DummyLLMClient("primary", fail=True)
    fallback = DummyLLMClient("instant", content="fallback answer")
    engine = RAGEngine(
        embedding_service=object(),
        vector_store=object(),
        memory_store=object(),
        llm_client=primary,
        instant_client=fallback,
        thinking_client=primary,
    )

    chunks = [
        chunk
        async for chunk in engine._get_llm_response(
            [{"role": "user", "content": "hello"}],
            client=primary,
        )
    ]

    assert chunks == [{"type": "content", "content": "fallback answer"}]
    assert engine._last_llm_metrics["provider_url"] == "instant"
    assert engine._last_llm_metrics["fallback_from"] == "primary"
