"""Guardrails for the collated #1153 + #800 fixes (issue-tracer Phase 4).

Four guardrail families the frozen checks deliberately leave to
implementation time (disclosed in 05-fix-plan.md v3 item 9):
1. Stream-auth authorization ordering: the durable session's stored vault
   resolves BEFORE requested-vault authorization (F5c) — clamping, null-body
   resolution, and memory capability derived from the stored vault.
2. Legacy idempotency migration convergence: duplicate reports WITH existing
   eval cases converge to canonical rows, foreign keys preserved, indexes
   created, rerun-safe (F8c/F9c).
3. Accessible-vaults indexed_file_count SQL semantics: files.status='indexed'
   only — pending/failed/partial uploads do not count (F4c).
4. Batch-route completion fence: a completed chat_messages row is never
   downgraded by a late reconciled write (F7c, atomic WHERE guard).
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.main import app
from app.models.database import init_db, run_migrations
from app.security import csrf_protect

ADMIN_USER = {"id": 1, "role": "admin", "username": "admin"}
MEMBER_USER = {"id": 2, "role": "member", "username": "member"}

#: init_db seeds a default vault (usually id 1) — never assume the table is
#: empty; high explicit ids keep fixtures collision-free (lesson 8fae4eb4).
V1, V2, V3 = 101, 102, 103
SESSION_7 = 701
SESSION_5 = 501


def _seed_user(conn, user_id, role, name):
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, hashed_password, role)"
        " VALUES (?, ?, 'x', ?)",
        (user_id, name, role),
    )


class StreamAuthOrderingTest(unittest.TestCase):
    """F5c: the stored session vault is authoritative for authorization."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self._tmpdir) / "app.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        _seed_user(self.conn, 1, "admin", "admin")
        _seed_user(self.conn, 2, "member", "member")
        # Vault 1 (member can read/write), Vault 2 (member can read only),
        # Vault 3 (member has NO access).
        for vid in (V1, V2, V3):
            self.conn.execute("INSERT INTO vaults (id, name) VALUES (?, ?)", (vid, f"V{vid}"))
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, 2, 'write')", (V1,))
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, 2, 'read')", (V2,))
        # Session 7 stored in vault 1 (member-writable).
        self.conn.execute(
            "INSERT INTO chat_sessions (id, vault_id, title) VALUES (?, ?, 's7')", (SESSION_7, V1))
        self.conn.commit()
        self.client = TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides[get_db] = lambda: self.conn
        self.addCleanup(app.dependency_overrides.pop, get_db, None)

    def _stream(self, body_user, vault_id, session_id=SESSION_7, expect_status=None):
        app.dependency_overrides[get_current_active_user] = lambda: body_user
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)

        def evaluate(conn, user, kind, resource_id, action, *a, **k):
            # Realistic policy: members act only via their vault_members rows.
            if user["role"] in ("admin", "superadmin"):
                return True
            row = conn.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (resource_id, user["id"]),
            ).fetchone()
            if row is None:
                return False
            level = row[0]
            order = {"read": 0, "write": 1, "admin": 2}
            return order.get(level, -1) >= order.get(action, 99)

        app.dependency_overrides[get_evaluate_policy] = lambda: evaluate
        self.addCleanup(app.dependency_overrides.pop, get_evaluate_policy, None)
        app.dependency_overrides[csrf_protect] = lambda: "t"
        self.addCleanup(app.dependency_overrides.pop, csrf_protect, None)
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "vault_id": vault_id,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        resp = self.client.post("/api/chat/stream", json=payload)
        if expect_status is not None:
            self.assertEqual(resp.status_code, expect_status, resp.text)
        return resp

    def test_mismatched_vault_clamped_to_stored_scope(self):
        # Member requests vault 2 (read-only) inside a session stored in
        # vault 1. The stored scope wins BEFORE authorization — the request
        # is authorized against vault 1 (member CAN write it), NOT rejected
        # through vault-2 semantics nor allowed to search vault 2.
        resp = self._stream(MEMBER_USER, vault_id=V2, session_id=SESSION_7)
        # 200 (SSE started) or 409/503 for model config are downstream
        # outcomes; the ordering contract is that authorization used vault 1
        # so the member was NOT 403'd for lacking write on vault 2.
        self.assertNotEqual(resp.status_code, 403, resp.text)
        self.assertNotIn("No read access", resp.text)

    def test_null_body_vault_resolves_to_stored_scope_for_member(self):
        # Non-admin + vault_id=None + existing session: previously the admin
        # 403 fired before the session was considered; now the stored scope
        # resolves first, so this is NOT an all-vaults rejection.
        resp = self._stream(MEMBER_USER, vault_id=None, session_id=SESSION_7)
        self.assertNotIn("Searching all vaults requires admin", resp.text)

    def test_inaccessible_requested_vault_on_session_clamped_not_403_read(self):
        # Member requests vault 3 (no access at all) but the session lives in
        # vault 1: the stored scope is authoritative, so the irrelevant
        # vault-3 read check must not reject the request.
        resp = self._stream(MEMBER_USER, vault_id=V3, session_id=SESSION_7)
        self.assertNotEqual(resp.status_code, 403, resp.text)


