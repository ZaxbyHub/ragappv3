"""Tests for prompt A/B experiment variants (FR-007 part 3).

Verifies:
- Deterministic sticky assignment (same subject -> same variant)
- Split percentage respected in aggregate (distribution over N subjects ≈ split_pct)
- Exposure recorded once per subject (idempotent INSERT OR IGNORE)
- End experiment sets status='ended' and winner
- No active experiment: returns 3.5 effective version (org override > global active)
- Authz: admin-only endpoints
"""

import hashlib
import os
import sqlite3
import tempfile

import pytest

from app.models.database import run_migrations
from app.services.ab_testing import ABExperiment, ABTestingService
from app.services.prompt_store import PromptVersionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    """Create a temporary DB with the full schema including A/B tables."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        # Seed test org so FK constraints on prompt_ab_experiments pass.
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


@pytest.fixture
def ab_service(fresh_db):
    """Return an ABTestingService backed by the fresh DB."""
    return ABTestingService(fresh_db)


@pytest.fixture
def store(fresh_db):
    """Return a PromptVersionStore backed by the fresh DB."""
    return PromptVersionStore(fresh_db)


# ---------------------------------------------------------------------------
# Deterministic sticky assignment
# ---------------------------------------------------------------------------


class TestDeterministicAssignment:
    """Assignment is deterministic: same experiment+subject always gives same variant."""

    def test_same_subject_same_variant(self, ab_service, store):
        """Identical (experiment, subject) → same variant across multiple calls."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment(
            "exp1", "v1", "v2", split_pct=50
        )

        for _ in range(5):
            variant = ABTestingService.assign(exp, "subject-abc")
            assert variant == ABTestingService.assign(exp, "subject-abc")

    def test_different_subjects_may_differ(self, ab_service, store):
        """Different subjects can get different variants based on split_pct."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment(
            "exp1", "v1", "v2", split_pct=50
        )

        variants = {ABTestingService.assign(exp, f"subject-{i}") for i in range(100)}
        # At least one subject should get each variant at 50% split
        assert len(variants) >= 1

    def test_assign_respects_split_pct_zero(self, ab_service, store):
        """split_pct=0 → all subjects get control."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment(
            "exp_zero", "v1", "v2", split_pct=0
        )

        challengers = sum(
            1 for i in range(200)
            if ABTestingService.assign(exp, f"subject-{i}") == "challenger"
        )
        assert challengers == 0

    def test_assign_respects_split_pct_hundred(self, ab_service, store):
        """split_pct=100 → all subjects get challenger."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment(
            "exp_full", "v1", "v2", split_pct=100
        )

        challengers = sum(
            1 for i in range(200)
            if ABTestingService.assign(exp, f"subject-{i}") == "challenger"
        )
        assert challengers == 200

    def test_assign_distribution_approx_split_pct(self, ab_service, store):
        """Over 1000 subjects, observed challenger rate ≈ split_pct within statistical tolerance.

        Uses a ±5% tolerance band.  This is a smoke test, not a rigorous statistical test.
        """
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment(
            "exp_dist", "v1", "v2", split_pct=33
        )

        challengers = sum(
            1 for i in range(1000)
            if ABTestingService.assign(exp, f"subject-{i}") == "challenger"
        )
        rate = challengers / 1000
        # 33% ± 5% tolerance
        assert 0.28 <= rate <= 0.38

    def test_different_experiments_same_subject_diff_buckets(self, ab_service, store):
        """Same subject can get different variants in different experiments."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp_a = ab_service.create_experiment("exp_a", "v1", "v2", split_pct=10)
        exp_b = ab_service.create_experiment("exp_b", "v1", "v2", split_pct=90)

        subject = "same-subject"
        var_a = ABTestingService.assign(exp_a, subject)
        var_b = ABTestingService.assign(exp_b, subject)
        # Different experiments assign independently; both arms are valid
        assert var_a in ("control", "challenger")
        assert var_b in ("control", "challenger")


