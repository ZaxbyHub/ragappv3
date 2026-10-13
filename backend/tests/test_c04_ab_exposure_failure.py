"""Issue #709 acceptance check (C04, AC5): a failed exposure write must
not drop the assigned A/B variant.

``RAGEngine._resolve_prompt_with_ab_sync`` currently calls
``ABTestingService.record_exposure`` (the analytics write) BEFORE reading
the variant's prompt content and before setting ``ab_experiment_id`` /
``ab_variant``. A raising exposure write (e.g. ``sqlite3.OperationalError:
database is locked``) hits the broad ``except`` and falls through with
``ab_variant=None`` — the engine then serves the NON-experiment prompt to
a subject that was deterministically assigned to the challenger, silently
corrupting the experiment (an exposed subject whose answer came from the
control arm).

Contract: the assigned variant must survive an exposure-write failure.
Analytics loss is acceptable; serving the wrong prompt arm is not.

Harness mirrors ``tests/test_prompt_ab_variants.py``
(``TestDoneMsgABKeys::test_done_msg_has_ab_keys_with_active_experiment``):
fresh temp DB via run_migrations, org 10 seed, two prompt versions, an
active experiment with split_pct=100 (subject ``user:1`` is guaranteed
challenger), a vault for the org, and a RAGEngine over the temp DB with
fake collaborators.
"""

import os
import sqlite3
import tempfile

import pytest

from app.models.database import run_migrations
from app.services.ab_testing import ABTestingService
from app.services.prompt_store import PromptVersionStore

_VAULT_ID = 9971  # unique to this module; avoids cross-suite collisions


@pytest.fixture
def fresh_db():
    """Temp DB with the full schema, org 10 seeded (FK target for A/B)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
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
        conn.close()
        os.unlink(path)


def test_exposure_write_failure_keeps_assigned_variant(monkeypatch, fresh_db):
    """record_exposure raising must not erase the assigned challenger variant."""
    store = PromptVersionStore(fresh_db)
    store.create_version("c04_ctrl", "control content", activate=True)
    store.create_version("c04_chlg", "challenger content")
    ab_service = ABTestingService(fresh_db)
    # split_pct=100 → every subject, including "user:1", is challenger.
    exp = ab_service.create_experiment(
        "c04_exp", "c04_ctrl", "c04_chlg", split_pct=100
    )

    fresh_db.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_VAULT_ID, 10, "c04 Exposure Failure Vault", "2024-01-01T00:00:00Z"),
    )
    fresh_db.commit()

    def _raise_locked(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    # _resolve_prompt_with_ab_sync imports ABTestingService at call time,
    # so patching the class attribute reaches the in-function import.
    monkeypatch.setattr(ABTestingService, "record_exposure", _raise_locked)

    from app.services.rag_engine import RAGEngine

    db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

    class _FakeEmbSvc:
        def embed_single(self, t):
            return []

        def embed_passage(self, t):
            return []

    class _FakeVec:
        pass

    class _FakeMem:
        pass

    class _FakeLLM:
        pass

    engine = RAGEngine(
        db_path=db_path,
        embedding_service=_FakeEmbSvc(),
        vector_store=_FakeVec(),
        memory_store=_FakeMem(),
        llm_client=_FakeLLM(),
    )

    result = engine._resolve_prompt_with_ab_sync(
        vault_id=_VAULT_ID, user_id=1
    )
    ab_var = result[2]

    assert ab_var == "challenger"
