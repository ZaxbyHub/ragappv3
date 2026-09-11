"""Issue #517 acceptance checks: route-level findings scale and export
disclosure.

Discriminating regression tests (expected RED at HEAD d9e3460):

* **AC11 (DRAFT-021)** — the findings listing scans only the newest
  ``_MAX_LIST_SCAN_ROWS`` (2000) rows in Python because
  ``DraftStore.list_findings`` has no SQL status/severity filters, so a
  ledger of 2050 findings reports a capped total, truncates the last page,
  loses filtered rows older than the scan window, and finding disposition
  404s on them (``_sync_get_finding`` scans too).
* **AC13 backend part** — export returns fact/approval/content headers but
  no unresolved-issues disclosure; the contract pinned here is an explicit
  ``X-Draft-Open-Blockers: <count>`` header on every export (plus the
  already-correct ``X-Draft-Content-Sha256`` match, asserted as the
  preserving half).

Also carries the **P2 preserving check**: chat-side
``validate_and_repair_citations`` with the default ``draft_count=0`` strips
``[D1]`` (the Draft Room registry must be opt-in so chat behavior is
unchanged).

Harness copied from ``test_draft_compile_routes.py`` (``CompileRouteTestBase``).
"""

import os
import shutil
import sys
import tempfile
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
from app.services.citation_validator import validate_and_repair_citations
from app.services.draft_store import DraftStore, sha256_text

TOTAL_FINDINGS = 2050  # strictly above _MAX_LIST_SCAN_ROWS (2000)
OLD_MESSAGE = "ZZXOLD-2050th"