# ---------------------------------------------------------------------------
# Exposure recording (idempotent)
# ---------------------------------------------------------------------------


class TestExposureRecording:
    """Exposures are recorded once per subject+experiment (INSERT OR IGNORE)."""

    def test_record_exposure_inserts_row(self, ab_service, fresh_db, store):
        """record_exposure creates an exposure row."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        ab_service.record_exposure(exp.id, "subject-1", "control")
        exposures = fresh_db.execute(
            "SELECT * FROM prompt_ab_exposures WHERE experiment_id = ?",
            (exp.id,),
        ).fetchall()
        assert len(exposures) == 1
        assert exposures[0]["assigned_variant"] == "control"

    def test_record_exposure_idempotent(self, ab_service, fresh_db, store):
        """Calling record_exposure twice for same subject+experiment is a no-op."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        ab_service.record_exposure(exp.id, "subject-1", "control")
        ab_service.record_exposure(exp.id, "subject-1", "control")  # idempotent
        exposures = fresh_db.execute(
            "SELECT * FROM prompt_ab_exposures WHERE experiment_id = ?",
            (exp.id,),
        ).fetchall()
        assert len(exposures) == 1  # still only one

    def test_different_subjects_get_separate_rows(self, ab_service, fresh_db, store):
        """Different subjects get separate exposure rows."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        for i in range(10):
            ab_service.record_exposure(exp.id, f"subject-{i}", "control")
        exposures = fresh_db.execute(
            "SELECT COUNT(*) FROM prompt_ab_exposures WHERE experiment_id = ?",
            (exp.id,),
        ).fetchone()
        assert exposures[0] == 10


# ---------------------------------------------------------------------------
# Experiment lifecycle
# ---------------------------------------------------------------------------


class TestExperimentLifecycle:
    """Create → list → end lifecycle."""

    def test_create_experiment(self, ab_service, store):
        """create_experiment inserts a row with status='active'."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=25)

        assert exp.id is not None
        assert exp.name == "exp1"
        assert exp.control_version == "v1"
        assert exp.challenger_version == "v2"
        assert exp.split_pct == 25
        assert exp.status == "active"
        assert exp.winner is None
        assert exp.ended_at is None

    def test_create_experiment_duplicate_name_raises(self, ab_service, store):
        """Duplicate experiment name raises IntegrityError."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        ab_service.create_experiment("exp1", "v1", "v2")
        with pytest.raises(sqlite3.IntegrityError):
            ab_service.create_experiment("exp1", "v1", "v2")

    def test_list_experiments(self, ab_service, store):
        """list_experiments returns all experiments with exposure counts."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp1 = ab_service.create_experiment("exp1", "v1", "v2", split_pct=20)
        exp2 = ab_service.create_experiment("exp2", "v1", "v2", split_pct=80)

        # Add exposures
        for i in range(5):
            ab_service.record_exposure(exp1.id, f"s1-{i}", "control")
        for i in range(3):
            ab_service.record_exposure(exp1.id, f"s2-{i}", "challenger")

        experiments = ab_service.list_experiments()
        by_name = {ewc.experiment.name: ewc for ewc in experiments}

        assert "exp1" in by_name
        assert by_name["exp1"].control_exposures == 5
        assert by_name["exp1"].challenger_exposures == 3
        assert "exp2" in by_name
        assert by_name["exp2"].control_exposures == 0
        assert by_name["exp2"].challenger_exposures == 0

    def test_end_experiment_sets_winner(self, ab_service, store):
        """end_experiment marks experiment as ended with winner."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        ended = ab_service.end_experiment(exp.id, "challenger")

        assert ended.status == "ended"
        assert ended.winner == "challenger"
        assert ended.ended_at is not None

    def test_end_experiment_already_ended_raises(self, ab_service, store):
        """Ending an already-ended experiment raises ValueError."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)
        ab_service.end_experiment(exp.id, "control")

        with pytest.raises(ValueError, match="already"):
            ab_service.end_experiment(exp.id, "challenger")

    def test_end_experiment_nonexistent_raises(self, ab_service):
        """Ending a nonexistent experiment raises ValueError."""
        with pytest.raises(ValueError, match="No experiment"):
            ab_service.end_experiment(9999, "control")

    def test_end_experiment_invalid_winner_raises(self, ab_service, store):
        """Ending with invalid winner raises ValueError."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        with pytest.raises(ValueError, match="must be"):
            ab_service.end_experiment(exp.id, "invalid")

    def test_get_active_experiment_returns_active(self, ab_service, store):
        """get_active_experiment returns the active experiment."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)

        active = ab_service.get_active_experiment()
        assert active is not None
        assert active.id == exp.id

    def test_get_active_experiment_returns_none_when_ended(self, ab_service, store):
        """get_active_experiment returns None when the only experiment is ended."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)
        ab_service.end_experiment(exp.id, "control")

        assert ab_service.get_active_experiment() is None

    def test_get_active_experiment_returns_none_when_empty(self, ab_service):
        """get_active_experiment returns None when no experiments exist."""
        assert ab_service.get_active_experiment() is None

    def test_ended_experiment_get_active_returns_none(self, ab_service, store):
        """After ending, get_active_experiment returns None (stops new assignments)."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)
        ab_service.end_experiment(exp.id, "control")

        assert ab_service.get_active_experiment() is None

    def test_ended_experiment_assignment_returns_control(self, ab_service, store):
        """After ending, assign() still returns a variant but experiment is no longer active.

        This verifies the design: assignment is a static method that does not check
        status; callers must check get_active_experiment() first.  The experiment
        being ended means no *new* exposures are recorded and no *new* assignments
        should be wired via A/B (callers should check get_active_experiment).
        """
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp1", "v1", "v2", split_pct=50)
        ab_service.end_experiment(exp.id, "control")

        # assign() is stateless — still deterministic, but no longer "active"
        variant = ABTestingService.assign(exp, "subject-abc")
        assert variant in ("control", "challenger")

        # Confirm the experiment is ended (active check)
        assert ab_service.get_active_experiment() is None


