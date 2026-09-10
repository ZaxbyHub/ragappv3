"""Issue #515 acceptance tests — WikiRetrieval defect (WIKI-014).

  AC14 (WIKI-014) Relation evidence must carry the REAL page id of the claim's
                   page. Today the relation SQL selects r.* (no page id) and
                   the evidence is built with page_id=d.get('page_id') or 0,
                   so serialized citations always carry page_id=0.

The fixtures mirror backend/tests/test_wiki_retrieval.py
(TestWikiRetrievalEntityAndRelationPipeline seeding helpers): a real
SQLite DB from init_db/run_migrations and the queue-backed _Pool stub.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as the other wiki test modules).
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

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from app.models.database import init_db, run_migrations
from app.services.wiki_retrieval import WikiRetrievalService

VAULT_ID = 515
PAGE_ID = 500       # the claim's real page
ENTITY_ID = 90
CLAIM_ID = 510
RELATION_ID = 600


class _Pool:
    """Minimal queue-backed pool matching the production
    get_connection/release_connection interface (mirrors test_wiki_retrieval)."""

    def __init__(self, path):
        self._path = path
        self._q = Queue(maxsize=5)

    def get_connection(self):
        try:
            return self._q.get_nowait()
        except Empty:
            c = sqlite3.connect(self._path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            return c

    def release_connection(self, c):
        try:
            self._q.put_nowait(c)
        except Exception:
            c.close()

    def close_all(self):
        while True:
            try:
                self._q.get_nowait().close()
            except Empty:
                break


class TestIssue515Ac14RelationEvidencePageId(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        db = str(Path(self._tmp) / "app.db")
        init_db(db)
        run_migrations(db)
        self._pool = _Pool(db)
        self.service = WikiRetrievalService(pool=self._pool)

        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO vaults (id, name) VALUES (?, ?)",
                (VAULT_ID, "Issue515Ac14"),
            )
            # Page P — what the relation evidence's page_id MUST resolve to.
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, "
                "markdown, summary, status) VALUES "
                f"({PAGE_ID}, {VAULT_ID}, 'afomis', 'AFOMIS', 'entity', "
                "'# AFOMIS', 'AFOMIS operations overview.', 'verified')"
            )
            # Entity with the page linked.
            conn.execute(
                "INSERT INTO wiki_entities (id, vault_id, canonical_name, "
                "entity_type, aliases_json, page_id) VALUES "
                f"({ENTITY_ID}, {VAULT_ID}, 'AFOMIS', 'organization', '[]', "
                f"{PAGE_ID})"
            )
            # Active claim attached to page P.
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, "
                "subject, predicate, object, claim_type, source_type, status, "
                "confidence) VALUES "
                f"({CLAIM_ID}, {VAULT_ID}, {PAGE_ID}, 'AFOMIS has a director.', "
                "'AFOMIS', 'director', 'operations', 'fact', 'document', "
                "'active', 0.85)"
            )
            # Relation backed by that claim.
            conn.execute(
                "INSERT INTO wiki_relations (id, vault_id, subject_entity_id, "
                "predicate, object_text, claim_id, confidence) VALUES "
                f"({RELATION_ID}, {VAULT_ID}, {ENTITY_ID}, 'director', "
                f"'operations', {CLAIM_ID}, 0.9)"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        import shutil
        self._pool.close_all()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_issue515_ac14_relation_evidence_carries_real_page_id(self):
        results = self.service.retrieve("AFOMIS director", vault_id=VAULT_ID)
        rel = next(
            (
                r
                for r in results
                if r.claim_id == CLAIM_ID and r.score_type == "relation"
            ),
            None,
        )
        self.assertIsNotNone(
            rel, "relation evidence for the seeded claim must be returned"
        )
        self.assertEqual(
            PAGE_ID,
            rel.page_id,
            "AC14: relation evidence must carry the claim's REAL wiki page id "
            f"({PAGE_ID}); today the SQL selects no page id and the evidence is "
            f"built with page_id=0",
        )
        self.assertNotIn(rel.page_id, (0, None))

        # The serialized citation/card payload carries it too.
        payload = rel.to_dict()
        self.assertEqual(PAGE_ID, payload["page_id"])
        self.assertEqual(f"w_{PAGE_ID}_{CLAIM_ID}", payload["id"])
        # Round-trip through JSON the way callers serialize the card.
        self.assertEqual(PAGE_ID, json.loads(json.dumps(payload))["page_id"])


if __name__ == "__main__":
    unittest.main()
