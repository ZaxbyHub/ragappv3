"""Issue #716 acceptance checks — wiki retrieval page-status / confidence fidelity.

C11-3 (archived page not served): wiki_retrieval's page lookups
(_fts_page_search, _get_page_evidence) never filter wiki_pages.status, so an
ARCHIVED page whose title/body match the query exactly is still served as
WikiEvidence. Zero results may reference the archived page.

C11-4 (zero confidence preserved): _fts_claim_search builds evidence
confidence with ``float(d.get("confidence") or 0.7)``, which turns a stored
confidence of exactly 0.0 into the 0.7 default.

The fixtures mirror tests/test_issue515_wiki_retrieval.py: a real SQLite DB
from init_db/run_migrations and the queue-backed _Pool stub.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as the other wiki test modules).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
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

VAULT_ID = 716
ARCHIVED_PAGE_ID = 7161
ARCHIVED_CLAIM_ID = 7162
ZEROCONF_PAGE_ID = 7163
ZEROCONF_CLAIM_ID = 7164

_PAGE_INSERT = (
    "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, "
    "markdown, summary, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_CLAIM_INSERT = (
    "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, "
    "subject, predicate, object, claim_type, source_type, status, "
    "confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class _Pool:
    """Minimal queue-backed pool matching the production
    get_connection/release_connection interface (mirrors test_issue515)."""

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


def _make_seeded_service(pages, claims):
    """Real DB + seeded wiki rows; returns (service, pool, tmpdir) — the
    caller must close_all/rmtree in a finally block (setUp/tearDown split
    of the neighboring unittest modules, as module-level functions)."""
    tmp = tempfile.mkdtemp()
    db = str(Path(tmp) / "app.db")
    init_db(db)
    run_migrations(db)
    pool = _Pool(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO vaults (id, name) VALUES (?, ?)",
            (VAULT_ID, "Issue716C11"),
        )
        conn.executemany(_PAGE_INSERT, pages)
        conn.executemany(_CLAIM_INSERT, claims)
        conn.commit()
    finally:
        conn.close()
    return WikiRetrievalService(pool=pool), pool, tmp


def test_archived_page_not_served():
    """[issue #716 / C11-3] An ARCHIVED page whose title/body match the query
    exactly must yield zero WikiEvidence for that page — today the page FTS
    lookup never filters status and serves it."""
    pages = [
        (
            ARCHIVED_PAGE_ID, VAULT_ID, "archived-board-review",
            "Quarterly Board Review", "overview",
            "# Quarterly Board Review\nArchived page body.",
            "The quarterly board review summary text.", "archived",
        ),
    ]
    # A claim attached to the archived page, worded so it cannot match the
    # query — the serving path under test is the page lookup, not claims FTS.
    claims = [
        (
            ARCHIVED_CLAIM_ID, VAULT_ID, ARCHIVED_PAGE_ID,
            "Routine maintenance completed quietly.",
            "maintenance", "completed", "quietly",
            "fact", "document", "active", 0.85,
        ),
    ]
    service, pool, tmp = _make_seeded_service(pages, claims)
    try:
        results = service.retrieve("quarterly board review", vault_id=VAULT_ID)
    finally:
        pool.close_all()
        shutil.rmtree(tmp, ignore_errors=True)

    served_count = sum(1 for r in results if r.page_id == ARCHIVED_PAGE_ID)
    assert served_count == 0


def test_zero_confidence_preserved():
    """[issue #716 / C11-4] A claim stored with confidence=0.0 and retrieved
    via claim FTS must return evidence confidence == 0.0 — today
    ``float(d.get("confidence") or 0.7)`` turns 0.0 into the 0.7 default."""
    pages = [
        (
            ZEROCONF_PAGE_ID, VAULT_ID, "zeta-quadrant-survey",
            "Zeta Quadrant Survey Page", "overview",
            "# Zeta Quadrant Survey Page\n", "", "verified",
        ),
    ]
    claims = [
        (
            ZEROCONF_CLAIM_ID, VAULT_ID, ZEROCONF_PAGE_ID,
            "Zeta quadrant survey concluded without findings.",
            "zeta quadrant survey", "concluded", "findings",
            "fact", "document", "active", 0.0,
        ),
    ]
    service, pool, tmp = _make_seeded_service(pages, claims)
    try:
        results = service.retrieve("zeta quadrant survey", vault_id=VAULT_ID)
    finally:
        pool.close_all()
        shutil.rmtree(tmp, ignore_errors=True)

    matching = [r for r in results if r.claim_id == ZEROCONF_CLAIM_ID]
    assert matching, "claim evidence for the seeded zero-confidence claim must be returned"
    evidence = matching[0]
    assert evidence.confidence == 0.0