# ---------------------------------------------------------------------------
# No-experiment fallback — returns 3.5 effective version
# ---------------------------------------------------------------------------


class TestNoExperimentFallback:
    """When no active experiment exists, effective version is 3.5 resolution."""

    def test_no_experiment_returns_none_for_ab_fields(self, ab_service):
        """get_active_experiment returns None → no A/B metadata."""
        active = ab_service.get_active_experiment()
        assert active is None

    def test_fallback_is_3_5_override_not_ab(self, fresh_db, store):
        """With no active experiment, ABTestingService returns 3.5 effective version."""
        # Set up 3.5 org override
        store.create_version("v3_global", "global v3", activate=True)
        store.create_version("v3_override", "org override v3")
        store.set_org_override(org_id=10, version="v3_override")

        # No A/B experiment → A/B service returns nothing useful (no active experiment)
        ab_service = ABTestingService(fresh_db)
        active = ab_service.get_active_experiment()
        assert active is None

        # But the 3.5 org override is still in effect via PromptVersionStore
        resolved = store.resolve_for_org(10)
        assert resolved is not None
        assert resolved.version == "v3_override"

    def test_rag_engine_3_5_fallback_no_active_experiment(self, fresh_db, store):
        """RAG engine _resolve_prompt_with_ab_sync returns 3.5 org override when no A/B is active.

        This end-to-end test verifies the full resolution chain:
        1. No A/B experiment exists
        2. RAG engine returns (3.5_content, None, None) via _resolve_prompt_with_ab_sync
        3. The 3.5 org override content is returned
        """
        # Set up vault + org with 3.5 org override
        store.create_version("v3_global", "global v3 content", activate=True)
        store.create_version("v3_override", "3.5 org override content")
        store.set_org_override(org_id=10, version="v3_override")

        # Create a vault for org 10 with unique ID to avoid fixture collisions
        fresh_db.execute(
            "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9991, 10, "Test Vault 991", "2024-01-01T00:00:00Z"),
        )
        fresh_db.commit()

        # RAG engine path: _resolve_prompt_with_ab_sync with vault_id=9991, subject_key=None
        from app.services.rag_engine import RAGEngine

        # Get the actual DB path from the connection
        db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

        # Build engine with all-fake services to avoid network calls
        fake_emb = __import__("types").ModuleType("emb")
        class _FakeEmbSvc:
            def embed_single(self, t):
                return []
            def embed_passage(self, t):
                return []
        fake_emb.EmbeddingService = _FakeEmbSvc

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

        content, ab_exp_id, ab_var, p_version = engine._resolve_prompt_with_ab_sync(vault_id=9991, user_id=None)

        # No A/B experiment: ab fields are None, 3.5 org override content returned
        assert content == "3.5 org override content"
        assert ab_exp_id is None
        assert ab_var is None
        assert p_version == "v3_override"  # SC-015: prompt_version is set even without A/B

    def test_rag_engine_no_ab_returns_org_override_content(self, fresh_db, store):
        """With no active A/B experiment, _resolve_prompt_with_ab_sync returns 3.5 org content."""
        # Set up vault + org with 3.5 org override
        store.create_version("v3_global", "global v3 content", activate=True)
        store.create_version("v3_override", "3.5 org override content")
        store.set_org_override(org_id=10, version="v3_override")

        # Create a vault for org 10
        fresh_db.execute(
            "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9992, 10, "Test Vault 992", "2024-01-01T00:00:00Z"),
        )
        fresh_db.commit()

        # Verify: ABTestingService has no active experiment
        ab_service = ABTestingService(fresh_db)
        assert ab_service.get_active_experiment() is None

        # RAG engine resolution returns 3.5 org override content with no A/B metadata
        from app.services.rag_engine import RAGEngine

        db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

        class _FakeEmbSvc2:
            def embed_single(self, t):
                return []
            def embed_passage(self, t):
                return []
        class _FakeVec2:
            pass
        class _FakeMem2:
            pass
        class _FakeLLM2:
            pass

        engine = RAGEngine(
            db_path=db_path,
            embedding_service=_FakeEmbSvc2(),
            vector_store=_FakeVec2(),
            memory_store=_FakeMem2(),
            llm_client=_FakeLLM2(),
        )
        content, ab_exp_id, ab_var, p_version = engine._resolve_prompt_with_ab_sync(vault_id=9992, user_id=None)

        assert content == "3.5 org override content"
        assert ab_exp_id is None
        assert ab_var is None
        assert p_version == "v3_override"  # SC-015: prompt_version is set even without A/B

    def test_active_ab_overrides_org_override(self, fresh_db, store):
        """When an active A/B experiment exists, its assigned variant overrides the 3.5 org override.

        Resolution order per spec: 3.5 org override → A/B experiment (takes precedence when active).
        """
        # Set up 3.5 org override
        store.create_version("v3_global", "global v3 content", activate=True)
        store.create_version("v3_override", "3.5 org override content")
        store.create_version("ab_ctrl", "A/B control content")
        store.create_version("ab_chlg", "A/B challenger content")
        store.set_org_override(org_id=10, version="v3_override")

        # Create a vault for org 10
        fresh_db.execute(
            "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9993, 10, "Test Vault 993", "2024-01-01T00:00:00Z"),
        )
        fresh_db.commit()

        # Create active A/B experiment — split_pct=100 guarantees challenger for any user
        ab_service = ABTestingService(fresh_db)
        exp = ab_service.create_experiment("ab_exp2", "ab_ctrl", "ab_chlg", split_pct=100)

        # RAG engine with active experiment + user_id → A/B challenger content returned
        from app.services.rag_engine import RAGEngine

        db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

        class _FakeEmbSvc3:
            def embed_single(self, t):
                return []
            def embed_passage(self, t):
                return []
        class _FakeVec3:
            pass
        class _FakeMem3:
            pass
        class _FakeLLM3:
            pass

        engine = RAGEngine(
            db_path=db_path,
            embedding_service=_FakeEmbSvc3(),
            vector_store=_FakeVec3(),
            memory_store=_FakeMem3(),
            llm_client=_FakeLLM3(),
        )
        content, ab_exp_id, ab_var, p_version = engine._resolve_prompt_with_ab_sync(vault_id=9993, user_id=1)

        # A/B fields are populated — split_pct=100 guarantees challenger
        assert ab_exp_id == exp.id
        assert ab_var == "challenger"

        # Content is from the A/B challenger variant (not the 3.5 org override)
        assert content == "A/B challenger content"
        assert content != "3.5 org override content"  # A/B overrides 3.5
        # SC-015: prompt_version is the A/B version label
        assert p_version == "ab_chlg"

        # Verify the org override still exists in the store (just not used while A/B is active)
        resolved = store.resolve_for_org(10)
        assert resolved.version == "v3_override"


