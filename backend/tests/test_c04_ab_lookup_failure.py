"""Issue #709 Phase 4.5 review finding: a failed A/B variant-content lookup
must not discard the assigned variant either.

The frozen exposure-write check (test_c04_ab_exposure_failure.py, AC5) pins
the telemetry half of the hardening. This sibling pins the OTHER half the
plan claims: when ``PromptVersionStore.get_version`` raises (same
writer-lock class of failure), the assigned-variant metadata
(``ab_experiment_id`` / ``ab_variant``) is retained while ``prompt_version``
labels the 3.5 effective version that actually served — for BOTH failure
modes of the lookup (raising ``get_version`` and a missing version row). An
implementation that isolated only the exposure write while leaving the
lookup inside the outer broad ``except`` would fail here.

Harness mirrors test_c04_ab_exposure_failure.py (fresh temp DB, org 10,
control+challenger versions, split_pct=100 experiment, vault, RAGEngine
over the temp DB with fake collaborators).
"""

import os
import sqlite3
import tempfile

import pytest

from app.models.database import run_migrations
from app.services.ab_testing import ABTestingService
from app.services.prompt_store import PromptVersionStore

_VAULT_ID = 9973  # unique to this module; avoids cross-suite collisions


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


def test_lookup_failure_keeps_assigned_variant_and_3_5_content(
    monkeypatch, fresh_db
):
    """get_version raising must retain the challenger metadata while the
    3.5 effective (global active) content is served."""
    store = PromptVersionStore(fresh_db)
    store.create_version("c04_ctrl", "control content", activate=True)
    store.create_version("c04_chlg", "challenger content")
    ab_service = ABTestingService(fresh_db)
    exp = ab_service.create_experiment(
        "c04_exp2", "c04_ctrl", "c04_chlg", split_pct=100
    )

    fresh_db.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_VAULT_ID, 10, "c04 Lookup Failure Vault", "2024-01-01T00:00:00Z"),
    )
    fresh_db.commit()

    def _raise_locked(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    # _resolve_prompt_with_ab_sync imports PromptVersionStore at call time,
    # so patching the class attribute reaches the in-function import.
    monkeypatch.setattr(PromptVersionStore, "get_version", _raise_locked)

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

    prompt_content, ab_experiment_id, ab_variant, prompt_version = (
        engine._resolve_prompt_with_ab_sync(vault_id=_VAULT_ID, user_id=1)
    )

    # The assignment survives the lookup failure.
    assert ab_variant == "challenger"
    assert ab_experiment_id == exp.id
    # prompt_version labels what SERVED: the variant content did not load,
    # so the 3.5 effective (global active = control) version is reported
    # while ab_variant keeps the assignment truth (review finding).
    assert prompt_version == "c04_ctrl"
    assert prompt_content == "control content"
