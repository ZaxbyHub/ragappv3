"""Issue #794 acceptance tests — wiki recompile must supersede removed-sentence claims.

RED at base, GREEN after the fix:

  AC1  Recompiling a document with changed content (same file_id) must
       supersede wiki claims whose sole-source sentence disappeared from the
       new text, so retrieval no longer serves the stale and the new
       contradictory claim together. RED at base, GREEN after the fix.
  AC2  WikiStore.mark_claims_stale_by_file's docstring must not claim it
       writes status='stale' (not a valid wiki_claims status); the code
       writes 'superseded'. RED at base, GREEN after the docstring fix.

Harness mirrors backend/tests/test_issue515_wiki_compiler.py / the
test_wiki_compile_processor ``_make_env`` pattern: a real temp SQLite DB
built by ``run_migrations`` with the optional heavy deps stubbed the same
way. No network / no LLM — the curator is force-disabled so only the
deterministic compiler paths run.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as test_wiki_curator.py /
# test_wiki_compile_processor.py so collection never depends on heavy deps).
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

from app.config import settings
from app.models.database import run_migrations
from app.services.wiki_compiler import WikiCompiler
from app.services.wiki_store import WikiStore


def test_recompile_supersedes_removed_sentence_claim_only():
    """AC1 (issue #794): recompile of changed text supersedes the
    sole-source claim of a sentence that no longer exists, while the
    unchanged sentence's claim stays active. RED at base, GREEN after the
    fix."""
    v1 = "Alice Smith is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."
    v2 = "Bob Jones is the AFOMIS Director. Carol Doe is the AFOMIS Deputy Director."

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, file_size, status) "
        "VALUES (1, 1, '/tmp/test.txt', 'test.txt', 100, 'indexed')"
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)

    saved_curator = getattr(settings, "wiki_llm_curator_enabled")
    settings.wiki_llm_curator_enabled = False
    try:
        compiler.compile_ingest_job(1, {"file_id": 1, "text": v1})
        compiler.compile_ingest_job(1, {"file_id": 1, "text": v2})

        alice_active = conn.execute(
            "SELECT COUNT(*) FROM wiki_claims "
            "WHERE status = 'active' AND claim_text LIKE '%Alice Smith%'"
        ).fetchone()[0]
        carol_active = conn.execute(
            "SELECT COUNT(*) FROM wiki_claims "
            "WHERE status = 'active' AND claim_text LIKE '%Carol Doe%'"
        ).fetchone()[0]
    finally:
        settings.wiki_llm_curator_enabled = saved_curator
        conn.close()
        shutil.rmtree(td, ignore_errors=True)

    assert (alice_active, carol_active) == (0, 1)


def test_mark_claims_stale_docstring_matches_written_status():
    """AC2 (issue #794 follow-up): the mark_claims_stale_by_file docstring
    must not document the nonexistent status='stale' — the code writes
    'superseded'. RED at base, GREEN after the docstring fix."""
    docstring = WikiStore.mark_claims_stale_by_file.__doc__
    contains_forbidden_status = "status='stale'" in docstring
    assert contains_forbidden_status is False