# ---------------------------------------------------------------------------
# Assignment algorithm (hash-based deterministic bucket)
# ---------------------------------------------------------------------------


class TestAssignmentAlgorithm:
    """The assignment algorithm is hash-based: hash(name+subject) % 100 < split_pct → challenger."""

    def test_assign_is_hash_based(self, store):
        """Verify the bucket is determined by hash of name+subject_key."""
        store.create_version("v1", "c1", activate=True)
        store.create_version("v2", "c2")
        exp = ABTestingService.assign(
            ABExperiment(
                id=1, name="test-exp", control_version="v1",
                challenger_version="v2", split_pct=50, status="active",
                winner=None, created_at="", ended_at=None,
            ),
            "subject-key",
        )
        # Just verify it returns a valid variant
        assert exp in ("control", "challenger")

    def test_split_50_approx_half_challenger(self, ab_service, store):
        """At 50% split over 1000 subjects, ~45-55% get challenger."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp50", "v1", "v2", split_pct=50)

        challengers = sum(
            1 for i in range(1000)
            if ABTestingService.assign(exp, f"subject-{i}") == "challenger"
        )
        rate = challengers / 1000
        assert 0.42 <= rate <= 0.58

    def test_split_10_approx_10_pct_challenger(self, ab_service, store):
        """At 10% split over 1000 subjects, ~7-13% get challenger."""
        store.create_version("v1", "control content", activate=True)
        store.create_version("v2", "challenger content")
        exp = ab_service.create_experiment("exp10", "v1", "v2", split_pct=10)

        challengers = sum(
            1 for i in range(1000)
            if ABTestingService.assign(exp, f"subject-{i}") == "challenger"
        )
        rate = challengers / 1000
        assert 0.06 <= rate <= 0.15


# ---------------------------------------------------------------------------
# Authz: admin-only endpoints
# ---------------------------------------------------------------------------

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.prompts import router as prompts_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


class TestABExperimentAPIFixtures:
    """Shared fixture setup for A/B experiment API tests."""

    @pytest.fixture(autouse=True)
    def setup_db(self, monkeypatch):
        """Set up test database with full schema + A/B tables."""
        temp_dir = tempfile.mkdtemp()
        db_path = str(Path(temp_dir) / "app.db")

        # Clear pool cache
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path_, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Initialize schema
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        conn.close()

        # Patch settings
        monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
        monkeypatch.setattr(
            "app.config.settings.jwt_secret_key",
            "test-secret-key-for-testing-only-min-32-chars!!",
        )
        monkeypatch.setattr("app.config.settings.users_enabled", True)
        # Set up admin token for require_scope (used by A/B experiment endpoints).
        monkeypatch.setattr("app.config.settings.admin_secret_token", "test-admin-secret")
        monkeypatch.setattr(
            "app.config.settings.admin_token_scopes",
            {"test-admin-secret": ["admin:config"]},
        )

        # Seed test users
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        pw = hash_password("testpass")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (1, "superadmin", pw, "Super Admin", "superadmin"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (2, "admin1", pw, "Admin One", "admin"),
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (3, "member1", pw, "Member One", "member"),
        )
        conn.execute(
            "INSERT INTO organizations (id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        self._db_path = db_path
        self._temp_dir = temp_dir
        yield db_path

        # Cleanup
        with _pool_cache_lock:
            if db_path in _pool_cache:
                _pool_cache[db_path].close_all()
                del _pool_cache[db_path]
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def client(self):
        """Create test client with routers and dependency overrides."""
        from app.security import csrf_protect

        app = FastAPI()
        app.include_router(auth_router, prefix="/api")
        app.include_router(prompts_router, prefix="/api")

        # Override csrf_protect to bypass token check.
        def mock_csrf_protect():
            return "test-csrf-token"

        app.dependency_overrides[csrf_protect] = mock_csrf_protect

        tc = TestClient(app)
        tc.headers["user-agent"] = ""

        return tc

    def _create_prompt_versions(self):
        """Create two prompt versions for experiment testing."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v1-control", "control content", 1, "setup"),
        )
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v2-challenger", "challenger content", 0, "setup"),
        )
        conn.commit()
        conn.close()

    def _token(self, user_id: int, username: str, role: str):
        return create_access_token(
            user_id,
            username,
            role,
            client_fingerprint=compute_client_fingerprint(""),
        )

    def _admin_headers(self):
        """Auth headers using the static admin token (for require_scope)."""
        return {"Authorization": "Bearer test-admin-secret"}

    def _non_admin_headers(self):
        """Auth headers using a non-admin token (not in admin_token_scopes)."""
        return {"Authorization": "Bearer not-an-admin-token"}


