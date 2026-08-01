"""Permanent route tests for the Draft Room compile/ledger/Ready/export surface
(issue #436, SPEC sections 8.2, 9.1, 9.3, 12.5).

Complements ``test_draft_routes.py`` (which owns CRUD/upload/manual-revision/
capabilities/pagination-shape/SSE-subscribed coverage) rather than duplicating
it. This file drives the compile-adjacent routes ``draft_room.py`` added in
Wave 4: idempotent/concurrent compile enqueue, start-stage acceptance,
per-child-route authorization, evidence/claim/finding paging, finding
disposition, the human-only Ready transition, and export's fact/approval
filename and header matrix.

Harness copied from ``test_draft_routes.py``'s ``DraftRoomTestBase`` (itself
copied from ``test_tags_routes.py``). Full pipeline execution (stage-by-stage
correctness) is owned by ``test_draft_pipeline.py``; this file never runs
``draft_pipeline.run_compile`` -- it seeds compile jobs/revisions/evidence/
claims/findings directly at the SQLite layer (through ``DraftStore`` where a
public accessor exists, raw SQL otherwise) so route behavior can be exercised
against every ledger state without a real model or retrieval call.

KNOWN BUG (found while writing this file, NOT fixed here per scope): cancelling
a still-*pending* compile job (``POST .../jobs/{job_id}/cancel``) only updates
``draft_jobs.status`` -- ``DraftStore.request_job_cancel`` never touches
``drafts.status``, which was set to ``'queued'`` when the job was enqueued.
Both a fresh ``POST .../compile`` and ``POST .../jobs/{job_id}/retry`` gate on
``drafts.status in _COMPILE_ALLOWED_PRIOR_STATUSES`` (``draft``,
``needs_review``, ``failed``, ``cancelled``, ``ready`` -- ``'queued'`` is
deliberately absent), so a draft whose only compile job was cancelled while
still pending is left permanently stuck at ``status='queued'`` with no active
job and no legal path back to a compilable state through the HTTP API. A
*running* job's cancellation is fine, because ``draft_pipeline.run_compile``
itself observes the cancellation and settles both the job and the draft to a
terminal status before returning. ``test_retry_audit_has_no_content`` below
seeds a *failed* job/draft pair directly instead of going through cancel to
stay independent of this gap.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import CSRFManager, csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.draft_pipeline import COMPILE_STAGE_ORDER
from app.services.draft_store import DraftStore, sha256_text


class _PoolWithConnectionCM:
    """Copied verbatim from ``test_draft_routes.py`` -- see that file for the
    rationale (the SSE route reads ``request.app.state.db_pool`` directly)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._closed = False

    def get_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


def _default_brief(**overrides) -> dict:
    brief = {
        "piece_type": "article",
        "audience": "general readers",
        "purpose": "inform readers about the topic",
        "tone": "clear and direct",
        "target_words": 500,
        "transformation_strength": "moderate",
        "primary_input_id": None,
        "must_include": [],
        "must_avoid": [],
        "preserve_quotes": True,
        "preserve_numbers": True,
        "preserve_uncertainty": True,
        "drafting_priority": "balanced",
        "additional_instructions": "",
    }
    brief.update(overrides)
    return brief


