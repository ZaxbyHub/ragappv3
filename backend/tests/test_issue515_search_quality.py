"""Issue #515 acceptance checks — search quality (KMS readiness + unicode + excerpts).

ACs covered here:
- AC20 (API-004): /kms/search works without vector readiness; kms_enabled=False
  still 503s (PRESERVING negative).
- AC21 (SEARCH-002): unicode (CJK) queries find body matches across documents
  list search, the ranked endpoint, and KMS retrieval; ASCII hyphen queries and
  pure-punctuation queries stay sane (PRESERVING siblings).
- AC22 (SEARCH-001): match-centered excerpts for late body matches,
  sender-only metadata matches, and KMS evidence beyond 600 chars.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# test_documents_auth stubs the optional heavy deps and imports app.main.
from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_documents_auth import TestDocumentAuthBase  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_vector_store,
)
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.kms_retrieval import KMSRetrievalService  # noqa: E402
from app.services.kms_store import KMSStore  # noqa: E402

# Distinct unicode term so cross-test probes cannot collide.
_CJK_TERM = "\u6771\u4eac"  # 東京


class _KmsRouteBase(unittest.TestCase):
    """Route-level base for KMS endpoints with dependency overrides.

    The vector store is forced NOT ready through the same seam
    ``require_model_ready`` reads (the ``get_vector_store`` dependency).
    """

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        self._original_kms_enabled = settings.kms_enabled
        settings.kms_enabled = True

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        self._not_ready_vector_store = MagicMock()
        self._not_ready_vector_store._ready = False

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        app.dependency_overrides[get_vector_store] = lambda: self._not_ready_vector_store
        self._deps = [get_db, get_current_active_user, get_evaluate_policy, get_vector_store]

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'V1', '')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        settings.kms_enabled = self._original_kms_enabled
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)


class TestAC20KmsSearchWithoutVectorReadiness(_KmsRouteBase):
    """AC20 (API-004): KMS search must not require vector-store readiness."""

    def test_issue515_ac20_kms_search_without_vector_readiness(self):
        conn = self._connection_pool.get_connection()
        try:
            entry = KMSStore(conn).create_entry(
                vault_id=1,
                title="Zephyr operations runbook",
                body="How to operate and monitor the zephyr cluster.",
            )
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/kms/search", params={"vault_id": 1, "q": "zephyr"}
        )
        self.assertEqual(
            response.status_code,
            200,
            f"KMS search must work while the vector store is not ready, "
            f"got {response.status_code}: {response.text}",
        )
        data = response.json()
        self.assertGreaterEqual(data["total"], 1)
        self.assertEqual(data["entries"][0]["id"], entry.id)
        self.assertIn("zephyr", data["entries"][0]["title"].lower())

        # The entries listing search must also work while not ready.
        listing = self.client.get(
            "/api/kms/entries", params={"vault_id": 1, "search": "zephyr"}
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertGreaterEqual(listing.json()["total"], 1)

        # PRESERVING safety negative: the kms_enabled master switch still 503s.
        settings.kms_enabled = False
        try:
            disabled = self.client.get(
                "/api/kms/search", params={"vault_id": 1, "q": "zephyr"}
            )
            self.assertEqual(disabled.status_code, 503)
            self.assertIn("disabled", disabled.json()["detail"].lower())
        finally:
            settings.kms_enabled = True


class TestAC21UnicodeQueriesFindBodyMatches(TestDocumentAuthBase):
    """AC21 (SEARCH-002): CJK queries match document/KMS BODY content."""

    def _seed_doc(self, file_id, vault_id, file_name, parsed_text,
                  email_subject=None, email_sender=None):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, "
                "chunk_count, vault_id, parsed_text, email_subject, email_sender) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id, file_name, f"/uploads/{file_name}", 100, "indexed",
                    1, vault_id, parsed_text, email_subject, email_sender,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_issue515_ac21_unicode_queries_find_body_matches(self):
        # Body contains the CJK term; the filename deliberately does not.
        self._seed_doc(
            240, 2, "plain_city_notes.txt",
            f"annual planning trip to {_CJK_TERM} for the quarterly review",
        )
        token = self._member_token()
        headers = self._auth_headers(token)

        # (a) documents list search finds the body match.
        listing = self.client.get(
            "/api/documents/", params={"search": _CJK_TERM, "vault_id": 2},
            headers=headers,
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        listed = listing.json()
        self.assertGreaterEqual(
            listed["total"], 1,
            "CJK body match must surface in documents list search "
            f"(filename-only fallback lost it): {listed}",
        )
        self.assertIn(240, [d["id"] for d in listed["documents"]])

        # (b) ranked endpoint returns 200 (not the 400 no-searchable-tokens
        # error) and includes the document.
        ranked = self.client.get(
            "/api/documents/search", params={"q": _CJK_TERM, "vault_id": 2},
            headers=headers,
        )
        self.assertEqual(
            ranked.status_code,
            200,
            f"CJK query must not be rejected as token-free, got "
            f"{ranked.status_code}: {ranked.text}",
        )
        ranked_data = ranked.json()
        self.assertIn(240, [r["id"] for r in ranked_data["results"]])

        # (c) KMSRetrievalService.retrieve finds the CJK body match.
        conn = self._connection_pool.get_connection()
        try:
            entry = KMSStore(conn).create_entry(
                vault_id=2,
                title="Tokyo trip notes",
                body=f"we visited the {_CJK_TERM} office last spring",
            )
        finally:
            self._connection_pool.release_connection(conn)
        original_kms_enabled = settings.kms_enabled
        settings.kms_enabled = True
        try:
            evidence = KMSRetrievalService(self._connection_pool).retrieve(
                _CJK_TERM, vault_id=2
            )
        finally:
            settings.kms_enabled = original_kms_enabled
        self.assertTrue(
            evidence,
            "KMS retrieval must return the entry whose body matches the CJK query",
        )
        self.assertEqual(evidence[0].entry_id, entry.id)

        # PRESERVING: ASCII hyphenated query still tokenizes safely (no 500).
        self._seed_doc(241, 2, "hyphen-probe.pdf", "model-x specification details")
        hyphen = self.client.get(
            "/api/documents/search", params={"q": "model-x", "vault_id": 2},
            headers=headers,
        )
        self.assertEqual(hyphen.status_code, 200, hyphen.text)
        self.assertEqual(hyphen.json()["total"], 1)

        # PRESERVING: pure-punctuation query stays sane (never a 500); the
        # existing 400-on-no-tokens guard may remain for ASCII punctuation.
        punct = self.client.get(
            "/api/documents/search", params={"q": "!!!", "vault_id": 2},
            headers=headers,
        )
        self.assertIn(punct.status_code, (200, 400), punct.text)


class TestAC22MatchCenteredExcerpts(TestDocumentAuthBase):
    """AC22 (SEARCH-001): excerpts are centered on the match, not the first
    N chars of the whole column / a fixed-priority metadata column."""

    def _seed_doc(self, file_id, vault_id, file_name, parsed_text,
                  email_subject=None, email_sender=None):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, "
                "chunk_count, vault_id, parsed_text, email_subject, email_sender) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id, file_name, f"/uploads/{file_name}", 100, "indexed",
                    1, vault_id, parsed_text, email_subject, email_sender,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_issue515_ac22_match_centered_excerpts(self):
        headers = self._auth_headers(self._member_token())

        # (a) body match LATE in the document (term offset > 600 chars).
        late_body = ("filler " * 130) + "uniqueterm appears very late in the body"
        self._seed_doc(250, 2, "late_match_report.txt", late_body)
        ranked = self.client.get(
            "/api/documents/search", params={"q": "uniqueterm", "vault_id": 2},
            headers=headers,
        )
        self.assertEqual(ranked.status_code, 200, ranked.text)
        results = ranked.json()["results"]
        self.assertTrue(results)
        result = results[0]
        self.assertEqual(result["id"], 250)
        self.assertIn(
            "<mark>uniqueterm</mark>",
            result["excerpt"],
            f"late body match must appear (highlighted) in the excerpt; "
            f"got: {result['excerpt'][:120]!r}",
        )

        # (b) sender-only match: term only in email_sender; a non-matching,
        # non-empty email_subject is present.
        self._seed_doc(
            251, 2, "plain_sender_doc.txt", "unrelated body text about budgets",
            email_subject="Totally unrelated subject line",
            email_sender="quartermaster general",
        )
        sender_resp = self.client.get(
            "/api/documents/search", params={"q": "quartermaster", "vault_id": 2},
            headers=headers,
        )
        self.assertEqual(sender_resp.status_code, 200, sender_resp.text)
        sender_results = sender_resp.json()["results"]
        self.assertTrue(sender_results)
        sender_result = sender_results[0]
        self.assertEqual(sender_result["id"], 251)
        self.assertEqual(sender_result["match_type"], "metadata")
        self.assertIn(
            "<mark>quartermaster</mark>",
            sender_result["excerpt"],
            f"sender-only match excerpt must come from the matched column; "
            f"got: {sender_result['excerpt'][:120]!r}",
        )

        # (c) KMS evidence contains a body match beyond 600 chars.
        conn = self._connection_pool.get_connection()
        try:
            entry = KMSStore(conn).create_entry(
                vault_id=2,
                title="Late term runbook",
                body=("filler " * 130) + "uniqueterm appears near the end",
            )
        finally:
            self._connection_pool.release_connection(conn)
        original_kms_enabled = settings.kms_enabled
        settings.kms_enabled = True
        try:
            evidence = KMSRetrievalService(self._connection_pool).retrieve(
                "uniqueterm", vault_id=2
            )
        finally:
            settings.kms_enabled = original_kms_enabled
        self.assertTrue(evidence, "KMS retrieval must find the late body match")
        self.assertEqual(evidence[0].entry_id, entry.id)
        self.assertIn(
            "uniqueterm",
            evidence[0].excerpt,
            f"KMS evidence must contain the matched term for late matches; "
            f"got: {evidence[0].excerpt[:120]!r}",
        )


if __name__ == "__main__":
    unittest.main()