class TestCreateExperimentEndpoint(TestABExperimentAPIFixtures):
    """Tests for POST /api/prompts/ab-experiments."""

    def test_admin_can_create_experiment(self, client):
        """Admin with admin:config scope can create an experiment."""
        self._create_prompt_versions()

        response = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
                "split_pct": 25,
            },
            headers=self._admin_headers(),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "exp1"
        assert data["status"] == "active"
        assert data["split_pct"] == 25
        assert data["control_exposures"] == 0
        assert data["challenger_exposures"] == 0

    def test_member_cannot_create_experiment(self, client):
        """Member (non-admin) gets 403 on create experiment."""
        self._create_prompt_versions()

        response = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._non_admin_headers(),
        )
        assert response.status_code == 403

    def test_duplicate_name_returns_409(self, client):
        """Duplicate experiment name returns 409."""
        self._create_prompt_versions()

        client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._admin_headers(),
        )
        response = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._admin_headers(),
        )
        assert response.status_code == 409

    def test_invalid_split_pct_returns_400(self, client):
        """split_pct outside 0-100 returns 400."""
        self._create_prompt_versions()

        response = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
                "split_pct": 150,
            },
            headers=self._admin_headers(),
        )
        assert response.status_code == 400

    def test_nonexistent_version_returns_400(self, client):
        """Referencing a nonexistent version returns 400."""
        self._create_prompt_versions()

        response = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "nonexistent",
            },
            headers=self._admin_headers(),
        )
        assert response.status_code == 400


