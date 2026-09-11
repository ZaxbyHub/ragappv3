"""Acceptance / regression tests for issue #516 (DRAFT-020, DRAFT-022,
DRAFT-023, DRAFT-024).

Four acceptance criteria, one test per AC, each printing a sentinel line
(``AC<n> CHECK: PASS`` / ``AC<n> CHECK: FAIL``, visible under ``-s``) so a CI
log names the acceptance criterion even when the traceback scrolls. Every
check asserts REQUIRED BEHAVIOR through current public APIs (HTTP routes via
TestClient, ``DraftStore`` public methods, ``draft_promotion.promote_input``)
against a real temp SQLite database:

* AC13 (DRAFT-020) -- Ready gating and summary blocker counts must be scoped
  to the draft's CURRENT revision. Today ``_sync_mark_ready`` and the
  ``_sync_open_blocker_count``/``_sync_ledger_counts`` roll-ups query
  ``draft_findings`` by ``draft_id`` only, so a historical (older-revision)
  open non-waivable blocker permanently wedges a corrected revision out of
  Ready and inflates the reported counts.
* AC14 (DRAFT-022) -- After a revision is marked Ready, a MATERIAL brief or
  input-metadata change must atomically move the draft back to
  ``needs_review`` and clear the Ready pointer (export then classifies
  ``not_ready``), while an unchanged-value update and a title-only update
  keep Ready. Today ``DraftStore.update_draft`` and
  ``DraftStore.update_input_metadata`` never invalidate.
* AC15 (DRAFT-023) -- If cancellation lands after input promotion's ingestion
  enqueue, compensation must also remove the orphaned ingestion job, so no
  pending document/provenance/ingestion remains. Today ``_promote``'s
  compensation deletes the files row, the promotion row and the bytes but
  leaves the enqueued job stranded against the deleted file.
* AC16 (DRAFT-024) -- An infrastructure error (raw ``sqlite3`` error, NOT a
  ``DraftStoreError``) from ``DraftStore.enqueue_parse_job`` during upload
  must not strand the input: no input row survives without a job, and a
  retry of the same content succeeds without a restart. Today the route's
  ``except DraftRoomHTTPError`` does not catch the raw error, so the input
  row stays committed and the retry 409s on ``duplicate_input``.

Harness copied from ``test_draft_routes.py``'s ``DraftRoomTestBase`` (per this
package's convention of duplicating rather than importing across test files),
extended with the direct-SQLite ready-eligibility seeding helpers from
``test_draft_compile_routes.py`` so AC13/AC14 can reach Ready through the REAL
``POST .../revisions/{id}/ready`` route (no direct ``drafts``-column seeding
of Ready state is needed or used), and with the direct-service promotion
pattern from ``test_draft_promote.py`` for AC15.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch

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
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_promotion import promote_input
from app.services.draft_store import DraftConflictError, DraftStore, sha256_text


class _PoolWithConnectionCM:
    """Thread-safe SQLite pool exposing both the ``get_connection``/
    ``release_connection`` idiom (backs the ``get_db`` override) and the
    ``with pool.connection() as conn`` context manager production code
    requires from ``request.app.state.db_pool`` -- copied verbatim from
    ``test_draft_routes.py``."""

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


class Issue516AcceptanceBase(unittest.TestCase):
    """Route-level Draft Room harness + ready-eligibility seeding helpers."""

    OWNER_ID = 1
    OTHER_ID = 2
    READ_VAULT_ID = 2

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_draft_room_enabled = settings.draft_room_enabled

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.draft_room_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

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

    # ── acceptance-check runner ────────────────────────────────────────────

    def _run_checks(self, ac_label: str, checks) -> None:
        """Run labeled zero-arg check callables.

        Every check runs even after an earlier one fails, so one execution
        reports the full per-criterion diagnosis; the first failure is then
        re-raised (chained) so the test fails with the primary reason.
        """
        failures = []
        for label, check in checks:
            try:
                check()
                print(f"{ac_label} [{label}]: ok")
            except Exception as exc:  # noqa: BLE001 -- report, re-raise below
                failures.append((label, exc))
                print(f"{ac_label} [{label}]: FAILED: {exc}")
        if failures:
            first_label, first_exc = failures[0]
            summary = "; ".join(f"[{lbl}] {exc}" for lbl, exc in failures)
            raise AssertionError(
                f"{ac_label} failed ({len(failures)} check(s)): {summary}"
            ) from first_exc

    # ── auth / API helpers ─────────────────────────────────────────────────

    def _headers(self, user_id=OWNER_ID, username="owner", role="member"):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"
        }

    def _owner_headers(self):
        return self._headers(self.OWNER_ID, "owner")

    def _create_draft(self, *, title="Issue516 Draft", brief=None):
        return self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": self.READ_VAULT_ID,
                "title": title,
                "mode": "rewrite",
                "tier": "standard",
                "brief": brief or _default_brief(),
            },
            headers=self._owner_headers(),
        )

    def _post_input(self, client, draft_id, *, content, filename, headers=None):
        return client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": "manuscript", "authority": "primary"},
            files={"file": (filename, content, "text/plain")},
            headers=headers or self._owner_headers(),
        )

    def _upload_input(self, draft_id, *, content=b"Hello world. This is the manuscript body.", filename="manuscript.txt"):
        return self._post_input(self.client, draft_id, content=content, filename=filename)

    def _lock_version(self, draft_id):
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        return resp.json()["summary"]["lock_version"]

    # ── direct-SQLite seeding / inspection (test_draft_compile_routes.py) ──

    def _mark_input_ready(self, input_id: int) -> None:
        """Bypass ``DraftJobProcessor`` (not running under TestClient) and move
        a freshly-uploaded input straight to ``parse_status='ready'``."""
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_inputs SET parse_status = 'ready', "
                "parsed_text = 'parsed body', parsed_char_count = 11 WHERE id = ?",
                (input_id,),
            )
            conn.execute(
                "UPDATE draft_jobs SET status = 'completed' WHERE input_id = ? "
                "AND job_type = 'parse_input'",
                (input_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_completed_job(self, draft_id: int) -> int:
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
                (draft_id, vault_id, self.OWNER_ID),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_current_revision(self, draft_id: int, *, job_id: int, content_md: str,
                               fact_status: str = "passed") -> int:
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
                "VALUES (?, ?, ?, 'pipeline', ?, ?, ?, 1, ?)",
                (draft_id, job_id, next_no, content_md,
                 sha256_text(content_md), fact_status, self.OWNER_ID),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_fact_stage(self, job_id: int, *, candidate_sha256: str) -> None:
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

    def _set_draft_status(self, draft_id: int, status: str) -> None:
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_ready_eligible_draft(self, content_md="# Body\n\nSome article text."):
        """A draft with exactly one clean, Fact-current, needs_review revision
        the REAL Ready route accepts (copied from test_draft_compile_routes.py)."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        self._mark_input_ready(upload.json()["input"]["id"])
        job_id = self._seed_completed_job(draft_id)
        revision_id = self._seed_current_revision(
            draft_id, job_id=job_id, content_md=content_md, fact_status="passed"
        )
        self._seed_fact_stage(job_id, candidate_sha256=sha256_text(content_md))
        self._set_draft_status(draft_id, "needs_review")
        return draft_id, job_id, revision_id

    def _mark_ready(self, draft_id, revision_id):
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/ready",
            json={"lock_version": self._lock_version(draft_id),
                  "acknowledge_source_only": False},
            headers=self._owner_headers(),
        )

    def _insert_open_blocker_finding(self, draft_id, *, revision_id, job_id,
                                     message="unsupported claim") -> int:
        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            return store.insert_finding(
                draft_id=draft_id, stage="fact", rule_id="unsupported-claim",
                rule_version="1", category="factuality", severity="blocker",
                message=message, revision_id=revision_id, job_id=job_id,
                waivable=False,
            )
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_row(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT status, ready_revision_id, ready_by, ready_at FROM drafts "
                "WHERE id = ?",
                (draft_id,),
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)

    def _snapshot_draft_room_files(self) -> list:
        root = Path(settings.data_dir) / "draft-room"
        if not root.exists():
            return []
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ── AC13 (DRAFT-020): revision-scoped blockers ──────────────────────────────


