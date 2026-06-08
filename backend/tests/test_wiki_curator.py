"""PR C: tests for the optional LLM Wiki Curator.

Covers:
  - The wiki_claims migration widens status CHECK to include
    'needs_review', adds created_by_kind, and preserves FTS triggers.
  - WikiCurator verification: source_quote substring + rapidfuzz fuzzy
    match; missing quote / wrong chunk_id / quote_mismatch all
    rejected; mode 'draft' stamps needs_review even on quote match.
  - Compiler integration: curator disabled => no calls. Curator enabled
    + ingest flag false => no calls. Curator enabled + ingest flag true
    => candidates flow through. Curator failures never fail the job.
  - extract_json handles strict / fenced / trailing-prose JSON.
  - PUT /wiki/claims with status=active on a curator claim re-verifies
    the source_quote and 400s on mismatch.
  - SSRF guard fires inside CuratorClient.propose, not just settings.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs.
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
from app.models.database import (
    init_db,
    migrate_add_curator_claim_support,
    run_migrations,
)
from app.services.wiki_curator import (
    CuratorChunk,
    WikiCurator,
    _quote_matches,
    deterministic_dedupe_key,
)
from app.services.wiki_curator_client import (
    CuratorClient,
    extract_json,
)
from app.services.wiki_store import WikiStore

# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestCuratorClaimMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "app.db")

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)
        os.rmdir(self.tmp)

    def test_fresh_init_includes_widened_check_and_new_column(self):
        # Fresh init via run_migrations triggers init_db (SCHEMA) +
        # migrate_add_wiki_tables (which now also includes the new
        # columns) + the explicit migrate_add_curator_claim_support.
        run_migrations(self.db)
        conn = sqlite3.connect(self.db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
            # 'needs_review' must be writable.
            conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                "VALUES (1, 't', 'document', 'needs_review')"
            )
            conn.commit()
            # And every other valid status.
            for s in ("active", "contradicted", "superseded", "unverified", "archived"):
                conn.execute(
                    "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                    "VALUES (1, ?, 'document', ?)",
                    (f"t_{s}", s),
                )
            conn.commit()
            # Forbidden status must still fail.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                    "VALUES (1, 'bad', 'document', 'invalid_status')"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_migration_idempotent(self):
        # First run via run_migrations.
        run_migrations(self.db)
        # Second explicit call must be a no-op.
        migrate_add_curator_claim_support(self.db)
        # Still fine.
        conn = sqlite3.connect(self.db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
        finally:
            conn.close()

    def test_upgrade_path_preserves_fk_cascades(self):
        """Reviewer Fix #1 (CRITICAL): construct the pre-PR-C schema
        directly and run the migration. After the swap, deleting a
        wiki_claims row MUST cascade to wiki_claim_sources and
        wiki_relations. Without ``legacy_alter_table=ON`` SQLite
        rewrites the child FK references to point at wiki_claims_old,
        and after the DROP the cascade silently no-ops."""
        # Build the pre-PR-C schema by hand.
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE wiki_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    slug TEXT, title TEXT, page_type TEXT, markdown TEXT,
                    summary TEXT, status TEXT, confidence REAL,
                    created_by INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP
                );
                -- Pre-PR-C wiki_claims: status CHECK does NOT include
                -- needs_review and there is no created_by_kind column.
                CREATE TABLE wiki_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                    claim_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL DEFAULT 'fact',
                    subject TEXT, predicate TEXT, object TEXT,
                    source_type TEXT NOT NULL CHECK (source_type IN (
                        'document','memory','chat_synthesis','manual','mixed')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','contradicted','superseded','unverified','archived')),
                    confidence REAL DEFAULT 0.0,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE wiki_claim_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL,
                    file_id INTEGER, chunk_id TEXT, source_label TEXT,
                    quote TEXT
                );
                CREATE TABLE wiki_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    subject_entity_id INTEGER,
                    predicate TEXT, object_entity_id INTEGER, object_text TEXT,
                    claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE,
                    confidence REAL DEFAULT 0.0
                );
                CREATE VIRTUAL TABLE wiki_claims_fts USING fts5(
                    claim_text, subject, predicate, object,
                    content='wiki_claims', content_rowid='id'
                );
                CREATE TRIGGER wiki_claims_fts_insert AFTER INSERT ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object)
                    VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
                END;
                CREATE TRIGGER wiki_claims_fts_delete AFTER DELETE ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object)
                    VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
                END;
                CREATE TRIGGER wiki_claims_fts_update AFTER UPDATE ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object)
                    VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
                    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object)
                    VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
                END;
                """
            )
            # Seed a claim + source row + relation that should cascade.
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type, status) "
                "VALUES (1, 1, 'Alice founded the company', 'document', 'active')"
            )
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind, quote) "
                "VALUES (1, 'document', 'Alice founded the company')"
            )
            conn.execute(
                "INSERT INTO wiki_relations (vault_id, predicate, claim_id) "
                "VALUES (1, 'founded', 1)"
            )
            # Sanity: cascade works pre-migration.
            conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.close()

        # Run the migration against the pre-PR-C schema.
        migrate_add_curator_claim_support(self.db)

        # Re-open and verify (a) the new column / status work, (b) the
        # FK references in dependent tables still point at wiki_claims
        # (NOT wiki_claims_old), (c) cascades fire end to end, (d)
        # foreign_key_check is clean.
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
            # FK target is the canonical name, not wiki_claims_old.
            for child in ("wiki_claim_sources", "wiki_relations"):
                fks = conn.execute(f"PRAGMA foreign_key_list({child})").fetchall()
                ref_tables = {row[2] for row in fks}
                self.assertIn(
                    "wiki_claims",
                    ref_tables,
                    f"{child} FK should reference wiki_claims, got {ref_tables}",
                )
                self.assertNotIn(
                    "wiki_claims_old",
                    ref_tables,
                    f"{child} FK still points at wiki_claims_old after migration",
                )
            # Cascade integrity check.
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
            # Now actually delete the parent and ensure the cascade runs.
            conn.execute("DELETE FROM wiki_claims WHERE id = 1")
            self.assertEqual(
                [], conn.execute("SELECT * FROM wiki_claim_sources").fetchall()
            )
            self.assertEqual(
                [], conn.execute("SELECT * FROM wiki_relations").fetchall()
            )
        finally:
            conn.close()

    def test_recovery_drops_stale_old_table_alongside_pre_pr_c_new_table(self):
        """Critic Fix #1 (LOW): if a previous run somehow left
        ``wiki_claims_old`` lingering AND ``wiki_claims`` is still in
        the pre-PR-C shape, the next run must drop the stale old table
        before re-renaming, otherwise ALTER TABLE fails with
        'table wiki_claims_old already exists'."""
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.executescript(
                """
                CREATE TABLE wiki_pages (id INTEGER PRIMARY KEY);
                CREATE TABLE wiki_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                    claim_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL DEFAULT 'fact',
                    subject TEXT, predicate TEXT, object TEXT,
                    source_type TEXT NOT NULL CHECK (source_type IN (
                        'document','memory','chat_synthesis','manual','mixed')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','contradicted','superseded','unverified','archived')),
                    confidence REAL DEFAULT 0.0,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                -- Stale wiki_claims_old left behind by a prior crash.
                CREATE TABLE wiki_claims_old (
                    id INTEGER PRIMARY KEY,
                    vault_id INTEGER NOT NULL,
                    claim_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL DEFAULT 'fact',
                    source_type TEXT NOT NULL CHECK (source_type IN (
                        'document','memory','chat_synthesis','manual','mixed')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','contradicted','superseded','unverified','archived')),
                    confidence REAL DEFAULT 0.0,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE wiki_claim_sources (
                    id INTEGER PRIMARY KEY,
                    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE
                );
                CREATE TABLE wiki_relations (
                    id INTEGER PRIMARY KEY,
                    vault_id INTEGER, predicate TEXT,
                    claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE wiki_claims_fts USING fts5(
                    claim_text, subject, predicate, object,
                    content='wiki_claims', content_rowid='id'
                );
                """
            )
            # Seed a row in the live table; stale table can stay empty.
            conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                "VALUES (1, 'live row', 'document', 'active')"
            )
        finally:
            conn.close()

        # Should NOT raise — the migration must drop the stale table first.
        migrate_add_curator_claim_support(self.db)

        conn = sqlite3.connect(self.db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
            # Stale table is gone.
            old = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='wiki_claims_old'"
            ).fetchone()
            self.assertIsNone(old)
            # Live row preserved.
            self.assertEqual(
                conn.execute(
                    "SELECT claim_text FROM wiki_claims WHERE claim_text='live row'"
                ).fetchone()[0],
                "live row",
            )
        finally:
            conn.close()

    def test_upgrade_path_recovers_from_interrupted_rename(self):
        """Reviewer Fix #3 (HIGH): if a previous run died after the
        rename but before the new CREATE TABLE, ``wiki_claims_old`` is
        present and ``wiki_claims`` is missing. The migration must
        detect this and restore ``wiki_claims`` by renaming back."""
        # Pre-create a stub wiki_claims_old (mimicking a crash after
        # rename). No wiki_claims present.
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.executescript(
                """
                CREATE TABLE wiki_pages (id INTEGER PRIMARY KEY);
                CREATE TABLE wiki_claims_old (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                    claim_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL DEFAULT 'fact',
                    subject TEXT, predicate TEXT, object TEXT,
                    source_type TEXT NOT NULL CHECK (source_type IN (
                        'document','memory','chat_synthesis','manual','mixed')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','contradicted','superseded','unverified','archived')),
                    confidence REAL DEFAULT 0.0,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE wiki_claim_sources (
                    id INTEGER PRIMARY KEY,
                    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE
                );
                CREATE TABLE wiki_relations (
                    id INTEGER PRIMARY KEY,
                    vault_id INTEGER, predicate TEXT,
                    claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE wiki_claims_fts USING fts5(
                    claim_text, subject, predicate, object
                );
                """
            )
            conn.execute(
                "INSERT INTO wiki_claims_old (vault_id, claim_text, source_type, status) "
                "VALUES (1, 'recovered', 'document', 'active')"
            )
        finally:
            conn.close()

        migrate_add_curator_claim_support(self.db)
        # After recovery the data should land in wiki_claims with the
        # new column and the old table should be gone.
        conn = sqlite3.connect(self.db)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
            }
            self.assertIn("created_by_kind", cols)
            rows = conn.execute(
                "SELECT claim_text FROM wiki_claims WHERE claim_text = 'recovered'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            old = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='wiki_claims_old'"
            ).fetchone()
            self.assertIsNone(old)
        finally:
            conn.close()

    def test_fts_search_works_after_migration(self):
        run_migrations(self.db)
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                "VALUES (1, 'CEO Alice founded the company', 'document', 'active')"
            )
            conn.commit()
            cid = cur.lastrowid
            # FTS triggers should populate the FTS table.
            rows = conn.execute(
                "SELECT rowid FROM wiki_claims_fts WHERE wiki_claims_fts MATCH 'Alice'"
            ).fetchall()
            self.assertTrue(any(r[0] == cid for r in rows))
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class TestExtractJson(unittest.TestCase):
    def test_strict(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json_block(self):
        text = "Sure!\n```json\n{\"a\": [1,2]}\n```\nDone."
        self.assertEqual(extract_json(text), {"a": [1, 2]})

    def test_trailing_prose(self):
        text = '{"a": 1}\n\nLet me know if anything else is needed.'
        self.assertEqual(extract_json(text), {"a": 1})

    def test_garbage(self):
        self.assertIsNone(extract_json("hello world"))
        self.assertIsNone(extract_json(""))

    def test_balanced_brace_with_strings_containing_braces(self):
        text = 'prefix {"a": "x{y}z"} suffix'
        self.assertEqual(extract_json(text), {"a": "x{y}z"})

    def test_balanced_brace_with_escaped_quotes(self):
        # The harder case: a JSON string field containing an escaped
        # double-quote. The scanner must track escape state so it
        # doesn't think the string ends mid-content.
        text = r'prefix {"a": "has \"quote\" inside"} suffix'
        self.assertEqual(extract_json(text), {"a": 'has "quote" inside'})

    def test_balanced_brace_with_brace_after_escaped_quote(self):
        # A `}` inside a string that ends with `\"` — the scanner must
        # leave string mode AT the closing real quote, not at the
        # escaped one.
        text = r'{"a": "ends with \"x\" }", "b": 1} trailing'
        self.assertEqual(extract_json(text), {"a": 'ends with "x" }', "b": 1})


# ---------------------------------------------------------------------------
# Quote verification
# ---------------------------------------------------------------------------


class TestQuoteMatches(unittest.TestCase):
    def test_exact_substring(self):
        self.assertTrue(_quote_matches("hello world", "say hello world to me"))

    def test_whitespace_normalized(self):
        self.assertTrue(_quote_matches("hello   world", "say hello world to me"))

    def test_case_insensitive(self):
        self.assertTrue(_quote_matches("Hello World", "say hello world to me"))

    def test_fuzzy_above_threshold(self):
        # 1 typo out of 11 chars — partial_ratio ~95
        self.assertTrue(
            _quote_matches("hello wrold", "say hello world to me", fuzzy_threshold=85)
        )

    def test_unrelated_quote_rejected(self):
        self.assertFalse(_quote_matches("foo bar baz", "completely different text"))

    def test_empty_inputs(self):
        self.assertFalse(_quote_matches("", "anything"))
        self.assertFalse(_quote_matches("anything", ""))


# ---------------------------------------------------------------------------
# WikiCurator verification logic (no real HTTP)
# ---------------------------------------------------------------------------


class _StubClient:
    """Fake CuratorClient.propose returning a canned string.

    We don't need a real httpx server; the verification logic happens
    after the JSON is parsed. Stubbing at this boundary keeps tests
    fast and deterministic.
    """

    def __init__(self, payload: str, *, raise_exc: Exception = None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = 0

    async def propose(self, *_args, **_kwargs):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload


class TestWikiCurator(unittest.TestCase):
    def setUp(self):
        # Snapshot + override curator settings per test for isolation.
        self._snap = {
            f: getattr(settings, f)
            for f in (
                "wiki_llm_curator_enabled",
                "wiki_llm_curator_url",
                "wiki_llm_curator_model",
                "wiki_llm_curator_mode",
                "wiki_llm_curator_require_quote_match",
                "wiki_llm_curator_require_chunk_id",
                "wiki_llm_curator_max_input_chars",
            )
        }
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_url = "https://api.example.com"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        settings.wiki_llm_curator_max_input_chars = 6000

    def tearDown(self):
        for f, v in self._snap.items():
            setattr(settings, f, v)

    def _curator(self, payload: str, *, raise_exc=None) -> WikiCurator:
        return WikiCurator(client=_StubClient(payload, raise_exc=raise_exc))

    def _chunks(self) -> list[CuratorChunk]:
        return [
            CuratorChunk(
                chunk_id="42_0",
                source_text="Alice founded the company. Bob is the CTO.",
                file_id=42,
                source_label="file:42",
            )
        ]

    def test_accepts_when_quote_matches_chunk(self):
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "Alice founded the company",
                    "claim_type": "fact",
                    "subject": "Alice",
                    "predicate": "founded",
                    "object": "the company",
                    "source_quote": "Alice founded the company",
                    "chunk_id": "42_0",
                    "confidence": 0.9,
                }
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 1)
        self.assertEqual(out.accepted[0].status, "active")  # mode=active_if_verified
        self.assertEqual(out.accepted[0].chunk_id, "42_0")
        self.assertEqual(out.accepted[0].file_id, 42)
        self.assertEqual(len(out.rejected), 0)

    def test_draft_mode_stamps_needs_review_even_on_quote_match(self):
        settings.wiki_llm_curator_mode = "draft"
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "Bob is the CTO",
                    "claim_type": "role",
                    "subject": "Bob",
                    "predicate": "is",
                    "object": "the CTO",
                    "source_quote": "Bob is the CTO",
                    "chunk_id": "42_0",
                    "confidence": 0.95,
                }
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 1)
        self.assertEqual(out.accepted[0].status, "needs_review")

    def test_rejects_missing_quote_and_emits_lint(self):
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "An unsupported assertion",
                    "source_quote": "",
                    "chunk_id": "42_0",
                }
            ]
        })
        store = MagicMock()
        cur = WikiCurator(client=_StubClient(payload), store=store)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.rejected), 1)
        self.assertEqual(out.rejected[0].reason, "missing_quote")
        # Lint finding emitted.
        self.assertEqual(len(out.lint_findings), 1)
        self.assertEqual(
            out.lint_findings[0]["finding_type"], "unsupported_claim"
        )

    def test_rejects_unknown_chunk_id(self):
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "x",
                    "source_quote": "Alice founded the company",
                    "chunk_id": "999_0",
                }
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.rejected), 1)
        self.assertEqual(out.rejected[0].reason, "missing_chunk_id")

    def test_rejects_quote_mismatch(self):
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "x",
                    "source_quote": "completely fabricated text not in source",
                    "chunk_id": "42_0",
                }
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.rejected), 1)
        self.assertEqual(out.rejected[0].reason, "quote_mismatch")

    def test_dedupes_against_deterministic_keys(self):
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "Alice founded the company",
                    "subject": "Alice",
                    "predicate": "founded",
                    "object": "the company",
                    "source_quote": "Alice founded the company",
                    "chunk_id": "42_0",
                }
            ]
        })
        seed = {
            deterministic_dedupe_key(
                "Alice", "founded", "the company", "Alice founded the company"
            )
        }
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks(),
                deterministic_dedupe_keys=seed,
            )
        )
        # Already covered by deterministic — silent drop, no lint.
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.rejected), 0)
        self.assertEqual(len(out.lint_findings), 0)

    def test_malformed_json_records_error_no_crash(self):
        cur = self._curator("not even json")
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertIn("json_parse_failed", out.errors)

    def test_transport_exception_records_error_no_crash(self):
        cur = self._curator("", raise_exc=RuntimeError("boom"))
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertTrue(any("transport_error" in e for e in out.errors))

    def test_per_chunk_parallelism_uses_concurrency_setting(self):
        """Reviewer Fix #4 (HIGH): wiki_llm_curator_concurrency must
        actually bound parallel curator calls. We track concurrent
        in-flight calls inside a stub and assert it never exceeds the
        configured cap."""
        settings.wiki_llm_curator_concurrency = 2

        in_flight = {"now": 0, "max": 0, "calls": 0}

        class _ConcurrencyStub:
            def __init__(self, payload: str):
                self.payload = payload

            async def propose(self, *_args, **_kwargs):
                in_flight["now"] += 1
                in_flight["calls"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
                # Yield so other gather() coroutines can interleave.
                await asyncio.sleep(0.02)
                in_flight["now"] -= 1
                return self.payload

        # 5 chunks; with concurrency=2 the stub should observe at most
        # 2 in-flight calls at any moment.
        chunks = [
            CuratorChunk(
                chunk_id=f"42_{i}",
                source_text=f"sentence {i}.",
                file_id=42,
                source_label="file:42",
            )
            for i in range(5)
        ]
        payload = json.dumps({"claims": []})
        cur = WikiCurator(client=_ConcurrencyStub(payload))
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=chunks)
        )
        self.assertEqual(out.calls, 5)
        self.assertEqual(in_flight["calls"], 5)
        self.assertLessEqual(in_flight["max"], 2)
        self.assertGreaterEqual(in_flight["max"], 1)

    def test_contradictions_become_lint_only(self):
        payload = json.dumps({
            "claims": [],
            "contradictions": [
                {
                    "claim_a": "A says X",
                    "claim_b": "B says not X",
                    "reason": "conflict",
                    "source_quote": "ignored",
                }
            ],
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(vault_id=1, file_id=42, chunks=self._chunks())
        )
        self.assertEqual(len(out.accepted), 0)
        self.assertEqual(len(out.lint_findings), 1)
        self.assertEqual(out.lint_findings[0]["finding_type"], "contradiction")


# ---------------------------------------------------------------------------
# CuratorClient SSRF guard
# ---------------------------------------------------------------------------


class TestCuratorClientSSRF(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ALLOW_LOCAL_CURATOR", None)

    def tearDown(self):
        os.environ.pop("ALLOW_LOCAL_CURATOR", None)

    def test_propose_blocks_loopback_without_opt_in(self):
        from app.services.curator_ssrf import CuratorURLBlocked

        client = CuratorClient(
            base_url="http://127.0.0.1:11434", model="qwen", timeout=5
        )

        async def runner():
            await client.propose([{"role": "user", "content": "hi"}])

        with self.assertRaises(CuratorURLBlocked):
            asyncio.run(runner())


# ---------------------------------------------------------------------------
# Compiler integration: curator gating
# ---------------------------------------------------------------------------


class TestCompilerCuratorGating(unittest.TestCase):
    """Sanity: when curator settings disable it, no client construction."""

    def setUp(self):
        self._snap = {
            f: getattr(settings, f)
            for f in (
                "wiki_llm_curator_enabled",
                "wiki_llm_curator_run_on_ingest",
                "wiki_llm_curator_run_on_query",
                "wiki_llm_curator_run_on_manual",
                "wiki_llm_curator_url",
                "wiki_llm_curator_model",
            )
        }

    def tearDown(self):
        for f, v in self._snap.items():
            setattr(settings, f, v)

    def _patched_compiler(self):
        # Build a WikiCompiler instance with stubbed db/store. We only
        # exercise the gate, not the full compile_ingest_job.
        from app.services.wiki_compiler import WikiCompiler

        store = MagicMock()
        compiler = WikiCompiler.__new__(WikiCompiler)  # type: ignore
        compiler._db = MagicMock()
        compiler._store = store
        return compiler, store

    def _ext(self):
        # The deterministic extractor's class name varies; the compiler
        # only reads .acronyms / .persons / .role_claims, so duck-type.
        class _E:
            acronyms: list = []
            persons: list = []
            role_claims: list = []

        return _E()

    def test_disabled_curator_returns_none(self):
        settings.wiki_llm_curator_enabled = False
        compiler, _ = self._patched_compiler()
        with patch(
            "app.services.wiki_compiler.CuratorClient"
        ) as ctor, patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            res = compiler._maybe_run_curator(
                vault_id=1,
                file_id=42,
                text="text",
                trigger="ingest",
                deterministic_extraction=self._ext(),
                page_id=1,
                existing_entities=[],
            )
        self.assertIsNone(res)
        ctor.assert_not_called()
        cur_ctor.assert_not_called()

    def test_enabled_but_query_flag_off_returns_none(self):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_url = "https://api.example.com"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_run_on_query = False
        compiler, _ = self._patched_compiler()
        with patch(
            "app.services.wiki_compiler.CuratorClient"
        ) as ctor, patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor:
            res = compiler._maybe_run_curator(
                vault_id=1,
                file_id=42,
                text="text",
                trigger="query",
                deterministic_extraction=self._ext(),
                page_id=1,
                existing_entities=[],
            )
        self.assertIsNone(res)
        ctor.assert_not_called()
        cur_ctor.assert_not_called()

    def test_enabled_but_url_blank_records_disabled(self):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = ""
        settings.wiki_llm_curator_model = "qwen-1b"
        compiler, _ = self._patched_compiler()
        res = compiler._maybe_run_curator(
            vault_id=1,
            file_id=42,
            text="text",
            trigger="ingest",
            deterministic_extraction=self._ext(),
            page_id=1,
            existing_entities=[],
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["accepted"], 0)
        self.assertTrue(any("disabled" in e for e in res["errors"]))


# ---------------------------------------------------------------------------
# Regression: running-loop path uses a shared executor (issue #109 — DD-C010)
# ---------------------------------------------------------------------------


class TestCuratorRunningLoopSharedExecutor(unittest.IsolatedAsyncioTestCase):
    """Issue #109: when called from inside a running event loop, the
    running-loop branch must reuse a single module-level executor
    instead of constructing (and tearing down) a fresh
    ``ThreadPoolExecutor(max_workers=1)`` for every curator call.
    """

    def setUp(self):
        self._snap = {
            f: getattr(settings, f)
            for f in (
                "wiki_llm_curator_enabled",
                "wiki_llm_curator_run_on_ingest",
                "wiki_llm_curator_url",
                "wiki_llm_curator_model",
            )
        }
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://api.example.com"
        settings.wiki_llm_curator_model = "qwen-1b"

    def tearDown(self):
        for f, v in self._snap.items():
            setattr(settings, f, v)

    async def test_running_loop_branch_uses_shared_executor(self):
        """The running-loop branch must not construct a new
        ThreadPoolExecutor per call — it must submit to the module-level
        shared executor. The thread is still required to break the
        event-loop deadlock; only the executor-construction overhead is
        removed.
        """
        from app.services.wiki_compiler import (
            _CURATOR_OFFLOAD_EXECUTOR,
            WikiCompiler,
        )
        from app.services.wiki_curator import CuratorResult

        compiler = WikiCompiler.__new__(WikiCompiler)  # type: ignore
        compiler._db = MagicMock()
        compiler._store = MagicMock()

        class _E:
            acronyms: list = []
            persons: list = []
            role_claims: list = []

        fake_result = CuratorResult()
        fake_result.accepted = []  # type: ignore[assignment]
        fake_result.lint_findings = []  # type: ignore[assignment]
        fake_result.errors = []  # type: ignore[assignment]

        async def _fake_curate(**_kwargs):
            return fake_result

        # The running-loop branch must not construct a new
        # ThreadPoolExecutor per call — it must submit to the
        # module-level shared executor. The thread is still required
        # to break the event-loop deadlock; only the
        # executor-construction overhead is removed.
        with patch(
            "app.services.wiki_compiler.CuratorClient"
        ), patch(
            "app.services.wiki_compiler.WikiCurator"
        ) as cur_ctor, patch(
            "app.services.wiki_compiler._CURATOR_OFFLOAD_EXECUTOR",
            wraps=_CURATOR_OFFLOAD_EXECUTOR,
        ) as shared_executor:
            cur_ctor.return_value.curate = _fake_curate
            res = compiler._maybe_run_curator(
                vault_id=1,
                file_id=42,
                text="text",
                trigger="ingest",
                deterministic_extraction=_E(),
                page_id=1,
                existing_entities=[],
            )

        # Result flowed through the running-loop branch.
        self.assertIsInstance(res, dict)
        self.assertEqual(res["accepted"], 0)
        # Shared executor was used exactly once.
        shared_executor.submit.assert_called_once()


# ---------------------------------------------------------------------------
# PUT /wiki/claims re-verification
# ---------------------------------------------------------------------------


class _Pool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    from contextlib import contextmanager

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class TestPutClaimReverify(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.deps import get_db, get_db_pool
        from app.main import app
        from app.services.auth_service import create_access_token, hash_password

        self.app = app
        self.client = TestClient(app)
        self.create_token = create_access_token
        self.hash_password = hash_password

        self.tmp = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        self._original_jwt = settings.jwt_secret_key
        self._original_users = settings.users_enabled
        settings.data_dir = Path(self.tmp)
        settings.users_enabled = True
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"

        self.db = str(Path(self.tmp) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()

        run_migrations(self.db)
        self.pool = _Pool(self.db)

        def override_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_db_pool] = lambda: self.pool

        # Seed admin + vault membership.
        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM users WHERE id != 0")
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1, 'admin1', ?, 'A', 'admin', 1)",
                (self.hash_password("pw"),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2, 'V2', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2, 1, 'write', 1)"
            )
            # Files row with parsed_text.
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name, file_size, status, parsed_text) "
                "VALUES (100, 2, '/uploads/x.txt', 'x.txt', 10, 'indexed', ?)",
                ("Alice founded the company. Bob is the CTO.",),
            )
            # Curator-authored claim with verifiable quote.
            cur = conn.execute(
                """
                INSERT INTO wiki_claims
                (vault_id, claim_text, claim_type, source_type, status, created_by_kind)
                VALUES (2, 'Alice founded the company', 'fact', 'document',
                        'needs_review', 'llm_curator')
                """
            )
            self.good_claim_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO wiki_claim_sources
                (claim_id, source_kind, file_id, chunk_id, source_label, quote)
                VALUES (?, 'document', 100, '100_0', 'file:100', ?)
                """,
                (self.good_claim_id, "Alice founded the company"),
            )
            # Curator-authored claim whose quote NO LONGER appears.
            cur2 = conn.execute(
                """
                INSERT INTO wiki_claims
                (vault_id, claim_text, claim_type, source_type, status, created_by_kind)
                VALUES (2, 'Carol is the CFO', 'role', 'document',
                        'needs_review', 'llm_curator')
                """
            )
            self.bad_claim_id = cur2.lastrowid
            conn.execute(
                """
                INSERT INTO wiki_claim_sources
                (claim_id, source_kind, file_id, chunk_id, source_label, quote)
                VALUES (?, 'document', 100, '100_0', 'file:100', ?)
                """,
                (self.bad_claim_id, "Carol is the CFO"),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        self.token = self.create_token(1, "admin1", "admin")

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        self.app.dependency_overrides.clear()
        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()
        self.pool.close_all()
        settings.data_dir = self._original_data_dir
        settings.jwt_secret_key = self._original_jwt
        settings.users_enabled = self._original_users
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hdr(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_promotion_with_verifiable_quote_succeeds(self):
        r = self.client.put(
            f"/api/wiki/claims/{self.good_claim_id}",
            headers=self._hdr(),
            json={"vault_id": 2, "status": "active"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "active")

    def test_promotion_with_unverifiable_quote_returns_400(self):
        r = self.client.put(
            f"/api/wiki/claims/{self.bad_claim_id}",
            headers=self._hdr(),
            json={"vault_id": 2, "status": "active"},
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("source_quote", r.json()["detail"])

    def test_promotion_with_no_file_source_returns_actionable_400(self):
        """Critic Fix #3 (LOW): curator claims authored on the
        query/manual trigger have file_id=NULL on every source row.
        The PUT must surface a clear 'no file source attached' message
        rather than the generic 'no longer verifiable' so operators
        know the claim CAN'T be auto-promoted (vs has just drifted)."""
        # Insert a curator claim whose only source has file_id=NULL +
        # a quote (mimics the query/manual curator trigger).
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO wiki_claims
                (vault_id, claim_text, claim_type, source_type, status, created_by_kind)
                VALUES (2, 'Sourceless curator claim', 'fact', 'document',
                        'needs_review', 'llm_curator')
                """
            )
            sourceless_claim_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO wiki_claim_sources
                (claim_id, source_kind, file_id, chunk_id, source_label, quote)
                VALUES (?, 'document', NULL, 'text_0', 'curator', 'Sourceless curator claim')
                """,
                (sourceless_claim_id,),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        r = self.client.put(
            f"/api/wiki/claims/{sourceless_claim_id}",
            headers=self._hdr(),
            json={"vault_id": 2, "status": "active"},
        )
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertIn("no source row references a document file", detail)
        self.assertIn("manual", detail)

    def test_non_active_status_change_skips_reverify(self):
        # Switching from needs_review to archived should not require quote
        # verification (it's only the active gate that needs proof).
        r = self.client.put(
            f"/api/wiki/claims/{self.bad_claim_id}",
            headers=self._hdr(),
            json={"vault_id": 2, "status": "archived"},
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