class CompileRouteTestBase(unittest.TestCase):
    """Same fixture shape as ``DraftRoomTestBase`` in ``test_draft_routes.py``,
    plus direct SQLite seeding helpers for ledger states no route can reach
    without a real compile run."""

    OWNER_ID = 1
    OTHER_ID = 2
    READ_VAULT_ID = 2
    NO_ACCESS_VAULT_ID = 9

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_draft_room_enabled = settings.draft_room_enabled
        self._original_ollama_chat_url = settings.ollama_chat_url
        self._original_instant_chat_url = settings.instant_chat_url
        self._original_draft_allowed_model_origins = settings.draft_allowed_model_origins
        self._original_allow_local_services = os.environ.get("ALLOW_LOCAL_SERVICES")

        settings.data_dir = __import__("pathlib").Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.draft_room_enabled = True
        # Compile enqueue enforces the provider-origin allowlist (SPEC 9.2)
        # BEFORE the job is created -- point it at a loopback origin so
        # every compile/retry route test can get past that gate without a
        # real network call.
        settings.ollama_chat_url = "http://127.0.0.1:11434"
        settings.instant_chat_url = "http://127.0.0.1:11434"
        settings.draft_allowed_model_origins = ["http://127.0.0.1:11434"]
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"

        self._db_path = str(__import__("pathlib").Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = _PoolWithConnectionCM(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        app.state.db_pool = self._connection_pool
        app.state.csrf_manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1,'owner',?, 'Owner','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (2,'other',?, 'Other','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Read Vault','r')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (9,'No Access Vault','n')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2,1,'write',1)"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        settings.draft_room_enabled = self._original_draft_room_enabled
        settings.ollama_chat_url = self._original_ollama_chat_url
        settings.instant_chat_url = self._original_instant_chat_url
        settings.draft_allowed_model_origins = self._original_draft_allowed_model_origins
        if self._original_allow_local_services is None:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        else:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._original_allow_local_services
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
        if hasattr(app.state, "csrf_manager"):
            del app.state.csrf_manager
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- auth helpers --

    def _headers(self, user_id=OWNER_ID, username="owner", role="member"):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"
        }

    def _owner_headers(self):
        return self._headers(self.OWNER_ID, "owner")

    def _other_headers(self):
        return self._headers(self.OTHER_ID, "other")

    # -- draft/input fixtures via the real API --

    def _create_draft(self, *, headers=None, vault_id=READ_VAULT_ID, title="Compile Draft", brief=None):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": vault_id,
                "title": title,
                "mode": "rewrite",
                "tier": "standard",
                "brief": brief or _default_brief(),
            },
            headers=headers,
        )
        return resp

    def _upload_input(self, draft_id, *, headers=None, filename="manuscript.txt",
                       content=b"Hello world. This is the manuscript body."):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": "manuscript", "authority": "primary"},
            files={"file": (filename, content, "text/plain")},
            headers=headers,
        )
        return resp

    def _mark_input_ready(self, input_id):
        """Bypass ``DraftJobProcessor`` (not running under ``TestClient``) and
        move a freshly-uploaded input straight to ``parse_status='ready'`` so
        compile's "every input must finish parsing" gate is satisfied."""
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_inputs SET parse_status = 'ready', "
                "parsed_text = 'Hello world. This is the manuscript body.', "
                "parsed_text_sha256 = ?, parsed_char_count = 42 WHERE id = ?",
                (sha256_text("Hello world. This is the manuscript body."), input_id),
            )
            conn.execute(
                "UPDATE draft_jobs SET status = 'completed' WHERE input_id = ? "
                "AND job_type = 'parse_input'",
                (input_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_with_ready_input(self, **kwargs):
        draft_id = self._create_draft(**kwargs).json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return draft_id

    def _lock_version(self, draft_id, headers=None):
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=headers or self._owner_headers()
        )
        return resp.json()["summary"]["lock_version"]

    def _compile(self, draft_id, *, start_stage="research", idempotency_key=None,
                 lock_version=None, headers=None, base_revision_id=0):
        headers = dict(headers or self._owner_headers())
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        lv = lock_version if lock_version is not None else self._lock_version(draft_id, headers)
        base = None if base_revision_id == 0 else base_revision_id
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/compile",
            json={"base_revision_id": base, "lock_version": lv, "start_stage": start_stage},
            headers=headers,
        )

    # -- direct-SQLite ledger seeding (no real compile run) --

    def _store(self):
        conn = self._connection_pool.get_connection()
        return conn, DraftStore(conn)

    def _release(self, conn):
        self._connection_pool.release_connection(conn)

    def _seed_completed_job(self, draft_id, *, owner_id=None):
        owner_id = owner_id or self.OWNER_ID
        conn = self._connection_pool.get_connection()
        try:
            vault_id = conn.execute(
                "SELECT vault_id FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, active_stage, max_model_calls, timeout_seconds, "
                "prompt_bundle_version) "
                "VALUES (?, ?, ?, 'compile', 'completed', 'assemble', 40, 1800, 'test')",
                (draft_id, vault_id, owner_id),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_current_revision(self, draft_id, *, job_id, content_md,
                                fact_status="passed", source="pipeline"):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_revisions SET is_current = 0 WHERE draft_id = ?", (draft_id,)
            )
            next_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM draft_revisions "
                    "WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()[0]
            )
            cur = conn.execute(
                "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
                "content_md, content_sha256, fact_status, is_current, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    draft_id, job_id, next_no, source, content_md,
                    sha256_text(content_md), fact_status, self.OWNER_ID,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_fact_stage(self, job_id, *, candidate_sha256):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO draft_job_stages (job_id, stage, attempt, status, "
                "input_sha256, candidate_sha256) "
                "VALUES (?, 'fact', 1, 'completed', 'x', ?)",
                (job_id, candidate_sha256),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _set_draft_status(self, draft_id, status):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_ready_eligible_draft(self, content_md="# Body\n\nSome article text."):
        """A draft with exactly one clean, Fact-current, needs_review revision --
        the minimum state the Ready route should accept."""
        draft_id = self._draft_with_ready_input()
        job_id = self._seed_completed_job(draft_id)
        revision_id = self._seed_current_revision(
            draft_id, job_id=job_id, content_md=content_md, fact_status="passed"
        )
        self._seed_fact_stage(job_id, candidate_sha256=sha256_text(content_md))
        self._set_draft_status(draft_id, "needs_review")
        return draft_id, job_id, revision_id

    def _mark_ready(self, draft_id, revision_id, *, acknowledge_source_only=False, headers=None):
        headers = headers or self._owner_headers()
        lv = self._lock_version(draft_id, headers)
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/ready",
            json={"lock_version": lv, "acknowledge_source_only": acknowledge_source_only},
            headers=headers,
        )

    def _audit_metadata(self, event_type):
        """All ``security_audit_log`` rows of one event type, metadata decoded."""
        conn = self._connection_pool.get_connection()
        try:
            rows = conn.execute(
                "SELECT metadata_json FROM security_audit_log WHERE event_type = ? "
                "ORDER BY id",
                (event_type,),
            ).fetchall()
            return [json.loads(r[0]) if r[0] else {} for r in rows]
        finally:
            self._connection_pool.release_connection(conn)


# ── idempotency ────────────────────────────────────────────────────────────


class TestCompileIdempotency(CompileRouteTestBase):
    def test_same_key_same_fingerprint_returns_existing_job_no_second_row(self):
        draft_id = self._draft_with_ready_input()
        first = self._compile(draft_id, idempotency_key="abc-123")
        self.assertEqual(first.status_code, 202, first.text)
        job_id = first.json()["id"]

        second = self._compile(draft_id, idempotency_key="abc-123")
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["id"], job_id)

        conn = self._connection_pool.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? AND job_type = 'compile'",
                (draft_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            self._connection_pool.release_connection(conn)

    def test_same_key_different_fingerprint_is_409(self):
        """A fresh compile (no prior checkpoints) always normalizes every
        requested ``start_stage`` back to ``research`` (SPEC 8.1), so two
        first-time compiles reusing a key never differ on start_stage alone
        -- the fingerprint also folds in ``base_revision_id``, which this
        test varies instead to produce a genuinely different fingerprint."""
        draft_id = self._draft_with_ready_input()
        first = self._compile(
            draft_id, idempotency_key="dup-key", start_stage="research", base_revision_id=None
        )
        self.assertEqual(first.status_code, 202, first.text)

        second = self._compile(
            draft_id, idempotency_key="dup-key", start_stage="research", base_revision_id=999999
        )
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json()["code"], "idempotency_key_conflict")

    def test_idempotency_key_length_bounds_enforced(self):
        draft_id = self._draft_with_ready_input()
        too_long = self._compile(draft_id, idempotency_key="x" * 129)
        self.assertEqual(too_long.status_code, 422, too_long.text)
        self.assertEqual(too_long.json()["code"], "invalid_idempotency_key")

        empty = self._compile(draft_id, idempotency_key="")
        self.assertEqual(empty.status_code, 422, empty.text)

        one_char = self._compile(draft_id, idempotency_key="k")
        self.assertEqual(one_char.status_code, 202, one_char.text)

    def test_idempotency_key_must_be_ascii(self):
        draft_id = self._draft_with_ready_input()
        lv = self._lock_version(draft_id)
        headers = dict(self._owner_headers())
        # httpx's own header layer rejects a `str` with non-ASCII bytes
        # before the request is even built, so this exercises the server's
        # ASCII check the way a real non-ASCII byte sequence would arrive:
        # as raw UTF-8-encoded header bytes.
        headers["Idempotency-Key"] = "café".encode("utf-8")
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/compile",
            json={"base_revision_id": None, "lock_version": lv, "start_stage": "research"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "invalid_idempotency_key")