class TestAC13RevisionScopedBlockers(Issue516AcceptanceBase):
    def test_ac13_revision_scoped_blockers(self):
        """A corrected current revision must be able to reach Ready while an
        open non-waivable blocker attached to an OLDER revision remains open
        in history; summary blocker counts must reflect only findings
        applicable to the current revision; a blocker on the CURRENT revision
        must still prevent Ready."""
        ac = "AC13"
        passed = False
        try:
            # Draft A: rev1 raised a non-waivable blocker; rev2 is the clean,
            # Fact-current corrected current revision.
            draft_a, job_a1, rev_a1 = self._seed_ready_eligible_draft(
                content_md="# v1\n\nOriginal body with an unsupported claim."
            )
            job_a2 = self._seed_completed_job(draft_a)
            rev_a2_content = "# v2\n\nCorrected body without the claim."
            rev_a2 = self._seed_current_revision(
                draft_a, job_id=job_a2, content_md=rev_a2_content
            )
            self._seed_fact_stage(job_a2, candidate_sha256=sha256_text(rev_a2_content))
            finding_id = self._insert_open_blocker_finding(
                draft_a, revision_id=rev_a1, job_id=job_a1,
                message="v1 claimed something unsupported",
            )

            # Draft B (control state): the open non-waivable blocker is
            # attached to the CURRENT revision itself.
            draft_b, job_b, rev_b = self._seed_ready_eligible_draft()
            self._insert_open_blocker_finding(
                draft_b, revision_id=rev_b, job_id=job_b
            )

            def check_a_old_revision_blocker_does_not_block_ready():
                resp = self._mark_ready(draft_a, rev_a2)
                self.assertEqual(
                    resp.status_code, 200,
                    "an open non-waivable blocker on an OLDER revision must not "
                    f"block the corrected current revision's Ready: {resp.text}",
                )
                self.assertEqual(
                    resp.json()["status"], "ready",
                    f"draft must be ready after marking rev {rev_a2}: {resp.text}",
                )

            def check_b_current_revision_blocker_still_blocks():
                resp = self._mark_ready(draft_b, rev_b)
                self.assertEqual(
                    resp.status_code, 409,
                    "a non-waivable open blocker on the CURRENT revision must "
                    f"still block Ready: {resp.text}",
                )
                self.assertEqual(resp.json()["code"], "non_waivable_blocker", resp.text)

            def check_c_counts_reflect_only_current_revision():
                detail = self.client.get(
                    f"/api/draft-room/drafts/{draft_a}", headers=self._owner_headers()
                ).json()
                self.assertEqual(
                    detail["summary"]["open_blocker_count"], 0,
                    "summary open_blocker_count must count only blockers "
                    "applicable to the current revision (the open blocker sits "
                    f"on old revision {rev_a1}): {detail['summary']}",
                )
                self.assertEqual(
                    detail["finding_counts_by_severity"].get("blocker", 0), 0,
                    "detail finding_counts_by_severity['blocker'] must count "
                    "only current-revision blockers: "
                    f"{detail['finding_counts_by_severity']}",
                )

            def check_history_old_finding_remains_open():
                resp = self.client.get(
                    f"/api/draft-room/drafts/{draft_a}/findings",
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                items = resp.json()["items"]
                matches = [f for f in items if f["id"] == finding_id]
                self.assertEqual(len(matches), 1, f"finding {finding_id} vanished: {items}")
                self.assertEqual(
                    matches[0]["status"], "open",
                    "the historical finding must remain open in history",
                )
                self.assertEqual(
                    matches[0]["revision_id"], rev_a1,
                    "the historical finding must still reference the old revision",
                )

            self._run_checks(ac, [
                ("a: old-revision blocker does not block Ready", check_a_old_revision_blocker_does_not_block_ready),
                ("b: current-revision blocker still blocks Ready [control]", check_b_current_revision_blocker_still_blocks),
                ("c: open_blocker_count counts only current-applicable blockers", check_c_counts_reflect_only_current_revision),
                ("guard: old finding remains open in history", check_history_old_finding_remains_open),
            ])
            passed = True
        finally:
            print(f"{ac} CHECK: {'PASS' if passed else 'FAIL'}")


# ── AC14 (DRAFT-022): approval invalidation on material metadata change ─────


class TestAC14MaterialChangeInvalidatesReady(Issue516AcceptanceBase):
    def test_ac14_material_change_invalidates_ready(self):
        """After a revision is Ready, changing a MATERIAL brief or
        input-metadata value must move the draft to needs_review, clear the
        active Ready pointer, and flip export classification to not_ready.
        Controls: an update with UNCHANGED values keeps Ready; a title-only
        change keeps Ready.

        Path note: Ready state is reached through the REAL mark-ready route
        over a seeded Fact-current revision (no direct ready-column seeding).
        """
        ac = "AC14"
        passed = False
        try:
            # Draft 1 carries the Ready approval used by the two controls and
            # the material-brief change; draft 2 (with an input) carries the
            # material input-metadata change.
            draft1, _job1, rev1 = self._seed_ready_eligible_draft()
            draft2, _job2, rev2 = self._seed_ready_eligible_draft()
            input2_id = self.client.get(
                f"/api/draft-room/drafts/{draft2}", headers=self._owner_headers()
            ).json()["inputs"][0]["id"]

            def check_baseline_ready_via_real_route():
                resp = self._mark_ready(draft1, rev1)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["status"], "ready", resp.text)
                resp2 = self._mark_ready(draft2, rev2)
                self.assertEqual(resp2.status_code, 200, resp2.text)
                self.assertEqual(resp2.json()["status"], "ready", resp2.text)

            def check_title_only_change_keeps_ready():
                resp = self.client.patch(
                    f"/api/draft-room/drafts/{draft1}",
                    json={"lock_version": self._lock_version(draft1),
                          "title": "Retitled But Ready"},
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(
                    resp.json()["status"], "ready",
                    f"title is non-material; Ready must survive: {resp.text}",
                )
                export = self.client.post(
                    f"/api/draft-room/drafts/{draft1}/revisions/{rev1}/export",
                    headers=self._owner_headers(),
                )
                self.assertEqual(
                    export.headers.get("X-Draft-Approval-Status"), "ready",
                    "title-only change must keep the ready export classification",
                )

            def check_unchanged_brief_keeps_ready():
                brief = self.client.get(
                    f"/api/draft-room/drafts/{draft1}", headers=self._owner_headers()
                ).json()["brief"]
                resp = self.client.patch(
                    f"/api/draft-room/drafts/{draft1}",
                    json={"lock_version": self._lock_version(draft1), "brief": brief},
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(
                    resp.json()["status"], "ready",
                    f"an update with UNCHANGED brief values must keep Ready: {resp.text}",
                )

            def check_material_brief_change_invalidates():
                changed = _default_brief(must_include=["the corrected figure"])
                resp = self.client.patch(
                    f"/api/draft-room/drafts/{draft1}",
                    json={"lock_version": self._lock_version(draft1), "brief": changed},
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                row = self._draft_row(draft1)
                self.assertEqual(
                    row["status"], "needs_review",
                    "a MATERIAL brief change (must_include) must move a Ready "
                    f"draft back to needs_review (got {row['status']!r})",
                )
                self.assertIsNone(
                    row["ready_revision_id"],
                    "the active Ready pointer must be cleared on a material "
                    f"brief change (got {row['ready_revision_id']!r})",
                )
                export = self.client.post(
                    f"/api/draft-room/drafts/{draft1}/revisions/{rev1}/export",
                    headers=self._owner_headers(),
                )
                self.assertEqual(
                    export.headers.get("X-Draft-Approval-Status"), "not_ready",
                    "export classification must become not_ready after a "
                    "material brief change",
                )

            def check_material_input_metadata_change_invalidates():
                resp = self.client.patch(
                    f"/api/draft-room/drafts/{draft2}/inputs/{input2_id}",
                    json={"role": "reference"},
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                row = self._draft_row(draft2)
                self.assertEqual(
                    row["status"], "needs_review",
                    "a MATERIAL input-metadata change (role) must move a Ready "
                    f"draft back to needs_review (got {row['status']!r})",
                )
                self.assertIsNone(
                    row["ready_revision_id"],
                    "the active Ready pointer must be cleared on a material "
                    f"input-metadata change (got {row['ready_revision_id']!r})",
                )
                export = self.client.post(
                    f"/api/draft-room/drafts/{draft2}/revisions/{rev2}/export",
                    headers=self._owner_headers(),
                )
                self.assertEqual(
                    export.headers.get("X-Draft-Approval-Status"), "not_ready",
                    "export classification must become not_ready after a "
                    "material input-metadata change",
                )

            self._run_checks(ac, [
                ("baseline: real mark-ready route reaches ready [guard]", check_baseline_ready_via_real_route),
                ("title-only change keeps Ready [control]", check_title_only_change_keeps_ready),
                ("unchanged-brief update keeps Ready [control]", check_unchanged_brief_keeps_ready),
                ("material brief change (must_include) invalidates Ready", check_material_brief_change_invalidates),
                ("material input-metadata change (role) invalidates Ready", check_material_input_metadata_change_invalidates),
            ])
            passed = True
        finally:
            print(f"{ac} CHECK: {'PASS' if passed else 'FAIL'}")


# ── AC15 (DRAFT-023): promotion compensation removes the enqueued job ───────


class _EnqueueThenCancelProcessor:
    """Mimics ``BackgroundProcessor.enqueue`` (the final try-step in
    ``draft_promotion._promote``): the job is inserted into the queue (the
    processor's own durable side effect), and the cancellation is delivered
    strictly AFTER that insertion."""

    def __init__(self) -> None:
        # ``enqueued`` is append-only history: it proves a job was handed to
        # the processor even after compensation clears ``jobs`` (the queue of
        # not-yet-started work), mirroring a real queue whose items get
        # cancelled in place rather than erased from history.
        self.enqueued: list[dict] = []
        self.jobs: list[dict] = []

    async def enqueue(self, **kwargs) -> None:
        self.enqueued.append(dict(kwargs))
        self.jobs.append(dict(kwargs))
        raise asyncio.CancelledError()

    def cancel_pending_jobs(self, **match) -> None:
        """Mirror of ``BackgroundProcessor.cancel_pending_jobs``: drop the
        recorded not-yet-started jobs matching ``match`` (the production API
        the promotion compensation path invokes)."""
        self.jobs = [
            job for job in self.jobs
            if any(job.get(key) != value for key, value in match.items())
        ]


class TestAC15PromotionCancellationCompensatesIngestionJob(Issue516AcceptanceBase):
    def test_ac15_promotion_cancellation_compensates_ingestion_job(self):
        """If cancellation lands AFTER the files row commits and AFTER the
        ingestion enqueue, compensation must also remove the orphaned
        ingestion job so no pending document/provenance/ingestion remains.

        Driven against ``promote_input`` directly with ``asyncio.run`` (the
        established pattern from ``test_draft_promote.py``: TestClient cannot
        deliver genuine mid-request task cancellation).
        """
        ac = "AC15"
        passed = False
        try:
            draft_id = self._create_draft(title="Cancel After Enqueue").json()["id"]
            upload = self._upload_input(draft_id, content=b"cancel after enqueue")
            self.assertEqual(upload.status_code, 202, upload.text)
            input_id = upload.json()["input"]["id"]
            self._mark_input_ready(input_id)

            conn = self._connection_pool.get_connection()
            try:
                store = DraftStore(conn)
                draft = store.get_draft(draft_id, self.OWNER_ID)
                input_record = store.get_input(
                    draft_id=draft_id, owner_id=self.OWNER_ID, input_id=input_id
                )
            finally:
                self._connection_pool.release_connection(conn)

            storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")
            processor = _EnqueueThenCancelProcessor()

            async def _run():
                return await promote_input(
                    storage=storage,
                    db_pool=self._connection_pool,
                    background_processor=processor,
                    draft_id=draft_id,
                    vault_id=draft.vault_id,
                    title="Cancelled After Enqueue",
                    promoted_by=self.OWNER_ID,
                    input_record=input_record,
                )

            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(_run())

            def check_precondition_job_was_enqueued_before_cancellation():
                self.assertEqual(
                    len(processor.enqueued), 1,
                    "precondition: enqueue must have inserted its job before "
                    "the cancellation was delivered (enqueue history)",
                )
                self.assertEqual(
                    processor.enqueued[0].get("source"), "draft_room_promote",
                    f"unexpected enqueued job payload: {processor.enqueued}",
                )

            def check_no_files_row():
                conn2 = self._connection_pool.get_connection()
                try:
                    count = conn2.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                finally:
                    self._connection_pool.release_connection(conn2)
                self.assertEqual(count, 0, "compensation must remove the files row")

            def check_no_promotion_row():
                conn2 = self._connection_pool.get_connection()
                try:
                    count = conn2.execute(
                        "SELECT COUNT(*) FROM draft_promotions WHERE draft_id = ?",
                        (draft_id,),
                    ).fetchone()[0]
                finally:
                    self._connection_pool.release_connection(conn2)
                self.assertEqual(
                    count, 0, "compensation must remove the draft_promotions row"
                )

            def check_no_staged_bytes():
                uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
                self.assertEqual(
                    list(uploads_dir.glob("*")), [],
                    "compensation must remove the copied bytes",
                )

            def check_no_orphaned_ingestion_job():
                self.assertEqual(
                    processor.jobs, [],
                    "compensation must also remove the orphaned ingestion job "
                    "enqueued for the now-deleted file, so no pending "
                    "ingestion remains: "
                    f"{processor.jobs}",
                )

            self._run_checks(ac, [
                ("precondition: job was enqueued before cancellation", check_precondition_job_was_enqueued_before_cancellation),
                ("guard: no files row remains", check_no_files_row),
                ("guard: no promotion row remains", check_no_promotion_row),
                ("guard: no staged bytes remain", check_no_staged_bytes),
                ("no orphaned ingestion job remains for the deleted file", check_no_orphaned_ingestion_job),
            ])
            passed = True
        finally:
            print(f"{ac} CHECK: {'PASS' if passed else 'FAIL'}")


# ── AC16 (DRAFT-024): upload scheduling durability ──────────────────────────


class TestAC16UploadSchedulingDurability(Issue516AcceptanceBase):
    def test_ac16_upload_enqueue_infra_error_is_rollback_durable(self):
        """A raw sqlite3 infrastructure error from
        ``DraftStore.enqueue_parse_job`` during upload must not strand the
        input: no input row survives without a job, a retry of the same
        content succeeds without a restart, and the original error still
        surfaces as a non-200 response. A ``DraftStoreError``-based conflict
        keeps today's mapped HTTP error and compensation (control).

        Uses a ``raise_server_exceptions=False`` client (established pattern,
        ``test_eval_error_sanitization.py``) so the untranslated
        ``sqlite3.OperationalError`` surfaces as a 500 response instead of
        being re-raised out of the TestClient call.
        """
        ac = "AC16"
        passed = False
        try:
            client = TestClient(app, raise_server_exceptions=False)
            # Same fingerprint binding as the primary client: auth tokens are
            # minted against an empty user-agent.
            client.headers["user-agent"] = ""
            draft_id = self._create_draft(title="Durable Upload").json()["id"]
            control_content = b"domain conflict control bytes"
            infra_content = b"infrastructure failure bytes"

            def check_domain_conflict_maps_and_compensates():
                with patch.object(
                    DraftStore,
                    "enqueue_parse_job",
                    side_effect=DraftConflictError(
                        "a parse job is already active for this input"
                    ),
                ):
                    resp = self._post_input(
                        client, draft_id, content=control_content, filename="control.txt"
                    )
                self.assertEqual(
                    resp.status_code, 409,
                    f"a DraftStoreError-based conflict must keep its mapped HTTP "
                    f"error: {resp.status_code} {resp.text}",
                )
                self.assertEqual(resp.json().get("code"), "conflict", resp.text)
                self.assertEqual(
                    self._stranded_input_count(draft_id), 0,
                    "domain-error compensation must leave no stranded input row",
                )

            def check_retry_after_domain_conflict_succeeds():
                resp = self._post_input(
                    self.client, draft_id, content=control_content, filename="control.txt"
                )
                self.assertEqual(
                    resp.status_code, 202,
                    f"retry after a compensated domain error must succeed: {resp.text}",
                )

            def check_infra_error_rolls_back_upload():
                files_before = self._snapshot_draft_room_files()
                with patch.object(
                    DraftStore,
                    "enqueue_parse_job",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    resp = self._post_input(
                        client, draft_id, content=infra_content, filename="infra.txt"
                    )
                self.assertGreaterEqual(
                    resp.status_code, 400,
                    f"the original infrastructure error must still surface as a "
                    f"non-200 response (got {resp.status_code})",
                )
                # The discriminating assertion: after the failed request, no
                # input row may remain without a job (the failed upload must
                # roll back cleanly, leaving no stranded input).
                self.assertEqual(
                    self._stranded_input_count(draft_id), 0,
                    "an infrastructure error from enqueue_parse_job must not "
                    "strand an input row without a parse job; the failed upload "
                    "must roll back cleanly",
                )
                files_after = self._snapshot_draft_room_files()
                self.assertEqual(
                    files_after, files_before,
                    "the rolled-back upload must leave no stored bytes behind",
                )

            def check_retry_after_infra_error_succeeds():
                resp = self._post_input(
                    self.client, draft_id, content=infra_content, filename="infra.txt"
                )
                self.assertEqual(
                    resp.status_code, 202,
                    f"a RETRY upload of the same content must succeed without a "
                    f"process restart after the infrastructure error: {resp.text}",
                )
                self.assertEqual(
                    self._stranded_input_count(draft_id), 0,
                    "the retried upload must leave every input with a parse job",
                )

            self._run_checks(ac, [
                ("control: DraftStoreError conflict maps to HTTP error and compensates", check_domain_conflict_maps_and_compensates),
                ("control: retry after domain conflict succeeds", check_retry_after_domain_conflict_succeeds),
                ("infra error surfaces non-200 and rolls the input back", check_infra_error_rolls_back_upload),
                ("retry after infra error succeeds without restart", check_retry_after_infra_error_succeeds),
            ])
            passed = True
        finally:
            print(f"{ac} CHECK: {'PASS' if passed else 'FAIL'}")

    def _stranded_input_count(self, draft_id) -> int:
        """Inputs of this draft with no parse job row at all (the exact
        stranding the AC forbids: a committed input nothing will ever parse)."""
        conn = self._connection_pool.get_connection()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM draft_inputs i WHERE i.draft_id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM draft_jobs j "
                    "WHERE j.input_id = i.id AND j.job_type = 'parse_input')",
                    (draft_id,),
                ).fetchone()[0]
            )
        finally:
            self._connection_pool.release_connection(conn)


if __name__ == "__main__":
    unittest.main()
