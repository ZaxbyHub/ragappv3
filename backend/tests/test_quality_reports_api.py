"""Route tests for the quality-report API (issue #237, PRODUCT-ENH-12).

Drives the registered endpoints through TestClient against a seeded
temporary SQLite database; the CSRF dependency is overridden the same way
the acceptance check does (the security dependency itself is replaced, so
the conftest bypass classification is irrelevant here).
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
from app.main import app
from app.models.database import init_db, run_migrations
from app.security import csrf_protect

ADMIN = {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "is_active": True,
    "must_change_password": 0,
}
MEMBER = {
    "id": 2,
    "username": "member",
    "role": "member",
    "is_active": True,
    "must_change_password": 0,
}


class QualityReportsApiTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        db_path = str(Path(self._tmpdir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")

        vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('qr', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'QR', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid
        self.user_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, seq,"
            " status, created_at) VALUES (?, 'user', 'What is the project"
            " deadline?', 'turn-1', 1, 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        ).lastrowid
        self.assistant_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources,"
            " turn_id, seq, status, created_at) VALUES"
            " (?, 'assistant', 'The deadline is October 15 [S1].', ?, 'turn-1',"
            " 2, 'done', CURRENT_TIMESTAMP)",
            (
                self.session_id,
                json.dumps(
                    [
                        {"label": "S1", "file_id": 11, "file_hash": "sha-s1-001"},
                        {"label": "S2", "file_id": 12, "file_hash": "sha-s1-001"},
                    ]
                ),
            ),
        ).lastrowid
        # An assistant message with no user-message turn sibling (orphan turn).
        self.orphan_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, seq,"
            " status, created_at) VALUES (?, 'assistant', 'orphan', 'turn-x', 3,"
            " 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        ).lastrowid
        # A message with malformed sources JSON.
        self.bad_sources_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources,"
            " turn_id, seq, status, created_at) VALUES"
            " (?, 'assistant', 'bad sources', ?, 'turn-bad', 4, 'done',"
            " CURRENT_TIMESTAMP)",
            (self.session_id, "{not json"),
        ).lastrowid
        self.conn.commit()

        self.client = TestClient(app, raise_server_exceptions=False)

        async def allow(user, resource_type, resource_id, action):
            return True

        async def deny(user, resource_type, resource_id, action):
            return False

        self._allow = allow
        self._deny = deny
        # addCleanup-based teardown: overrides and the module cache are
        # removed even when setUp itself fails partway (PRR-024d), and the
        # module-level _release_id_cache is cleared so no stubbed release id
        # can leak across tests (PRR-005).
        from app.api.routes import quality_reports as _qr

        _qr._release_id_cache.clear()
        self.addCleanup(_qr._release_id_cache.clear)
        self.addCleanup(self.conn.close)
        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_current_active_user] = lambda: ADMIN
        app.dependency_overrides[get_evaluate_policy] = lambda: allow
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        for dep in (get_db, get_current_active_user, get_evaluate_policy, csrf_protect):
            self.addCleanup(app.dependency_overrides.pop, dep, None)

    def _report(self, message_id=None, category="incorrect_answer", note="wrong date"):
        return {
            "session_id": self.session_id,
            "message_id": message_id or self.assistant_msg_id,
            "category": category,
            "note": note,
        }

    # -- submit ----------------------------------------------------------

    def test_submit_report_happy_path_with_provenance(self):
        response = self.client.post("/api/quality/reports", json=self._report())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["category"], "incorrect_answer")
        self.assertEqual(body["turn_id"], "turn-1")
        self.assertEqual(body["seq"], 2)
        self.assertTrue(body["provenance"]["config_ref"])
        self.assertEqual(
            body["provenance"]["source_file_hashes"], ["sha-s1-001"]  # deduped
        )

    def test_submit_report_rejects_unknown_category(self):
        response = self.client.post(
            "/api/quality/reports", json=self._report(category="wrong_category")
        )
        self.assertEqual(response.status_code, 422)

    def test_submit_report_unknown_session_404(self):
        payload = self._report()
        payload["session_id"] = 99999
        response = self.client.post("/api/quality/reports", json=payload)
        self.assertEqual(response.status_code, 404)

    def test_submit_report_message_not_in_session_404(self):
        response = self.client.post(
            "/api/quality/reports", json=self._report(message_id=99999)
        )
        self.assertEqual(response.status_code, 404)

    def test_submit_report_no_vault_write_403(self):
        app.dependency_overrides[get_evaluate_policy] = lambda: self._deny
        try:
            response = self.client.post("/api/quality/reports", json=self._report())
            self.assertEqual(response.status_code, 403)
        finally:
            app.dependency_overrides[get_evaluate_policy] = lambda: self._allow

    def test_submit_report_cross_user_non_admin_403(self):
        app.dependency_overrides[get_current_active_user] = lambda: MEMBER
        try:
            response = self.client.post("/api/quality/reports", json=self._report())
            self.assertEqual(response.status_code, 403)
        finally:
            app.dependency_overrides[get_current_active_user] = lambda: ADMIN

    def test_submit_report_malformed_sources_yields_empty_hashes(self):
        response = self.client.post(
            "/api/quality/reports",
            json=self._report(message_id=self.bad_sources_msg_id),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["provenance"]["source_file_hashes"], []
        )

    def test_list_reports_returns_session_reports(self):
        self.client.post("/api/quality/reports", json=self._report())
        response = self.client.get(
            "/api/quality/reports", params={"session_id": self.session_id}
        )
        self.assertEqual(response.status_code, 200)
        reports = response.json()["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["category"], "incorrect_answer")

    # -- convert ---------------------------------------------------------

    def _submit_and_convert(self, expected="The deadline is October 15", message_id=None):
        report = self.client.post(
            "/api/quality/reports", json=self._report(message_id=message_id)
        ).json()
        return self.client.post(
            f"/api/quality/reports/{report['id']}/convert",
            json={"expected_outcome": expected},
        )

    def test_convert_report_happy_path(self):
        response = self._submit_and_convert()
        self.assertEqual(response.status_code, 200)
        case = response.json()
        self.assertEqual(case["query"], "What is the project deadline?")
        self.assertEqual(case["expected_outcome"], "The deadline is October 15")
        self.assertEqual(case["provenance"]["turn_id"], "turn-1")
        self.assertEqual(
            case["provenance"]["source_file_hashes"], ["sha-s1-001"]
        )

    def test_convert_unknown_report_404(self):
        response = self.client.post(
            "/api/quality/reports/99999/convert",
            json={"expected_outcome": "x"},
        )
        self.assertEqual(response.status_code, 404)

    def test_convert_orphan_turn_404(self):
        response = self._submit_and_convert(message_id=self.orphan_msg_id)
        self.assertEqual(response.status_code, 404)

    def test_convert_rejects_empty_expected_outcome(self):
        report = self.client.post(
            "/api/quality/reports", json=self._report()
        ).json()
        response = self.client.post(
            f"/api/quality/reports/{report['id']}/convert",
            json={"expected_outcome": ""},
        )
        self.assertEqual(response.status_code, 422)

    # -- compare ---------------------------------------------------------

    def _case_id(self):
        response = self._submit_and_convert()
        return response.json()["id"]

    def test_compare_before_after_with_pinned_deltas(self):
        case_id = self._case_id()
        body = {
            "before": {
                "answer": "I could not find the deadline.",
                "cited_source_labels": ["S1"],
                "retrieved_source_labels": ["S1"],
            },
            "after": {
                "answer": "The deadline is October 15.",
                "cited_source_labels": ["S1", "S2"],
                "retrieved_source_labels": ["S1"],
            },
        }
        response = self.client.post(
            f"/api/quality/eval-cases/{case_id}/compare", json=body
        )
        self.assertEqual(response.status_code, 200)
        metrics = response.json()["metrics"]
        self.assertAlmostEqual(metrics["fact_coverage"]["before"], 0.0)
        self.assertAlmostEqual(metrics["fact_coverage"]["after"], 1.0)
        self.assertAlmostEqual(metrics["fact_coverage"]["delta"], 1.0)
        self.assertAlmostEqual(metrics["citation_validity"]["before"], 1.0)
        self.assertAlmostEqual(metrics["citation_validity"]["after"], 0.5)
        self.assertAlmostEqual(metrics["citation_validity"]["delta"], -0.5)

    def test_compare_unknown_case_404(self):
        response = self.client.post(
            "/api/quality/eval-cases/99999/compare",
            json={"before": {"answer": ""}, "after": {"answer": ""}},
        )
        self.assertEqual(response.status_code, 404)

    # -- authz negatives (PRR-004): require_admin_role enforcement --------

    def test_convert_member_403(self):
        report = self.client.post(
            "/api/quality/reports", json=self._report()
        ).json()
        app.dependency_overrides[get_current_active_user] = lambda: MEMBER
        try:
            response = self.client.post(
                f"/api/quality/reports/{report['id']}/convert",
                json={"expected_outcome": "x"},
            )
        finally:
            app.dependency_overrides[get_current_active_user] = lambda: ADMIN
        self.assertEqual(response.status_code, 403)

    def test_compare_member_403(self):
        case_id = self._case_id()
        app.dependency_overrides[get_current_active_user] = lambda: MEMBER
        try:
            response = self.client.post(
                f"/api/quality/eval-cases/{case_id}/compare",
                json={"before": {"answer": ""}, "after": {"answer": ""}},
            )
        finally:
            app.dependency_overrides[get_current_active_user] = lambda: ADMIN
        self.assertEqual(response.status_code, 403)

    # -- list negatives (PRR-024b) ----------------------------------------

    def test_list_reports_missing_session_id_422(self):
        response = self.client.get("/api/quality/reports")
        self.assertEqual(response.status_code, 422)

    # -- duplicate submission (PRR-024a): insert-only contract -------------

    def test_duplicate_submit_creates_independent_reports(self):
        first = self.client.post("/api/quality/reports", json=self._report())
        second = self.client.post("/api/quality/reports", json=self._report())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        # Insert-only: no dedup constraint — each submission is its own row.
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        listing = self.client.get(
            "/api/quality/reports", params={"session_id": self.session_id}
        ).json()["reports"]
        self.assertEqual(len(listing), 2)


if __name__ == "__main__":
    unittest.main()