# ── start-stage acceptance ───────────────────────────────────────────────────


class TestStartStageAcceptance(CompileRouteTestBase):
    def test_assemble_is_rejected_as_a_start_stage(self):
        draft_id = self._draft_with_ready_input()
        resp = self._compile(draft_id, start_stage="assemble")
        self.assertEqual(resp.status_code, 422, resp.text)
        # FastAPI's framework validation error retains the standard 422 shape.
        self.assertIn("start_stage", resp.text)

    def test_intake_is_rejected_as_a_start_stage(self):
        draft_id = self._draft_with_ready_input()
        resp = self._compile(draft_id, start_stage="intake")
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_every_allowed_start_stage_is_accepted_and_still_reaches_fact_and_assemble(self):
        allowed = ("research", "outline", "draft", "lint", "copy", "standards", "fact")
        self.assertEqual(
            allowed, tuple(s for s in COMPILE_STAGE_ORDER if s not in ("intake", "assemble"))
        )
        for stage in allowed:
            with self.subTest(stage=stage):
                draft_id = self._draft_with_ready_input(title=f"Draft {stage}")
                resp = self._compile(draft_id, start_stage=stage)
                self.assertEqual(resp.status_code, 202, resp.text)
                job = resp.json()
                # A fresh compile has no prior checkpoints, so every requested
                # stage normalizes back to the first prerequisite (research) --
                # SPEC section 8.1 "moves start_stage backward to the first
                # missing/mismatched prerequisite". The orchestrator's own
                # COMPILE_STAGE_ORDER (asserted above) guarantees every run
                # still walks through fact and assemble regardless of where it
                # resumes; the actual pipeline walk is exercised by
                # test_draft_pipeline.py.
                self.assertEqual(job["start_stage"], "research")

                # A malformed/unknown value is a framework 422, not silently
                # coerced.
                bad = self._compile(draft_id, start_stage="not_a_stage")
                self.assertEqual(bad.status_code, 422, bad.text)


# ── concurrency ──────────────────────────────────────────────────────────────


class TestConcurrentCompile(CompileRouteTestBase):
    def test_concurrent_compiles_yield_exactly_one_active_job(self):
        draft_id = self._draft_with_ready_input()
        lv = self._lock_version(draft_id)
        headers = self._owner_headers()

        results = []
        barrier = threading.Barrier(2)

        def _fire():
            barrier.wait(timeout=5)
            resp = self.client.post(
                f"/api/draft-room/drafts/{draft_id}/compile",
                json={"base_revision_id": None, "lock_version": lv, "start_stage": "research"},
                headers=headers,
            )
            results.append(resp)

        threads = [threading.Thread(target=_fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(results), 2)
        statuses = sorted(r.status_code for r in results)
        # One request wins (202); the other loses to either the lock_version
        # bump or the one-active-compile index, both of which are 409s.
        self.assertEqual(statuses, [202, 409])

        conn = self._connection_pool.get_connection()
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'compile' AND status IN ('pending','running')",
                (draft_id,),
            ).fetchone()[0]
            self.assertEqual(active, 1)
        finally:
            self._connection_pool.release_connection(conn)