class QualityIdempotencyMigrationTest(unittest.TestCase):
    """F8c/F9c: legacy duplicate convergence with eval-case re-parenting."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _db(self):
        return str(Path(self._tmpdir) / "app.db")

    def test_duplicates_with_cases_converge_and_rerun_safe(self):
        db_path = self._db()
        init_db(db_path)
        run_migrations(db_path)
        # Converged schema ships the unique indexes (fresh-DB convergence);
        # simulate the LEGACY pre-index database by dropping them, then dirty
        # it with duplicates — the migration must re-converge and recreate.
        conn = sqlite3.connect(db_path)
        conn.execute("DROP INDEX IF EXISTS idx_quality_reports_message_category")
        conn.execute("DROP INDEX IF EXISTS idx_quality_eval_cases_report_unique")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO vaults (id, name) VALUES (501, 'mv')")
        conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (1, 501)")
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, turn_id,"
            " seq) VALUES (100, 1, 'assistant', 'a', 't1', 2)")
        conn.execute(
            "INSERT INTO quality_reports (id, session_id, message_id, category,"
            " turn_id, seq, config_ref) VALUES (10, 1, 100, 'cat', 't1', 2, 'r')")
        conn.execute(
            "INSERT INTO quality_reports (id, session_id, message_id, category,"
            " turn_id, seq, config_ref) VALUES (11, 1, 100, 'cat', 't1', 2, 'r')")
        conn.execute(
            "INSERT INTO quality_eval_cases (id, report_id, query, expected_outcome)"
            " VALUES (50, 10, 'q', 'e')")
        conn.execute(
            "INSERT INTO quality_eval_cases (id, report_id, query, expected_outcome)"
            " VALUES (51, 11, 'q', 'e')")
        conn.execute(
            "INSERT INTO quality_eval_cases (id, report_id, query, expected_outcome)"
            " VALUES (52, 10, 'q2', 'e2')")
        conn.commit()
        conn.close()
        # Run the migration (run_migrations includes the new step; rerun-safe).
        run_migrations(db_path)
        run_migrations(db_path)  # idempotent rerun
        conn = sqlite3.connect(db_path)
        reports = conn.execute(
            "SELECT id FROM quality_reports ORDER BY id").fetchall()
        cases = conn.execute(
            "SELECT id, report_id FROM quality_eval_cases ORDER BY id").fetchall()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name IN ('quality_reports','quality_eval_cases')"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(reports, [(10,)])  # canonical survives, dupe deleted
        # All cases re-parented to 10 and deduplicated: lowest id 50 survives.
        self.assertEqual(cases, [(50, 10)])
        self.assertIn("idx_quality_reports_message_category", indexes)
        self.assertIn("idx_quality_eval_cases_report_unique", indexes)

    def test_uniqueness_enforced_post_migration(self):
        db_path = self._db()
        init_db(db_path)
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO vaults (id, name) VALUES (501, 'mv')")
        conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (1, 501)")
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, turn_id,"
            " seq) VALUES (100, 1, 'assistant', 'a', 't1', 2)")
        conn.execute(
            "INSERT INTO quality_reports (session_id, message_id, category,"
            " turn_id, seq, config_ref) VALUES (1, 100, 'cat', 't1', 2, 'r')")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO quality_reports (session_id, message_id, category,"
                " turn_id, seq, config_ref) VALUES (1, 100, 'cat', 't1', 2, 'r')")
        conn.close()


class AccessibleVaultIndexedCountTest(unittest.TestCase):
    """F4c: indexed_file_count counts only files.status='indexed'."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self._tmpdir) / "app.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.addCleanup(self.conn.close)
        _seed_user(self.conn, 2, "member", "member")
        self.conn.execute("INSERT INTO vaults (id, name) VALUES (601, 'V1x')")
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission)"
            " VALUES (601, 2, 'write')")
        for i, status in enumerate(
            ("indexed", "indexed", "pending", "processing", "error", "partial")
        ):
            self.conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size,"
                " status) VALUES (601, ?, ?, 1, ?)",
                (f"/p/f{i}", f"f{i}", status),
            )
        self.conn.commit()
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: self.conn
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        app.dependency_overrides[get_current_active_user] = lambda: ADMIN_USER
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)
        app.dependency_overrides[get_evaluate_policy] = (
            lambda: (lambda *a, **k: True)
        )
        self.addCleanup(app.dependency_overrides.pop, get_evaluate_policy, None)

    def _fixture_vault(self):
        resp = self.client.get("/api/vaults/accessible")
        self.assertEqual(resp.status_code, 200, resp.text)
        # init_db seeds a default vault — select OUR fixture vault by id.
        return next(v for v in resp.json()["vaults"] if v["id"] == 601)

    def test_indexed_count_excludes_non_indexed_statuses(self):
        vault = self._fixture_vault()
        self.assertEqual(vault["file_count"], 6)
        self.assertEqual(vault["indexed_file_count"], 2)

    def test_zero_indexed_vault_reports_zero_not_file_count(self):
        self.conn.execute("UPDATE files SET status = 'pending'")
        self.conn.commit()
        vault = self._fixture_vault()
        self.assertEqual(vault["file_count"], 6)
        self.assertEqual(vault["indexed_file_count"], 0)