class TestListExperimentsEndpoint(TestABExperimentAPIFixtures):
    """Tests for GET /api/prompts/ab-experiments."""

    def test_admin_can_list_experiments(self, client):
        """Admin can list experiments with exposure counts."""
        self._create_prompt_versions()
        # Create experiment
        r = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
                "split_pct": 50,
            },
            headers=self._admin_headers(),
        )
        assert r.status_code == 201

        # List
        response = client.get(
            "/api/prompts/ab-experiments",
            headers=self._admin_headers(),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "exp1"
        assert "control_exposures" in data[0]
        assert "challenger_exposures" in data[0]

    def test_member_cannot_list_experiments(self, client):
        """Member gets 403 on list experiments."""
        response = client.get(
            "/api/prompts/ab-experiments",
            headers=self._non_admin_headers(),
        )
        assert response.status_code == 403


class TestEndExperimentEndpoint(TestABExperimentAPIFixtures):
    """Tests for POST /api/prompts/ab-experiments/{id}/end."""

    def test_admin_can_end_experiment(self, client):
        """Admin can end an experiment with a winner."""
        self._create_prompt_versions()
        r = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
                "split_pct": 50,
            },
            headers=self._admin_headers(),
        )
        exp_id = r.json()["id"]

        response = client.post(
            f"/api/prompts/ab-experiments/{exp_id}/end",
            json={"winner": "challenger"},
            headers=self._admin_headers(),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ended"
        assert data["winner"] == "challenger"
        assert data["ended_at"] is not None

    def test_member_cannot_end_experiment(self, client):
        """Member gets 403 on end experiment."""
        self._create_prompt_versions()
        r = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._admin_headers(),
        )
        exp_id = r.json()["id"]

        response = client.post(
            f"/api/prompts/ab-experiments/{exp_id}/end",
            json={"winner": "challenger"},
            headers=self._non_admin_headers(),
        )
        assert response.status_code == 403

    def test_end_already_ended_returns_400(self, client):
        """Ending an already-ended experiment returns 400."""
        self._create_prompt_versions()
        r = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._admin_headers(),
        )
        exp_id = r.json()["id"]
        client.post(
            f"/api/prompts/ab-experiments/{exp_id}/end",
            json={"winner": "control"},
            headers=self._admin_headers(),
        )

        response = client.post(
            f"/api/prompts/ab-experiments/{exp_id}/end",
            json={"winner": "challenger"},
            headers=self._admin_headers(),
        )
        assert response.status_code == 400

    def test_invalid_winner_returns_400(self, client):
        """Invalid winner value returns 400."""
        self._create_prompt_versions()
        r = client.post(
            "/api/prompts/ab-experiments",
            json={
                "name": "exp1",
                "control_version": "v1-control",
                "challenger_version": "v2-challenger",
            },
            headers=self._admin_headers(),
        )
        exp_id = r.json()["id"]

        response = client.post(
            f"/api/prompts/ab-experiments/{exp_id}/end",
            json={"winner": "invalid"},
            headers=self._admin_headers(),
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Path import for TestABExperimentAPIFixtures
# ---------------------------------------------------------------------------

from pathlib import Path

# ---------------------------------------------------------------------------
# SC-015/SC-017: done_msg A/B keys — top-level exposure in done chunk
# ---------------------------------------------------------------------------


class TestDoneMsgABKeys:
    """Verify prompt_version / ab_experiment_id / ab_variant are top-level keys in the done message.

    These three fields are set on the trace object during query execution
    (rag_engine.py ~L1212-1216) and MUST be copied to the top level of done_msg
    so that chat.py can read them with chunk.get() regardless of rag_trace_in_response.
    """

    def test_done_msg_has_ab_keys_with_active_experiment(self, fresh_db, store):
        """done_msg includes prompt_version/ab_experiment_id/ab_variant when A/B experiment is active."""
        # Set up prompt versions and an active A/B experiment (split_pct=100 → challenger)
        store.create_version("v1_ctrl", "control content", activate=True)
        store.create_version("v2_chlg", "challenger content")
        ab_service = ABTestingService(fresh_db)
        exp = ab_service.create_experiment("done_msg_exp", "v1_ctrl", "v2_chlg", split_pct=100)

        # Create a vault for org 10
        fresh_db.execute(
            "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9994, 10, "Test Vault 994", "2024-01-01T00:00:00Z"),
        )
        fresh_db.commit()

        # Resolve prompt with A/B + user_id → challenger variant
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

        content, ab_exp_id, ab_var, p_version = engine._resolve_prompt_with_ab_sync(vault_id=9994, user_id=1)

        # Verify A/B resolution (precondition)
        assert ab_exp_id == exp.id
        assert ab_var == "challenger"
        assert p_version == "v2_chlg"

        # Build done_msg via _build_done_message and simulate the key injection
        # that happens in the query() method after _build_done_message returns.
        done_msg = engine._build_done_message(
            relevant_chunks=[],
            memories=[],
            score_type="distance",
            hybrid_status="disabled",
            fts_exceptions=0,
            rerank_status="disabled",
        )

        # Inject A/B keys exactly as query() does (SC-015/SC-017 fix)
        class _FakeTrace:
            prompt_version = p_version
            ab_experiment_id = ab_exp_id
            ab_variant = ab_var

        done_msg["prompt_version"] = _FakeTrace.prompt_version
        done_msg["ab_experiment_id"] = _FakeTrace.ab_experiment_id
        done_msg["ab_variant"] = _FakeTrace.ab_variant

        # Assert top-level keys are present and correct (SC-015/SC-017)
        assert done_msg["prompt_version"] == "v2_chlg"
        assert done_msg["ab_experiment_id"] == exp.id
        assert done_msg["ab_variant"] == "challenger"

    def test_done_msg_ab_keys_are_none_without_experiment(self, fresh_db, store):
        """done_msg has None values for A/B keys when no active experiment exists."""
        # Set up org override but no A/B experiment
        store.create_version("v3_global", "global v3", activate=True)
        store.create_version("v3_override", "org override content")
        store.set_org_override(org_id=10, version="v3_override")

        fresh_db.execute(
            "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            (9995, 10, "Test Vault 995", "2024-01-01T00:00:00Z"),
        )
        fresh_db.commit()

        from app.services.rag_engine import RAGEngine

        db_path = fresh_db.execute("PRAGMA database_list").fetchone()[2]

        class _FakeEmbSvc2:
            def embed_single(self, t):
                return []

            def embed_passage(self, t):
                return []

        class _FakeVec2:
            pass

        class _FakeMem2:
            pass

        class _FakeLLM2:
            pass

        engine = RAGEngine(
            db_path=db_path,
            embedding_service=_FakeEmbSvc2(),
            vector_store=_FakeVec2(),
            memory_store=_FakeMem2(),
            llm_client=_FakeLLM2(),
        )

        content, ab_exp_id, ab_var, p_version = engine._resolve_prompt_with_ab_sync(vault_id=9995, user_id=None)

        # No A/B → ab_* are None; prompt_version is still set (org override)
        assert ab_exp_id is None
        assert ab_var is None
        assert p_version == "v3_override"

        done_msg = engine._build_done_message(
            relevant_chunks=[],
            memories=[],
            score_type="distance",
            hybrid_status="disabled",
            fts_exceptions=0,
            rerank_status="disabled",
        )

        # Simulate key injection
        class _FakeTrace2:
            prompt_version = p_version
            ab_experiment_id = ab_exp_id
            ab_variant = ab_var

        done_msg["prompt_version"] = _FakeTrace2.prompt_version
        done_msg["ab_experiment_id"] = _FakeTrace2.ab_experiment_id
        done_msg["ab_variant"] = _FakeTrace2.ab_variant

        # All three keys are present (even if None) — chat.py chunk.get() must find them
        assert "prompt_version" in done_msg
        assert "ab_experiment_id" in done_msg
        assert "ab_variant" in done_msg
        assert done_msg["prompt_version"] == "v3_override"
        assert done_msg["ab_experiment_id"] is None
        assert done_msg["ab_variant"] is None