# ── authorization (SPEC section 9.1) ────────────────────────────────────────


class TestChildRouteAuthorization(CompileRouteTestBase):
    """Every new child route: non-owner -> 404 (never 403, existence must not
    leak); revoked vault read -> 403; owner keeps working."""

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft()
        self.finding_id = None  # populated per-test where needed

    def _revoke_vault_read(self):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "DELETE FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (self.READ_VAULT_ID, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_non_owner_gets_404_never_403_on_every_new_route(self):
        other = self._other_headers()
        checks = [
            ("post", f"/api/draft-room/drafts/{self.draft_id}/compile",
             {"base_revision_id": None, "lock_version": 1, "start_stage": "research"}),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/jobs/{self.job_id}/stages", None),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/evidence", None),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/claims", None),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/findings", None),
            ("post", f"/api/draft-room/drafts/{self.draft_id}/findings/999/disposition",
             {"action": "dismiss", "base_revision_id": None, "lock_version": 1}),
            ("post", f"/api/draft-room/drafts/{self.draft_id}/revisions/{self.revision_id}/ready",
             {"lock_version": 1}),
            ("post", f"/api/draft-room/drafts/{self.draft_id}/revisions/{self.revision_id}/export",
             None),
        ]
        for method, path, body in checks:
            with self.subTest(path=path):
                fn = getattr(self.client, method)
                resp = fn(path, json=body, headers=other) if body is not None or method == "post" \
                    else fn(path, headers=other)
                self.assertEqual(resp.status_code, 404, resp.text)
                self.assertNotEqual(resp.status_code, 403)

    def test_child_id_from_another_draft_never_resolves(self):
        """A job/revision that exists but belongs to a *different* draft owned
        by the same user must not resolve through this draft's routes -- a
        child ID alone is never sufficient authorization (SPEC 9.1 rule 5)."""
        other_draft_id = self._draft_with_ready_input(title="Other Draft")
        resp = self.client.get(
            f"/api/draft-room/drafts/{other_draft_id}/jobs/{self.job_id}/stages",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_revoked_vault_read_is_403_content_ops_owner_still_works_after_grant(self):
        self._revoke_vault_read()
        owner = self._owner_headers()
        for method, path in [
            ("get", f"/api/draft-room/drafts/{self.draft_id}/evidence"),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/claims"),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/findings"),
            ("get", f"/api/draft-room/drafts/{self.draft_id}/jobs/{self.job_id}/stages"),
        ]:
            with self.subTest(path=path):
                resp = getattr(self.client, method)(path, headers=owner)
                self.assertEqual(resp.status_code, 403, resp.text)
                self.assertEqual(resp.json()["code"], "vault_access_revoked")

        # Owner metadata listing and job cancel stay available per SPEC 9.1
        # rule 3 even after revocation.
        listing = self.client.get("/api/draft-room/drafts", headers=owner)
        self.assertEqual(listing.status_code, 200, listing.text)

        # Regrant and confirm the owner can act again.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (?, ?, 'read', ?)",
                (self.READ_VAULT_ID, self.OWNER_ID, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self.client.get(f"/api/draft-room/drafts/{self.draft_id}/evidence", headers=owner)
        self.assertEqual(resp.status_code, 200, resp.text)


# ── paging / no-embedded-bodies ──────────────────────────────────────────────


class TestLedgerPaging(CompileRouteTestBase):
    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md="# Heading\n\nThe review window is thirty days long. More text."
        )

    def test_evidence_listing_paginated_envelope(self):
        input_id = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}", headers=self._owner_headers()
        ).json()["inputs"][0]["id"]
        conn, store = self._store()
        try:
            store.insert_evidence(
                job_id=self.job_id, label="S1", source_kind="draft_input",
                title="Manuscript", passage="the review window is thirty days",
                source_content_sha256=sha256_text("x"),
                draft_input_id=input_id,
            )
        finally:
            self._release(conn)

        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/evidence?job_id={self.job_id}",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("items", body)
        self.assertIn("total", body)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["per_page"], 50)

    def test_stage_listing_never_embeds_content_by_default(self):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO draft_job_stages (job_id, stage, attempt, status, "
                "input_sha256, artifact_sha256, content_md) "
                "VALUES (?, 'draft', 1, 'completed', 'x', 'y', 'SECRET SECTION TEXT')",
                (self.job_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/jobs/{self.job_id}/stages",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("SECRET SECTION TEXT", resp.text)
        for item in resp.json()["items"]:
            self.assertIsNone(item["content_md"])

        included = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/jobs/{self.job_id}/stages"
            "?include_content=true",
            headers=self._owner_headers(),
        )
        self.assertIn("SECRET SECTION TEXT", included.text)

    def test_claims_listing_paginated_and_findings_listing_paginated(self):
        conn, store = self._store()
        try:
            store.insert_claim(
                revision_id=self.revision_id, ordinal=1,
                claim_text="the review window is thirty days",
                span_start=13, span_end=46,
                claim_type="factual", status="supported", severity="info",
            )
            store.insert_finding(
                draft_id=self.draft_id, stage="standards", rule_id="rv-1",
                rule_version="1", category="style", severity="warning",
                message="passive voice", revision_id=self.revision_id, job_id=self.job_id,
            )
        finally:
            self._release(conn)

        claims = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/claims", headers=self._owner_headers()
        )
        self.assertEqual(claims.status_code, 200, claims.text)
        self.assertEqual(claims.json()["page"], 1)
        self.assertEqual(len(claims.json()["items"]), 1)

        findings = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings", headers=self._owner_headers()
        )
        self.assertEqual(findings.status_code, 200, findings.text)
        self.assertEqual(findings.json()["page"], 1)
        self.assertEqual(len(findings.json()["items"]), 1)

    def test_findings_status_and_severity_filters_are_validated(self):
        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings?status=not_a_status",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 422, resp.text)

        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings?severity=not_a_severity",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 422, resp.text)


