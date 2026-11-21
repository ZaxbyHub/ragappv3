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

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes.chat import ChatStreamRequest
from app.main import app
from app.models.database import (
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
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

    def test_converged_db_skips_convergence_scans(self):
        """RP-004: a second run on an already-converged DB early-returns without
        re-journaling the convergence (the indexes-existing fast path)."""
        db_path = self._db()
        init_db(db_path)
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        first = conn.execute(
            "SELECT COUNT(*) FROM migration_journal"
            " WHERE migration_name ="
            " 'migrate_add_quality_idempotency_indexes'"
        ).fetchone()[0]
        conn.close()
        run_migrations(db_path)  # converged → fast path
        conn = sqlite3.connect(db_path)
        second = conn.execute(
            "SELECT COUNT(*) FROM migration_journal"
            " WHERE migration_name ="
            " 'migrate_add_quality_idempotency_indexes'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(first, second, "converged restart must not re-run convergence")

    def test_differing_notes_are_merged_not_discarded(self):
        """F-03: a discarded duplicate report's note is merged into the
        canonical row, not silently deleted."""
        db_path = self._db()
        init_db(db_path)
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DROP INDEX IF EXISTS idx_quality_reports_message_category")
        conn.execute("DROP INDEX IF EXISTS idx_quality_eval_cases_report_unique")
        conn.execute("INSERT INTO vaults (id, name) VALUES (501, 'mv')")
        conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (1, 501)")
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, turn_id, seq)"
            " VALUES (100, 1, 'assistant', 'a', 't1', 2)"
        )
        conn.execute(
            "INSERT INTO quality_reports (id, session_id, message_id, category,"
            " turn_id, seq, config_ref, note) VALUES (10, 1, 100, 'cat', 't1', 2,"
            " 'r', 'first reviewer note')"
        )
        conn.execute(
            "INSERT INTO quality_reports (id, session_id, message_id, category,"
            " turn_id, seq, config_ref, note) VALUES (11, 1, 100, 'cat', 't1', 2,"
            " 'r', 'second reviewer note')"
        )
        conn.commit()
        conn.close()
        run_migrations(db_path)
        conn = sqlite3.connect(db_path)
        note = conn.execute(
            "SELECT note FROM quality_reports WHERE id = 10"
        ).fetchone()[0]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM quality_reports WHERE message_id = 100 AND category = 'cat'"
        ).fetchone()[0]
        conn.close()
        # Both distinct notes survive on the canonical (lowest-id) report.
        self.assertIn("first reviewer note", note)
        self.assertIn("second reviewer note", note)
        # The duplicate report row is gone; one canonical row remains.
        self.assertEqual(remaining, 1)
        # F-03 observability: the convergence is journaled, not silent.
        journal = sqlite3.connect(db_path)
        row = journal.execute(
            "SELECT phase, outcome FROM migration_journal"
            " WHERE migration_name ="
            " 'migrate_add_quality_idempotency_indexes'"
        ).fetchone()
        journal.close()
        self.assertIsNotNone(row, "migration outcome must be journaled")
        self.assertEqual(row[0], "succeeded")
        self.assertEqual(row[1], "ok")

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


class StreamAuthClampDirectTest(unittest.TestCase):
    """F-01 (#1153 AC5 backend clamp): get_stream_auth reaches the vault clamp.

    The HTTP-level StreamAuthOrderingTest legs assert only "not 403", but every
    leg returned 500 at ``app.state.rag_engine`` BEFORE ``get_stream_auth`` ran,
    so the stored-vault clamp was unproven on the backend. This drives
    ``get_stream_auth`` as a coroutine against a real pooled connection with the
    user-resolution seam patched, so the clamp and the policy check actually
    execute and their outcome is asserted.
    """

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
        for vid in (V1, V2, V3):
            self.conn.execute(
                "INSERT INTO vaults (id, name) VALUES (?, ?)", (vid, f"V{vid}")
            )
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) "
            "VALUES (?, 2, 'write')",
            (V1,),
        )
        self.conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) "
            "VALUES (?, 2, 'read')",
            (V2,),
        )
        # Session 7 stored in vault 1 (member-writable).
        self.conn.execute(
            "INSERT INTO chat_sessions (id, vault_id, title) VALUES (?, ?, 's7')",
            (SESSION_7, V1),
        )
        self.conn.commit()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)
        self.addCleanup(self.pool.close_all)
        app.state.db_pool = self.pool
        self.addCleanup(setattr, app.state, "db_pool", None)

    def _run_auth(self, body):
        from app.api.routes.chat import get_stream_auth

        async def _go():
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/chat/stream",
                "headers": [],
                "query_string": b"",
            }
            request = Request(scope)
            request.scope["app"] = app
            return await get_stream_auth(request, body)

        return asyncio.run(_go())

    def _patch_user(self, user):
        import app.api.routes.chat as chat_mod

        async def _resolve(conn, request, authorization, access_token):
            return user

        original = chat_mod._resolve_active_user
        chat_mod._resolve_active_user = _resolve
        self.addCleanup(setattr, chat_mod, "_resolve_active_user", original)

    def test_requested_inaccessible_vault_clamped_to_stored_scope(self):
        # Member's session lives in V1 (write); the request names V3 (no access).
        # The stored scope must win: clamp to V1, authorize against V1, succeed.
        self._patch_user(MEMBER_USER)
        body = ChatStreamRequest(
            messages=[{"role": "user", "content": "hi"}],
            vault_id=V3,
            session_id=SESSION_7,
        )
        user = self._run_auth(body)
        self.assertEqual(user["id"], MEMBER_USER["id"])
        # The clamp fired: the requested V3 was rewritten to the stored V1.
        self.assertEqual(body.vault_id, V1)

    def test_no_session_inaccessible_vault_is_rejected(self):
        # Control: with no session there is no clamp, so a member requesting an
        # inaccessible vault is 403. This is the behavior the positive leg only
        # escapes via the clamp — proving the clamp is load-bearing, not a hole.
        from fastapi import HTTPException

        self._patch_user(MEMBER_USER)
        body = ChatStreamRequest(
            messages=[{"role": "user", "content": "hi"}], vault_id=V3
        )
        with self.assertRaises(HTTPException) as ctx:
            self._run_auth(body)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