class _PoolWithConnectionCM:
    """Copied verbatim from ``test_draft_compile_routes.py``."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self):
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


class Issue517RouteBase(unittest.TestCase):
    """Same fixture shape as ``CompileRouteTestBase``."""

    OWNER_ID = 1
    READ_VAULT_ID = 2

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._originals = {
            "jwt_secret_key": settings.jwt_secret_key,
            "users_enabled": settings.users_enabled,
            "data_dir": settings.data_dir,
            "draft_room_enabled": settings.draft_room_enabled,
            "ollama_chat_url": settings.ollama_chat_url,
            "instant_chat_url": settings.instant_chat_url,
            "draft_allowed_model_origins": settings.draft_allowed_model_origins,
        }
        self._original_allow_local_services = os.environ.get("ALLOW_LOCAL_SERVICES")

        settings.data_dir = __import__("pathlib").Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.draft_room_enabled = True
        settings.ollama_chat_url = "http://127.0.0.1:11434"
        settings.instant_chat_url = "http://127.0.0.1:11434"
        settings.draft_allowed_model_origins = ["http://127.0.0.1:11434"]
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"

        self._db_path = str(
            __import__("pathlib").Path(self._temp_dir) / "app.db"
        )

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
        app.state.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )

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
                "INSERT OR IGNORE INTO users (id, username, hashed_password, "
                "full_name, role, is_active) VALUES (1,'owner',?, 'Owner','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (2,'Read Vault','r')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, "
                "permission, granted_by) VALUES (2,1,'write',1)"
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

        for key, value in self._originals.items():
            setattr(settings, key, value)
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

    # -- helpers --

    def _headers(self, user_id=OWNER_ID, username="owner", role="member"):
        return {
            "Authorization": (
                "Bearer "
                + create_access_token(
                    user_id,
                    username,
                    role,
                    client_fingerprint=compute_client_fingerprint(""),
                )
            )
        }

    def _owner_headers(self):
        return self._headers(self.OWNER_ID, "owner")

    def _create_draft(self, *, title="Findings Draft"):
        resp = self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": self.READ_VAULT_ID,
                "title": title,
                "mode": "rewrite",
                "tier": "standard",
                "brief": _default_brief(),
            },
            headers=self._owner_headers(),
        )
        self.assertIn(resp.status_code, (200, 201), resp.text)
        return resp.json()["id"]

    def _upload_and_ready_input(self, draft_id):
        upload = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": "manuscript", "authority": "primary"},
            files={"file": ("m.txt", b"Hello world. This is the manuscript body.", "text/plain")},
            headers=self._owner_headers(),
        )
        self.assertIn(upload.status_code, (200, 201, 202), upload.text)
        input_id = upload.json()["input"]["id"]
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_inputs SET parse_status = 'ready', parsed_text = "
                "'Hello world. This is the manuscript body.', parsed_text_sha256 = ?, "
                "parsed_char_count = 42 WHERE id = ?",
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

    def _seed_completed_job(self, draft_id):
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

    def _seed_current_revision(self, draft_id, *, job_id, content_md,
                               fact_status="passed"):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_revisions SET is_current = 0 WHERE draft_id = ?",
                (draft_id,),
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
                (
                    draft_id, job_id, next_no, content_md,
                    sha256_text(content_md), fact_status, self.OWNER_ID,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self._connection_pool.release_connection(conn)

    def _set_draft_status(self, draft_id, status):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _seed_ready_eligible_draft(self, content_md="# Body\n\nSome article text."):
        draft_id = self._create_draft()
        self._upload_and_ready_input(draft_id)
        job_id = self._seed_completed_job(draft_id)
        revision_id = self._seed_current_revision(
            draft_id, job_id=job_id, content_md=content_md
        )
        self._set_draft_status(draft_id, "needs_review")
        return draft_id, job_id, revision_id

    def _lock_version(self, draft_id):
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        return resp.json()["summary"]["lock_version"]


# ── AC11 (DRAFT-021): findings listing and disposition at scale ──────────────


class TestFindingsListingAtScale(Issue517RouteBase):
    def setUp(self):
        super().setUp()
        self.draft_id = self._create_draft(title="Scale Draft")
        self._upload_and_ready_input(self.draft_id)
        self.job_id = self._seed_completed_job(self.draft_id)
        self.revision_id = self._seed_current_revision(
            self.draft_id,
            job_id=self.job_id,
            content_md="# Heading\n\nBody text for the scale fixture.",
        )
        self._set_draft_status(self.draft_id, "needs_review")

        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            # The ONE row that matches severity='warning' AND status='open' is
            # inserted FIRST (lowest id / oldest), so it sits beyond the
            # newest-2000 scan window once the bulk rows land on top of it.
            self.old_finding_id = store.insert_finding(
                draft_id=self.draft_id,
                stage="lint",
                rule_id="old-warning",
                rule_version="1",
                category="style",
                severity="warning",
                message=OLD_MESSAGE,
                revision_id=self.revision_id,
                job_id=self.job_id,
            )
            for i in range(TOTAL_FINDINGS - 1):
                store.insert_finding(
                    draft_id=self.draft_id,
                    stage="lint",
                    rule_id="bulk-info",
                    rule_version="1",
                    category="style",
                    severity="info",
                    message=f"bulk finding number {i}",
                    revision_id=self.revision_id,
                    job_id=self.job_id,
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM draft_findings WHERE draft_id = ?",
                (self.draft_id,),
            ).fetchone()[0]
            self.assertEqual(count, TOTAL_FINDINGS)
        finally:
            self._connection_pool.release_connection(conn)

    def test_total_is_exact_and_last_page_is_reachable(self):
        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings"
            "?per_page=100&page=1",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        print("AC11 CHECK: FAIL", flush=True)
        self.assertEqual(
            resp.json()["total"],
            TOTAL_FINDINGS,
            "the findings listing total must be the exact row count, not a "
            "scan-window cap",
        )

        last_page = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings"
            f"?per_page=100&page=21",
            headers=self._owner_headers(),
        )
        self.assertEqual(last_page.status_code, 200, last_page.text)
        self.assertTrue(
            last_page.json()["items"],
            "the last page (2050 rows at per_page=100 -> page 21) must be "
            "non-empty",
        )

    def test_filters_find_rows_older_than_the_scan_window(self):
        resp = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings"
            "?severity=warning&status=open&per_page=100",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        print("AC11 CHECK: FAIL", flush=True)
        body = resp.json()
        self.assertEqual(
            body["total"],
            1,
            "filtering severity=warning & status=open must find the one "
            "matching row even when it is older than the newest-2000 scan "
            "window",
        )
        self.assertEqual(
            [item["id"] for item in body["items"]],
            [self.old_finding_id],
            "the filtered listing must return the old warning row itself",
        )

    def test_disposition_reaches_findings_older_than_the_scan_window(self):
        lv = self._lock_version(self.draft_id)
        resp = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/"
            f"{self.old_finding_id}/disposition",
            json={
                "action": "dismiss",
                "base_revision_id": self.revision_id,
                "lock_version": lv,
                "note": "old but reachable",
            },
            headers=self._owner_headers(),
        )

        print("AC11 CHECK: FAIL", flush=True)
        self.assertEqual(
            resp.status_code,
            200,
            f"disposition must reach a finding older than the scan window "
            f"(got {resp.status_code}: {resp.text})",
        )
        self.assertEqual(resp.json()["finding"]["status"], "dismissed")


# ── AC13 (backend): export must disclose unresolved blockers ─────────────────


class TestExportDisclosesOpenBlockers(Issue517RouteBase):
    def test_export_headers_disclose_the_open_blocker_count(self):
        content_md = "# Body\n\nfact-checked text with one open blocker."
        draft_id, job_id, revision_id = self._seed_ready_eligible_draft(
            content_md=content_md
        )
        conn = self._connection_pool.get_connection()
        try:
            DraftStore(conn).insert_finding(
                draft_id=draft_id,
                stage="fact",
                rule_id="fact.claim_unsupported",
                rule_version="1",
                category="factuality",
                severity="blocker",
                message="an atomic claim is unsupported",
                revision_id=revision_id,
                job_id=job_id,
                waivable=False,
            )
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        print("AC13 CHECK: FAIL", flush=True)
        self.assertEqual(
            resp.headers.get("X-Draft-Open-Blockers"),
            "1",
            "export must disclose the count of unresolved blocker findings "
            "for the exported revision (contract: an explicit "
            "X-Draft-Open-Blockers header); headers were: "
            f"{dict(resp.headers)}",
        )

        # Preserving half — already satisfied today: the export names the
        # exact revision via its content hash.
        self.assertEqual(
            resp.headers.get("X-Draft-Content-Sha256"), sha256_text(content_md)
        )
        self.assertEqual(resp.headers.get("X-Draft-Fact-Status"), "passed")


# ── P2 (preserving): chat citation default strips [D1] ───────────────────────


class TestChatCitationDefaultStripsDraftLabels(unittest.TestCase):
    """P2 preserving check — must stay GREEN: with no Draft registry supplied
    (``draft_count=0`` default), a chat answer's ``[D1]`` is invalid and is
    stripped, exactly as before Draft Room support existed."""

    def test_default_draft_count_strips_d1_and_keeps_valid_s1(self):
        result = validate_and_repair_citations(
            "Chat answer citing [S1] and a draft label [D1] that must go.",
            source_count=1,
            memory_count=1,
        )
        self.assertIn("[S1]", result.repaired_content)
        self.assertNotIn("[D1]", result.repaired_content)
        self.assertIn("D1", result.invalid_draft_citations)
        self.assertNotIn("D1", result.valid_citations)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