# ── finding disposition (SPEC section 8.2) ──────────────────────────────────


class TestFindingDisposition(CompileRouteTestBase):
    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        # A span that exactly covers "a fix" (12..17), with a suggestion.
        self.span_start = self.CONTENT.index("a fix")
        self.span_end = self.span_start + len("a fix")

    def _insert_finding(self, *, severity="warning", waivable=True, suggestion="a correction"):
        conn, store = self._store()
        try:
            return store.insert_finding(
                draft_id=self.draft_id, stage="standards", rule_id="rule-x",
                rule_version="1", category="style", severity=severity,
                message="needs a fix", revision_id=self.revision_id, job_id=self.job_id,
                waivable=waivable, suggestion=suggestion,
                span_start=self.span_start, span_end=self.span_end,
            )
        finally:
            self._release(conn)

    def _dispose(self, finding_id, action, *, note=None, base_revision_id=None):
        lv = self._lock_version(self.draft_id)
        base = base_revision_id if base_revision_id is not None else self.revision_id
        return self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/{finding_id}/disposition",
            json={"action": action, "base_revision_id": base, "lock_version": lv, "note": note},
            headers=self._owner_headers(),
        )

    def test_apply_creates_immutable_manual_revision_and_invalidates_fact_and_ready(self):
        # First mark the draft Ready so we can prove apply invalidates it.
        ready = self._mark_ready(self.draft_id, self.revision_id)
        self.assertEqual(ready.status_code, 200, ready.text)

        finding_id = self._insert_finding()
        resp = self._dispose(finding_id, "apply", note="fixing per style desk")
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["finding"]["status"], "applied")
        new_revision_id = body["revision"]["id"]
        self.assertNotEqual(new_revision_id, self.revision_id)
        self.assertEqual(body["revision"]["source"], "manual")
        self.assertEqual(body["revision"]["fact_status"], "not_run")

        # The old revision's bytes are untouched (immutability).
        old = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/revisions/{self.revision_id}",
            headers=self._owner_headers(),
        ).json()
        self.assertEqual(old["content_md"], self.CONTENT)
        self.assertFalse(old["summary"]["is_current"])

        new = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/revisions/{new_revision_id}",
            headers=self._owner_headers(),
        ).json()
        self.assertIn("a correction", new["content_md"])
        self.assertTrue(new["summary"]["is_current"])

        detail = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}", headers=self._owner_headers()
        ).json()
        self.assertEqual(detail["summary"]["status"], "needs_review")

    def test_apply_with_stale_span_is_409_never_a_silent_overwrite(self):
        finding_id = self._insert_finding()
        # Mutate the current revision's bytes out from under the finding by
        # applying a manual revision first -- the span the finding was raised
        # against no longer exists at those exact bytes.
        lv = self._lock_version(self.draft_id)
        edit = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/revisions",
            json={"base_revision_id": self.revision_id, "lock_version": lv,
                  "content_md": "# Heading\n\nCompletely different body now."},
            headers=self._owner_headers(),
        )
        self.assertEqual(edit.status_code, 201, edit.text)
        new_current_id = edit.json()["summary"]["id"]

        resp = self._dispose(finding_id, "apply", note="stale now",
                              base_revision_id=new_current_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn(resp.json()["code"], ("stale_span", "finding_revision_stale"))

        # The manual edit survives unchanged -- no silent overwrite.
        current = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/revisions/{new_current_id}",
            headers=self._owner_headers(),
        ).json()
        self.assertEqual(current["content_md"], "# Heading\n\nCompletely different body now.")

    def test_dismiss_is_refused_for_a_blocker(self):
        finding_id = self._insert_finding(severity="blocker")
        resp = self._dispose(finding_id, "dismiss", note="not needed")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "blocker_not_dismissable")

    def test_dismiss_succeeds_for_a_non_blocker(self):
        finding_id = self._insert_finding(severity="info")
        resp = self._dispose(finding_id, "dismiss", note="acknowledged")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["finding"]["status"], "dismissed")

    def test_waive_refused_without_waivable(self):
        finding_id = self._insert_finding(severity="blocker", waivable=False)
        resp = self._dispose(finding_id, "waive", note="waiving anyway")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "finding_not_waivable")

    def test_waive_refused_with_empty_reason(self):
        finding_id = self._insert_finding(severity="blocker", waivable=True)
        resp = self._dispose(finding_id, "waive", note="   ")
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "waiver_reason_required")

        resp = self._dispose(finding_id, "waive", note=None)
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_waive_refused_with_changed_span_hash(self):
        """A finding's span hash is re-derived from the *exact revision it was
        raised against* (revisions are otherwise immutable, so this can only
        happen through direct corruption/a bug elsewhere) -- the waiver
        route must still refuse rather than trust the stored
        ``span_text_sha256`` blindly."""
        finding_id = self._insert_finding(severity="blocker", waivable=True)
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_revisions SET content_md = ? WHERE id = ?",
                ("# Heading\n\nEntirely rewritten paragraph body.", self.revision_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self._dispose(finding_id, "waive", note="waiving stale text")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "stale_span")

    def test_waive_succeeds_and_records_actor_reason_rule_version(self):
        finding_id = self._insert_finding(severity="blocker", waivable=True)
        resp = self._dispose(finding_id, "waive", note="acceptable risk, documented")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["finding"]
        self.assertEqual(body["status"], "waived")
        self.assertEqual(body["resolved_by"], self.OWNER_ID)
        self.assertEqual(body["resolution_note"], "acceptable risk, documented")
        self.assertEqual(body["waiver_rule_version"], "1")
        self.assertIsNotNone(body["waiver_text_sha256"])

    def test_waiver_with_bumped_rule_version_no_longer_protects_ready(self):
        """SPEC section 8.2/12.5 rule 6: a waiver only counts against the SAME
        rule version it was granted under. Simulates a later boilerplate/rule
        bump (``rule_version`` on the finding row itself moves forward, as a
        deployed rule update would produce) and asserts Ready refuses the now
        -mismatched waiver rather than trusting the stale grant."""
        finding_id = self._insert_finding(severity="blocker", waivable=True)
        resp = self._dispose(finding_id, "waive", note="ok for v1")
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_findings SET rule_version = '2' WHERE id = ?", (finding_id,)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        ready = self._mark_ready(self.draft_id, self.revision_id)
        self.assertEqual(ready.status_code, 409, ready.text)
        self.assertEqual(ready.json()["code"], "invalid_waiver")