class BatchCompletionFenceTest(unittest.TestCase):
    """F7c: completed rows are never downgraded by a late reconciled write."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = str(Path(self._tmpdir) / "app.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.addCleanup(self.conn.close)
        _seed_user(self.conn, 1, "admin", "admin")
        self.conn.execute("INSERT INTO vaults (id, name) VALUES (701, 'V1f')")
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission)"
            " VALUES (701, 1, 'admin')")
        self.conn.execute(
            "INSERT INTO chat_sessions (id, vault_id, title)"
            " VALUES (501, 701, 's')")
        self.conn.commit()
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: self.conn
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        app.dependency_overrides[get_current_active_user] = lambda: ADMIN_USER
        self.addCleanup(app.dependency_overrides.pop, get_current_active_user, None)
        app.dependency_overrides[csrf_protect] = lambda: "t"
        self.addCleanup(app.dependency_overrides.pop, csrf_protect, None)
        app.dependency_overrides[get_rag_engine] = lambda: None
        self.addCleanup(app.dependency_overrides.pop, get_rag_engine, None)

    def _completed_row(self):
        return self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, seq,"
            " status) VALUES (501, 'assistant', 'FULL ANSWER', 'turn-f1', 2,"
            " 'complete')"
        ).lastrowid

    def test_late_interrupted_reconcile_cannot_downgrade_complete(self):
        row_id = self._completed_row()
        self.conn.commit()
        resp = self.client.post(
            "/api/chat/sessions/501/messages/batch",
            json={
                "messages": [
                    {
                        "role": "assistant",
                        "content": "PARTIAL MIRROR",
                        "turn_id": "turn-f1",
                        "status": "interrupted",
                    }
                ]
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        row = self.conn.execute(
            "SELECT content, status FROM chat_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row[0], "FULL ANSWER")
        self.assertEqual(row[1], "complete")

    def test_non_complete_rows_still_reconcile(self):
        row_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, seq,"
            " status) VALUES (501, 'assistant', 'old', 'turn-f2', 4, 'interrupted')"
        ).lastrowid
        self.conn.commit()
        resp = self.client.post(
            "/api/chat/sessions/501/messages/batch",
            json={
                "messages": [
                    {
                        "role": "assistant",
                        "content": "new mirror",
                        "turn_id": "turn-f2",
                        "status": "failed",
                    }
                ]
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        row = self.conn.execute(
            "SELECT content, status FROM chat_messages WHERE id = ?", (row_id,)
        ).fetchone()
        self.assertEqual(row[0], "new mirror")
        self.assertEqual(row[1], "failed")


if __name__ == "__main__":
    unittest.main()