# ── Ready eligibility (SPEC section 12.5) ───────────────────────────────────


class TestReadyEligibility(CompileRouteTestBase):
    def test_no_current_revision_is_refused(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft()
        # Make it not-current by inserting a newer current revision.
        self._seed_current_revision(draft_id, job_id=job_id, content_md="# Newer\n\nbody")
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "not_current_revision")

    def test_active_job_is_refused(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft()
        conn = self._connection_pool.get_connection()
        try:
            vault_id = conn.execute(
                "SELECT vault_id FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', 'pending', 40, 1800)",
                (draft_id, vault_id, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "active_job")

    def test_fact_hash_mismatch_is_refused(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(
            content_md="# A\n\noriginal text"
        )
        # Overwrite the fact stage's candidate hash so it no longer matches.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_job_stages SET candidate_sha256 = 'deadbeef' "
                "WHERE job_id = ? AND stage = 'fact'",
                (job_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "fact_candidate_mismatch")

    def test_non_waivable_blocker_is_refused(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft()
        conn, store = self._store()
        try:
            store.insert_finding(
                draft_id=draft_id, stage="fact", rule_id="unsupported-claim",
                rule_version="1", category="factuality", severity="blocker",
                message="unsupported claim", revision_id=revision_id, job_id=job_id,
                waivable=False,
            )
        finally:
            self._release(conn)
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "non_waivable_blocker")

    def test_stale_evidence_is_refused(self):
        content_md = "# A\n\nthe review window is thirty days"
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(content_md=content_md)
        # Insert evidence pointing at the draft's own manuscript input but
        # with a source_content_sha256 that does not match the input's live
        # parsed-text hash -- exactly what SPEC 12.6 re-resolution catches.
        input_id = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        ).json()["inputs"][0]["id"]
        conn, store = self._store()
        try:
            store.insert_evidence(
                job_id=job_id, label="D1", source_kind="draft_input",
                title="Manuscript", passage="the review window is thirty days",
                source_content_sha256="not-the-real-hash",
                draft_input_id=input_id,
            )
        finally:
            self._release(conn)
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "evidence_changed")

        # And the revision is stamped invalidated as a side effect, per SPEC 12.6.
        detail = self.client.get(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}",
            headers=self._owner_headers(),
        ).json()
        self.assertEqual(detail["summary"]["fact_status"], "invalidated")

    def test_missing_source_only_acknowledgement_is_refused(self):
        content_md = "# A\n\nsource-only body"
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(content_md=content_md)
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_revisions SET qa_summary_json = ? WHERE id = ?",
                (json.dumps({"source_only": True}), revision_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self._mark_ready(draft_id, revision_id, acknowledge_source_only=False)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "source_only_acknowledgment_required")

        ok = self._mark_ready(draft_id, revision_id, acknowledge_source_only=True)
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_clean_ledger_succeeds_and_there_is_no_automatic_path_to_ready(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft()
        resp = self._mark_ready(draft_id, revision_id)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "ready")

        # Structural guarantee (SPEC section 8.2/12.5 rule 8): the only
        # candidate write-path function whose source assigns
        # ``status = 'ready'`` is ``_sync_mark_ready``, reachable solely from
        # the authenticated ``mark_revision_ready`` handler. This inspects
        # the actual source text of every other function that legally writes
        # ``drafts.status`` rather than trusting a docstring claim.
        import inspect

        from app.api.routes import draft_room

        candidates = {
            "_sync_mark_ready": draft_room._sync_mark_ready,
            "_sync_enqueue_compile": draft_room._sync_enqueue_compile,
            "_sync_apply_finding": draft_room._sync_apply_finding,
            "archive_draft": draft_room.archive_draft,
            "restore_draft": draft_room.restore_draft,
            "update_draft": draft_room.update_draft,
        }
        writers = [
            name
            for name, fn in candidates.items()
            if "status = 'ready'" in inspect.getsource(fn)
            or 'status = "ready"' in inspect.getsource(fn)
        ]
        self.assertEqual(writers, ["_sync_mark_ready"])

        # draft_pipeline (the orchestrator) never assigns status='ready' at
        # all -- Assemble sets needs_review, per SPEC section 11.9.
        from app.services import draft_pipeline as pipeline_module

        pipeline_source = inspect.getsource(pipeline_module)
        self.assertNotIn("status = 'ready'", pipeline_source)
        self.assertNotIn('status = "ready"', pipeline_source)


# ── export (SPEC section 8.2 filename/header matrix) ────────────────────────


class TestExportMatrix(CompileRouteTestBase):
    def _export(self, draft_id, revision_id, *, ack=False):
        suffix = "?acknowledge_not_fact_checked=true" if ack else ""
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export{suffix}",
            headers=self._owner_headers(),
        )

    def test_non_fact_current_requires_ack_and_uses_unverified_filename(self):
        draft_id = self._draft_with_ready_input()
        job_id = self._seed_completed_job(draft_id)
        content_md = "# Draft\n\nnot fact-checked yet"
        revision_id = self._seed_current_revision(
            draft_id, job_id=job_id, content_md=content_md, fact_status="not_run"
        )
        self._set_draft_status(draft_id, "needs_review")

        without_ack = self._export(draft_id, revision_id, ack=False)
        self.assertEqual(without_ack.status_code, 422, without_ack.text)
        self.assertEqual(without_ack.json()["code"], "export_ack_required")

        with_ack = self._export(draft_id, revision_id, ack=True)
        self.assertEqual(with_ack.status_code, 200, with_ack.text)
        self.assertEqual(with_ack.text, content_md)
        self.assertEqual(with_ack.headers["X-Draft-Fact-Status"], "not_run")
        self.assertEqual(with_ack.headers["X-Draft-Approval-Status"], "not_ready")
        self.assertIn("UNVERIFIED.md", with_ack.headers["content-disposition"])

    def test_fact_checked_not_ready_uses_review_filename_no_ack_required(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(
            content_md="# Draft\n\nfact-checked but not yet approved"
        )
        resp = self._export(draft_id, revision_id, ack=False)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("REVIEW.md", resp.headers["content-disposition"])
        self.assertNotIn("UNVERIFIED", resp.headers["content-disposition"])
        self.assertEqual(resp.headers["X-Draft-Fact-Status"], "passed")
        self.assertEqual(resp.headers["X-Draft-Approval-Status"], "not_ready")

    def test_current_ready_revision_uses_ordinary_filename(self):
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(
            content_md="# Draft\n\napproved content, byte exact"
        )
        ready = self._mark_ready(draft_id, revision_id)
        self.assertEqual(ready.status_code, 200, ready.text)

        resp = self._export(draft_id, revision_id, ack=False)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.text, "# Draft\n\napproved content, byte exact")
        disposition = resp.headers["content-disposition"]
        self.assertNotIn("UNVERIFIED", disposition)
        self.assertNotIn("REVIEW", disposition)
        self.assertEqual(resp.headers["X-Draft-Fact-Status"], "passed")
        self.assertEqual(resp.headers["X-Draft-Approval-Status"], "ready")

    def test_export_bytes_are_byte_exact_including_trailing_whitespace(self):
        content_md = "# Title\r\n\r\nBody with trailing spaces   \nand a final newline\n"
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(content_md=content_md)
        resp = self._export(draft_id, revision_id, ack=False)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.text, content_md)


# ── capabilities honesty ────────────────────────────────────────────────────


class TestCapabilitiesHonesty(CompileRouteTestBase):
    def test_promote_available_stays_false_and_no_promote_route(self):
        resp = self.client.get("/api/draft-room/capabilities", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["promote_available"])

        from app.api.routes.draft_room import router as draft_room_router

        routes = {
            (method, route.path)
            for route in draft_room_router.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("POST", "/draft-room/drafts/{draft_id}/promote"), routes)


# ── audit metadata never carries content (SPEC section 9.3) ────────────────


class TestAuditMetadataNoContent(CompileRouteTestBase):
    FORBIDDEN_SUBSTRINGS = (
        "manuscript body", "SECRET SECTION TEXT", "the review window is thirty days",
        "Traceback", "raise ", "Exception:",
    )

    def _assert_metadata_clean(self, metadata_list):
        for metadata in metadata_list:
            blob = json.dumps(metadata)
            for forbidden in self.FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(forbidden, blob, f"leaked content in audit metadata: {blob!r}")
            # Every value must be a short scalar identifier/hash/version --
            # never an embedded document body.
            for key, value in metadata.items():
                if isinstance(value, str):
                    self.assertLess(
                        len(value), 200,
                        f"suspiciously long string in audit metadata field {key!r}",
                    )

    def test_compile_audit_has_no_content(self):
        draft_id = self._draft_with_ready_input()
        resp = self._compile(draft_id)
        self.assertEqual(resp.status_code, 202, resp.text)
        self._assert_metadata_clean(self._audit_metadata("draft_compile_requested"))

    def test_cancel_audit_has_no_content(self):
        draft_id = self._draft_with_ready_input()
        job_id = self._compile(draft_id).json()["id"]
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self._assert_metadata_clean(self._audit_metadata("draft_job_cancelled"))

    def test_retry_audit_has_no_content(self):
        # Seed a *failed* compile job directly rather than cancelling a
        # pending one through the route: see KNOWN BUG note in this file's
        # module docstring -- cancelling a still-pending compile job never
        # resets drafts.status off 'queued', which then permanently blocks
        # both a fresh compile and a retry with 409 invalid_state. Seeding a
        # terminal 'failed' job/draft pair (the state draft_pipeline itself
        # produces on a real failure) keeps this test independent of that gap.
        draft_id = self._draft_with_ready_input()
        job_id = self._seed_completed_job(draft_id)
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'failed', error_code = 'model_unavailable' "
                "WHERE id = ?",
                (job_id,),
            )
            conn.execute("UPDATE drafts SET status = 'failed' WHERE id = ?", (draft_id,))
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        self._assert_metadata_clean(self._audit_metadata("draft_job_retried"))

    def test_apply_dismiss_waive_ready_export_audit_have_no_content(self):
        content_md = "# Heading\n\nthis needs a fix here and stays fine elsewhere."
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(content_md=content_md)
        span_start = content_md.index("a fix")
        span_end = span_start + len("a fix")

        conn, store = self._store()
        try:
            applyable = store.insert_finding(
                draft_id=draft_id, stage="standards", rule_id="rule-a", rule_version="1",
                category="style", severity="warning", message="needs a fix",
                revision_id=revision_id, job_id=job_id, waivable=True,
                suggestion="a correction", span_start=span_start, span_end=span_end,
            )
            dismissable = store.insert_finding(
                draft_id=draft_id, stage="standards", rule_id="rule-b", rule_version="1",
                category="style", severity="info", message="minor style note",
                revision_id=revision_id, job_id=job_id, waivable=True,
            )
            waivable = store.insert_finding(
                draft_id=draft_id, stage="standards", rule_id="rule-c", rule_version="1",
                category="boilerplate", severity="blocker", message="boilerplate phrase",
                revision_id=revision_id, job_id=job_id, waivable=True,
                span_start=span_start, span_end=span_end,
            )
        finally:
            self._release(conn)

        lv = self._lock_version(draft_id)
        self.client.post(
            f"/api/draft-room/drafts/{draft_id}/findings/{dismissable}/disposition",
            json={"action": "dismiss", "base_revision_id": revision_id, "lock_version": lv,
                  "note": "ok"},
            headers=self._owner_headers(),
        )
        lv = self._lock_version(draft_id)
        self.client.post(
            f"/api/draft-room/drafts/{draft_id}/findings/{waivable}/disposition",
            json={"action": "waive", "base_revision_id": revision_id, "lock_version": lv,
                  "note": "accepted risk"},
            headers=self._owner_headers(),
        )
        lv = self._lock_version(draft_id)
        apply_resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/findings/{applyable}/disposition",
            json={"action": "apply", "base_revision_id": revision_id, "lock_version": lv,
                  "note": "applying"},
            headers=self._owner_headers(),
        )
        self.assertEqual(apply_resp.status_code, 201, apply_resp.text)

        self._assert_metadata_clean(self._audit_metadata("draft_finding_dismiss"))
        self._assert_metadata_clean(self._audit_metadata("draft_finding_waive"))
        self._assert_metadata_clean(self._audit_metadata("draft_finding_apply"))

        # Ready ran on the *original* revision before the apply happened
        # above invalidated it, so seed a second clean revision to exercise
        # the ready/export audit metadata independently.
        draft_id2, job_id2, revision_id2 = self._seed_ready_eligible_draft(
            content_md="# Clean\n\nnothing to disclose here"
        )
        ready_resp = self._mark_ready(draft_id2, revision_id2)
        self.assertEqual(ready_resp.status_code, 200, ready_resp.text)
        self._assert_metadata_clean(self._audit_metadata("draft_ready_marked"))

        export_resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id2}/revisions/{revision_id2}/export",
            headers=self._owner_headers(),
        )
        self.assertEqual(export_resp.status_code, 200, export_resp.text)
        self._assert_metadata_clean(self._audit_metadata("draft_exported"))


if __name__ == "__main__":
    unittest.main()
