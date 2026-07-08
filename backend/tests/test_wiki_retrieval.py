"""Tests for WikiRetrievalService and related helpers."""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import pytest

from app.services.wiki_retrieval import (
    WikiEvidence,
    WikiRetrievalService,
    extract_query_intent,
    normalize_fts_query,
)


class TestNormalizeFtsQuery(unittest.TestCase):
    def test_strips_common_stop_words(self):
        result = normalize_fts_query("who is the chief of staff")
        # "who", "is", "the", "of" are stop words; "chief" and "staff" should remain
        self.assertIn("chief", result)
        self.assertIn("staff", result)
        self.assertNotIn(" is ", f" {result} ")

    def test_preserves_all_caps_acronyms(self):
        result = normalize_fts_query("what does AFOMIS do")
        self.assertIn("AFOMIS", result)

    def test_escapes_fts5_operators(self):
        # FTS5 special chars must be stripped; vacuous isinstance replaced
        raw = normalize_fts_query("AND OR NOT test")
        self.assertIn("test", raw)
        for ch in r'"()*\-/:;,?!@#$%^&+=<>{}|[]\\':
            self.assertNotIn(ch, raw, f"char {ch!r} must not appear in output")

    def test_empty_query_returns_empty(self):
        self.assertEqual(normalize_fts_query(""), "")

    def test_single_acronym_preserved(self):
        result = normalize_fts_query("AFMEDCOM")
        self.assertIn("AFMEDCOM", result)


class TestFtsOperatorAdversarial(unittest.TestCase):
    """Adversarial tests for FTS5 operator stripping in normalize_fts_query."""

    def test_quotes_stripped(self):
        result = normalize_fts_query('"python java"')
        self.assertNotIn('"', result)
        self.assertIn("python", result)
        self.assertIn("java", result)

    def test_parentheses_stripped(self):
        result = normalize_fts_query("python (AND java)")
        self.assertNotIn("(", result)
        self.assertNotIn(")", result)

    def test_asterisk_stripped(self):
        result = normalize_fts_query("pyth*")
        self.assertNotIn("*", result)
        self.assertIn("pyth", result)

    def test_colon_stripped(self):
        # Colon is a column-qualifier prefix — must not appear in output
        result = normalize_fts_query("title:python")
        self.assertNotIn(":", result)
        self.assertIn("python", result)

    def test_brackets_stripped(self):
        result = normalize_fts_query("[secret]")
        self.assertNotIn("[", result)
        self.assertNotIn("]", result)
        # Brackets stripped; "secret" itself is kept (>=2 chars, not a stop word)
        self.assertIn("secret", result)

    def test_pipe_stripped(self):
        result = normalize_fts_query("python|java")
        self.assertNotIn("|", result)

    def test_boolean_and_does_not_crash(self):
        # AND is an acronym (2+ uppercase) so it passes through, but
        # since tokens are space-joined the result is safe for FTS5.
        result = normalize_fts_query("python AND java")
        self.assertIsInstance(result, str)
        for ch in r'"()*\-/:;,?!@#$%^&+=<>{}|[]\\':
            self.assertNotIn(ch, result)

    def test_phrase_query_neutralized(self):
        # Phrase quotes are stripped; the phrase is kept as a joined string
        result = normalize_fts_query('"exact phrase"')
        self.assertNotIn('"', result)
        # Quotes replaced with spaces, phrase kept as-is (joined)
        self.assertIn("exact phrase", result)

    def test_prefix_query_neutralized(self):
        # Trailing wildcard asterisk is stripped
        result = normalize_fts_query("soft*")
        self.assertNotIn("*", result)
        self.assertIn("soft", result)

    def test_near_operator_neutralized(self):
        # NEAR is an acronym, passes through as-is
        result = normalize_fts_query("python NEAR java")
        self.assertIsInstance(result, str)
        for ch in r'"()*\-/:;,?!@#$%^&+=<>{}|[]\\':
            self.assertNotIn(ch, result)

    def test_mixed_operators(self):
        # All special chars stripped; tokens extracted
        result = normalize_fts_query("(python OR java) AND test*")
        for ch in '()*"':
            self.assertNotIn(ch, result)
        self.assertNotIn("*", result)
        # meaningful tokens survive
        self.assertIn("python", result)
        self.assertIn("java", result)

    def test_no_special_chars_in_output(self):
        # For any input with FTS5 special chars, the output must NOT contain
        # any of the FTS5 special characters as standalone characters.
        inputs = [
            'python "java"',
            "python (AND java)",
            "pyth*",
            "title:python",
            "[secret]",
            "python|java",
            "python AND java",
            '"exact phrase"',
            "soft*",
            "python NEAR java",
            "(python OR java) AND test*",
        ]
        for raw in inputs:
            result = normalize_fts_query(raw)
            for ch in r'"()*\-/:;,?!@#$%^&+=<>{}|[]\\':
                self.assertNotIn(ch, result, f"input {raw!r}: char {ch!r} must not appear in output {result!r}")

    def test_uppercase_AND_stripped(self):
        result = normalize_fts_query("Cats AND Dogs")
        self.assertEqual(result, "cats dogs")

    def test_uppercase_OR_stripped(self):
        result = normalize_fts_query("Python OR Java")
        self.assertEqual(result, "python java")

    def test_uppercase_NOT_stripped(self):
        result = normalize_fts_query("Python NOT Java")
        self.assertEqual(result, "python java")

    def test_uppercase_NEAR_stripped(self):
        result = normalize_fts_query("Python NEAR Java")
        self.assertEqual(result, "python java")

    def test_lowercase_not_in_stop_words(self):
        result = normalize_fts_query("often not python")
        self.assertEqual(result, "often python")

    def test_mixed_case_And_treated_as_word(self):
        result = normalize_fts_query("Cats And Dogs")
        self.assertEqual(result, "cats dogs")

    def test_real_acronyms_still_preserved(self):
        result = normalize_fts_query("API AND SQL")
        self.assertEqual(result, "API SQL")


class TestExtractQueryIntent(unittest.TestCase):
    def test_extracts_all_caps_entity(self):
        entities, predicates = extract_query_intent("Who leads AFOMIS?")
        self.assertIn("AFOMIS", entities)

    def test_extracts_question_subject(self):
        entities, predicates = extract_query_intent("What is the mission of Task Force Alpha?")
        # Should capture some entity-like terms
        self.assertIsInstance(entities, list)

    def test_extracts_predicate_terms(self):
        _, predicates = extract_query_intent("who is the chief of staff for AFSOC?")
        self.assertIn("chief", predicates)

    def test_empty_query(self):
        entities, predicates = extract_query_intent("")
        self.assertEqual(entities, [])
        self.assertEqual(predicates, [])


class TestWikiEvidenceToDict(unittest.TestCase):
    def _make_evidence(self, **kwargs):
        defaults = dict(
            label_placeholder="W1",
            page_id=1,
            claim_id=10,
            title="Test Page",
            slug="test-page",
            page_type="entity",
            claim_text="The chief is Col Smith.",
            excerpt="",
            confidence=0.9,
            page_status="verified",
            claim_status="active",
            score=0.85,
            score_type="fts",
            freshness=None,
            source_count=2,
            provenance_summary="2 docs",
        )
        defaults.update(kwargs)
        return WikiEvidence(**defaults)

    def test_to_dict_has_wiki_label(self):
        ev = self._make_evidence()
        d = ev.to_dict()
        self.assertEqual(d["wiki_label"], "W1")

    def test_to_dict_status_prefers_claim_status(self):
        ev = self._make_evidence(claim_status="active", page_status="stale")
        d = ev.to_dict()
        self.assertEqual(d["status"], "active")

    def test_to_dict_status_falls_back_to_page_status(self):
        ev = self._make_evidence(claim_status=None, page_status="verified")
        d = ev.to_dict()
        self.assertEqual(d["status"], "verified")

    def test_to_dict_has_split_page_claim_status(self):
        ev = self._make_evidence(claim_status="verified", page_status="draft")
        d = ev.to_dict()
        self.assertIn("page_status", d)
        self.assertIn("claim_status", d)
        self.assertEqual(d["page_status"], "draft")
        self.assertEqual(d["claim_status"], "verified")


class TestWikiRetrievalServiceNullVault(unittest.TestCase):
    def test_returns_empty_for_none_vault(self):
        """retrieve() must return [] when vault_id is None (synchronous)."""
        pool = MagicMock()
        svc = WikiRetrievalService(pool=pool)
        result = svc.retrieve("test query", vault_id=None)
        self.assertEqual(result, [])
        # Pool.get() should never be called for None vault
        pool.get.assert_not_called()


class TestWikiRetrievalServiceFtsPageSearchThreshold(unittest.TestCase):
    """Regression tests for issue #101: the FTS page-search fallback threshold
    was hardcoded in wiki_retrieval._retrieve_sync. After the fix, the
    threshold is configurable via wiki_fts_page_search_max_candidates and
    flows through WikiRetrievalService.__init__.
    """

    def test_explicit_constructor_threshold_is_honored(self):
        pool = MagicMock()
        svc = WikiRetrievalService(pool=pool, fts_page_search_max_candidates=7)
        self.assertEqual(svc._fts_page_search_max_candidates, 7)

    def test_default_threshold_reads_from_settings(self):
        from app.config import settings

        pool = MagicMock()
        svc = WikiRetrievalService(pool=pool)
        self.assertEqual(
            svc._fts_page_search_max_candidates,
            settings.wiki_fts_page_search_max_candidates,
        )

    def test_zero_threshold_is_allowed_and_unclamped_to_zero(self):
        # 0 means "always run the FTS page-search fallback" — a valid
        # operator-controlled behavior, not a misconfiguration.
        pool = MagicMock()
        svc = WikiRetrievalService(pool=pool, fts_page_search_max_candidates=0)
        self.assertEqual(svc._fts_page_search_max_candidates, 0)

    def test_negative_threshold_is_clamped_to_zero(self):
        # Negative values are nonsensical; clamp to 0 (always-run).
        pool = MagicMock()
        svc = WikiRetrievalService(pool=pool, fts_page_search_max_candidates=-3)
        self.assertEqual(svc._fts_page_search_max_candidates, 0)

    def test_phase4_skipped_when_candidates_meet_threshold(self):
        """When the FTS claim search alone (phase 3) yields >= threshold
        candidates, the FTS page-search fallback (phase 4) must NOT run.

        We stand up a real FTS5 virtual table mirroring the production
        schema, run a real retrieve(), and assert via a spy on
        _fts_page_search that the spy was never called.
        """
        import tempfile
        from pathlib import Path
        from queue import Empty, Queue

        from app.models.database import init_db, run_migrations

        tmp = tempfile.mkdtemp()
        db = str(Path(tmp) / "app.db")
        init_db(db)
        run_migrations(db)

        class _Pool:
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

        try:
            pool = _Pool(db)
            conn = sqlite3.connect(db)
            try:
                # Use a unique vault id to avoid collisions across runs.
                # The production code in TestWikiRetrievalEndToEnd hardcodes
                # vault_id=1 and page_id=1, which is what causes the local
                # IntegrityError on stale test data; this test only needs
                # the FTS claim path to be populated, so any vault id works.
                conn.execute(
                    "INSERT INTO vaults (id, name) VALUES (?, ?)",
                    (7777, "ThresholdTest"),
                )
                for pid in (1, 2, 3):
                    conn.execute(
                        "INSERT INTO wiki_pages (id, vault_id, slug, title, "
                        "page_type, markdown, status) VALUES (?, 7777, ?, ?, "
                        "'overview', '# x', 'verified')",
                        (pid, f"page-{pid}", f"Page {pid}"),
                    )
                # Three FTS claim hits so phase 3 fills >= 3 candidates.
                # claim_id == page_id here is sufficient for this test —
                # the schema doesn't enforce a particular mapping.
                for cid, txt in (
                    (1, "zlorptanium reactor alpha"),
                    (2, "zlorptanium reactor beta"),
                    (3, "zlorptanium reactor gamma"),
                ):
                    conn.execute(
                        "INSERT INTO wiki_claims (id, vault_id, page_id, "
                        "claim_text, claim_type, source_type, status, "
                        "confidence) VALUES (?, 7777, ?, ?, 'fact', "
                        "'document', 'active', 0.9)",
                        (cid, cid, txt),
                    )
                conn.commit()
            finally:
                conn.close()

            # threshold=3: phase 3 yields 3 candidates, which meets the
            # threshold, so phase 4 must be skipped.
            svc = WikiRetrievalService(
                pool=pool, fts_page_search_max_candidates=3
            )
            phase4_calls = {"n": 0}

            def spy(*args, **kwargs):
                phase4_calls["n"] += 1
                return []

            svc._fts_page_search = spy
            results = svc.retrieve("zlorptanium reactor", vault_id=7777)
            self.assertGreaterEqual(
                len(results), 3, "phase 3 should yield at least 3 candidates"
            )
            self.assertEqual(
                phase4_calls["n"],
                0,
                "phase 4 must not run when candidates meet the threshold",
            )

            pool.close_all()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_phase4_runs_when_candidates_below_threshold(self):
        """Inverse of the above: with threshold=3 and only 1 candidate from
        phases 1-3, the FTS page-search fallback MUST run.
        """
        import tempfile
        from pathlib import Path
        from queue import Empty, Queue

        from app.models.database import init_db, run_migrations

        tmp = tempfile.mkdtemp()
        db = str(Path(tmp) / "app.db")
        init_db(db)
        run_migrations(db)

        class _Pool:
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

        try:
            pool = _Pool(db)
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "INSERT INTO vaults (id, name) VALUES (?, ?)",
                    (8888, "ThresholdTest2"),
                )
                # Single FTS claim hit; threshold=3 forces phase 4 to run.
                conn.execute(
                    "INSERT INTO wiki_pages (id, vault_id, slug, title, "
                    "page_type, markdown, status) VALUES (1, 8888, 'p', "
                    "'P', 'overview', '# x', 'verified')"
                )
                conn.execute(
                    "INSERT INTO wiki_claims (id, vault_id, page_id, "
                    "claim_text, claim_type, source_type, status, "
                    "confidence) VALUES (1, 8888, 1, "
                    "'zlorptanium reactor alpha', 'fact', 'document', "
                    "'active', 0.9)"
                )
                conn.commit()
            finally:
                conn.close()

            svc = WikiRetrievalService(
                pool=pool, fts_page_search_max_candidates=3
            )
            phase4_calls = {"n": 0}

            def spy(*args, **kwargs):
                phase4_calls["n"] += 1
                return []

            svc._fts_page_search = spy
            results = svc.retrieve("zlorptanium reactor", vault_id=8888)
            # Phase 3 contributes 1 candidate; phase 4 must be invoked to
            # try to fill the rest.
            self.assertGreaterEqual(len(results), 1)
            self.assertGreaterEqual(
                phase4_calls["n"],
                1,
                "phase 4 must run when candidates fall below threshold",
            )

            pool.close_all()
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestWikiRetrievalServiceEmptyDb(unittest.TestCase):
    def _make_service_with_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE wiki_claims (
                id INTEGER PRIMARY KEY, vault_id INTEGER, page_id INTEGER,
                claim_text TEXT, claim_type TEXT DEFAULT 'fact',
                status TEXT DEFAULT 'active', confidence REAL DEFAULT 0.8,
                predicate TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE wiki_pages (
                id INTEGER PRIMARY KEY, vault_id INTEGER, slug TEXT, title TEXT,
                page_type TEXT DEFAULT 'entity', status TEXT DEFAULT 'draft',
                confidence REAL DEFAULT 0.5, last_compiled_at TEXT
            );
            CREATE TABLE wiki_entities (
                id INTEGER PRIMARY KEY, vault_id INTEGER, canonical_name TEXT,
                entity_type TEXT DEFAULT 'organization', aliases_json TEXT DEFAULT '[]',
                page_id INTEGER
            );
            CREATE TABLE wiki_relations (
                id INTEGER PRIMARY KEY, vault_id INTEGER, subject_entity_id INTEGER,
                predicate TEXT, object_entity_id INTEGER, object_text TEXT,
                claim_id INTEGER, confidence REAL DEFAULT 0.8
            );
        """)
        conn.commit()

        pool = MagicMock()
        pool.get_connection.return_value = conn
        pool.release_connection = MagicMock()
        return WikiRetrievalService(pool=pool), conn

    def test_empty_db_returns_list(self):
        """retrieve() should return [] on empty DB (no FTS tables → graceful empty)."""
        svc, _ = self._make_service_with_conn()
        try:
            result = svc.retrieve("AFOMIS mission", vault_id=1)
            self.assertIsInstance(result, list)
        except Exception as e:
            # Acceptable: no FTS tables — expected graceful empty
            self.assertIn("no such table", str(e).lower())


class TestWikiRetrievalEndToEnd(unittest.TestCase):
    """Exercises the real FTS query + production pool interface.

    Regression guard for two bugs fixed together: (1) retrieve() must use the
    production pool's get_connection/release_connection, and (2) the FTS queries
    must reference the virtual table by name in MATCH (the aliased ``fts MATCH``
    form raises "no such column: fts" on this SQLite build).
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from queue import Empty, Queue

        from app.models.database import init_db, run_migrations

        self._tmp = tempfile.mkdtemp()
        db = str(Path(self._tmp) / "app.db")
        init_db(db)
        run_migrations(db)

        class _Pool:
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

        self._pool = _Pool(db)
        self.service = WikiRetrievalService(pool=self._pool)

        conn = sqlite3.connect(db)
        try:
            conn.execute("INSERT OR REPLACE INTO vaults (id, name) VALUES (1, 'V1')")
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, status) "
                "VALUES (1, 1, 'runbook', 'Runbook', 'overview', '# Runbook', 'verified')"
            )
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, claim_type, "
                "source_type, status, confidence) VALUES "
                "(1, 1, 1, 'The zlorptanium reactor must be cooled nightly.', 'fact', "
                "'document', 'active', 0.9)"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        import shutil

        self._pool.close_all()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fts_claim_search_returns_evidence(self):
        results = self.service.retrieve("zlorptanium", vault_id=1)
        self.assertTrue(results, "expected FTS claim match for 'zlorptanium'")
        self.assertEqual(results[0].label_placeholder, "W1")
        self.assertIn("zlorptanium", (results[0].claim_text or "").lower())

    def test_no_match_returns_empty(self):
        self.assertEqual(self.service.retrieve("nonexistentword", vault_id=1), [])


class TestEntityMismatchFilterExpandedMatch(unittest.TestCase):
    """Regression test for issue #102: entity mismatch filter over-rejects
    valid FTS claim results.

    Scenario: query "who is AFOMIS deputy chief?" extracts entity candidate
    "AFOMIS".  FTS finds a claim whose ``subject`` column is "AFOMIS" and
    ``claim_text`` mentions "deputy chief" — but the claim_text itself does
    NOT contain the literal "AFOMIS".  The entity's alias "Air Force Medical
    Information Systems" appears in the page title.  Before the fix, the
    claim was rejected because the filter only checked claim_text + title
    against raw entity candidates.  After the fix, the filter also checks
    the claim's subject/object fields and uses canonical names + aliases
    from matched entities.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from queue import Empty, Queue

        from app.models.database import init_db, run_migrations

        self._tmp = tempfile.mkdtemp()
        db = str(Path(self._tmp) / "app.db")
        init_db(db)
        run_migrations(db)

        class _Pool:
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

        self._pool = _Pool(db)
        self.service = WikiRetrievalService(pool=self._pool)

        conn = sqlite3.connect(db)
        try:
            # vault
            conn.execute(
                "INSERT INTO vaults (id, name) VALUES (?, ?)",
                (99, "EntityMismatchTest"),
            )
            # entity page
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, "
                "markdown, status) VALUES (10, 99, 'afomis', 'AFOMIS', "
                "'entity', '# AFOMIS', 'verified')"
            )
            # AFOMIS entity with an alias
            conn.execute(
                "INSERT INTO wiki_entities (id, vault_id, canonical_name, "
                "entity_type, aliases_json, page_id) VALUES "
                "(1, 99, 'AFOMIS', 'organization', "
                "'[\"Air Force Medical Information Systems\"]', 10)"
            )
            # claim page — title does NOT contain "AFOMIS"
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, "
                "markdown, status) VALUES (11, 99, 'personnel', "
                "'Personnel Roster', "
                "'entity', '# Personnel', 'verified')"
            )
            # Claim: subject="AFOMIS" (FTS matches this), claim_text has
            # "deputy chief" but NOT "AFOMIS".
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, "
                "subject, predicate, object, "
                "claim_type, source_type, status, confidence) VALUES "
                "(20, 99, 11, 'Major General Justin Woods serves as deputy chief.', "
                "'AFOMIS', 'has_deputy_chief', 'Justin Woods', "
                "'fact', 'document', 'active', 0.9)"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        import shutil

        self._pool.close_all()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fts_claim_passes_when_subject_contains_entity(self):
        """FTS finds a claim via subject column; the entity mismatch filter
        must accept it because the claim's subject matches the entity."""
        results = self.service.retrieve(
            "who is AFOMIS deputy chief?", vault_id=99
        )
        claim_results = [r for r in results if r.claim_id == 20]
        self.assertTrue(
            claim_results,
            "FTS claim should pass entity mismatch filter when claim "
            "subject matches entity (issue #102)",
        )

    def test_fts_claim_rejected_when_no_match_at_all(self):
        """A claim whose text, subject, and object contain none of the
        entity names/aliases should still be rejected."""
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, "
                "subject, predicate, object, "
                "claim_type, source_type, status, confidence) VALUES "
                "(21, 99, 11, 'The weather is sunny today.', "
                "'WeatherService', 'reports', 'sunny', "
                "'fact', 'document', 'active', 0.7)"
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        results = self.service.retrieve(
            "who is AFOMIS deputy chief?", vault_id=99
        )
        # Claim 21 should NOT appear
        claim_21 = [r for r in results if r.claim_id == 21]
        self.assertEqual(
            claim_21,
            [],
            "unrelated claim should be filtered out by entity mismatch",
        )


class TestWikiRetrievalEntityAndRelationPipeline(unittest.TestCase):
    """End-to-end coverage for the previously-untested retrieval phases
    (issue #99): entity exact match (canonical + alias), relation lookup with
    predicate scoring, exact-entity page evidence, batched claim provenance
    (issue #276 E1-3), and the multi-candidate ranking/sort.

    Also serves as the regression for issue #276 A6-3 (claim-status predicate):
    a 'superseded' claim must never reach the result list.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from queue import Empty, Queue

        from app.models.database import init_db, run_migrations

        self._tmp = tempfile.mkdtemp()
        db = str(Path(self._tmp) / "app.db")
        init_db(db)
        run_migrations(db)

        class _Pool:
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

        self._pool = _Pool(db)
        self.service = WikiRetrievalService(pool=self._pool)

        conn = sqlite3.connect(db)
        try:
            conn.execute("INSERT OR REPLACE INTO vaults (id, name) VALUES (1, 'V1')")
            # A page that the entity points at (exact-entity page evidence path).
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, slug, title, page_type, markdown, "
                "summary, status, confidence) VALUES "
                "(1, 1, 'afomis', 'AFOMIS', 'entity', '# AFOMIS', "
                "'Armed Forces Operations', 'verified', 0.9)"
            )
            # Entity with an alias and a linked page. The alias is an ALL-CAPS
            # token so extract_query_intent surfaces it as an entity candidate
            # (the extractor only treats ALL-CAPS 2+ char tokens as candidates),
            # which is what exercises the json_each alias-lookup branch.
            conn.execute(
                "INSERT INTO wiki_entities (id, vault_id, canonical_name, entity_type, "
                "aliases_json, page_id) VALUES "
                "(1, 1, 'AFOMIS', 'organization', '[\"AFO\"]', 1)"
            )
            # An active claim backing a relation. The predicate is 'director'
            # (a member of _PREDICATE_TERMS) so a query containing 'director'
            # exercises the +0.1 boost branch of predicate scoring.
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, subject, "
                "predicate, object, claim_type, source_type, status, confidence) VALUES "
                "(10, 1, 1, 'AFOMIS has a director.', 'AFOMIS', 'director', "
                "'operations', 'fact', 'document', 'active', 0.85)"
            )
            # A superseded claim about the same entity — must be EXCLUDED (A6-3).
            conn.execute(
                "INSERT INTO wiki_claims (id, vault_id, page_id, claim_text, subject, "
                "predicate, object, claim_type, source_type, status, confidence) VALUES "
                "(11, 1, 1, 'AFOMIS was disbanded.', 'AFOMIS', 'status', 'disbanded', "
                "'fact', 'document', 'superseded', 0.5)"
            )
            # Relation: AFOMIS -director-> operations, backed by the active claim.
            conn.execute(
                "INSERT INTO wiki_relations (id, vault_id, subject_entity_id, predicate, "
                "object_text, claim_id, confidence) VALUES "
                "(1, 1, 1, 'director', 'operations', 10, 0.9)"
            )
            # Provenance sources for the active claim (E1-3 batch path).
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind, file_id) VALUES "
                "(10, 'document', 7)"
            )
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind, memory_id) VALUES "
                "(10, 'memory', 42)"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        import shutil
        self._pool.close_all()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_entity_exact_match_canonical_resolves_and_returns_relation(self):
        """Phase 1 canonical-name match feeds Phase 2 relation lookup."""
        results = self.service.retrieve("AFOMIS", vault_id=1)
        # The active claim (10) should appear via the relation path.
        claim_ids = {r.claim_id for r in results}
        self.assertIn(10, claim_ids, "canonical entity match must surface the active relation claim")
        # The superseded claim (11) must NEVER appear (A6-3 regression).
        self.assertNotIn(11, claim_ids, "superseded claim must be excluded by status predicate")

    def test_entity_exact_match_alias_resolves(self):
        """Phase 1 alias lookup via json_each resolves the entity from its alias.

        Uses the ALL-CAPS alias 'AFO' so extract_query_intent surfaces it as an
        entity candidate; the canonical-name branch won't match 'AFO', so the
        only way the entity resolves is via the json_each(alias) lookup.
        """
        results = self.service.retrieve("AFO", vault_id=1)
        claim_ids = {r.claim_id for r in results}
        self.assertIn(
            10, claim_ids,
            "alias lookup must resolve the entity and surface its relation claim",
        )

    def test_relation_lookup_predicate_match_boosts_score(self):
        """Phase 2 predicate scoring: a matching predicate adds +0.1 and records matched_predicate."""
        results = self.service.retrieve("AFOMIS director", vault_id=1)
        rel = next((r for r in results if r.claim_id == 10), None)
        self.assertIsNotNone(rel, "relation claim must be returned")
        self.assertEqual(rel.score_type, "relation")
        self.assertEqual(rel.matched_predicate, "director")
        # base 0.85 + 0.1 boost
        self.assertAlmostEqual(rel.score, 0.95, places=5)

    def test_relation_lookup_predicate_miss_penalizes_score(self):
        """Phase 2 predicate scoring: a query predicate that does not match the
        relation's predicate subtracts 0.15. 'chief' is in _PREDICATE_TERMS but
        the relation predicate is 'director', so it counts as a miss."""
        results = self.service.retrieve("AFOMIS chief", vault_id=1)
        rel = next((r for r in results if r.claim_id == 10), None)
        self.assertIsNotNone(rel, "relation claim must be returned even when predicate misses")
        self.assertIsNone(rel.matched_predicate)
        # base 0.85 - 0.15 penalty
        self.assertAlmostEqual(rel.score, 0.70, places=5)

    def test_get_page_evidence_for_matched_entity(self):
        """The exact-entity page branch produces page evidence with score_type='exact_entity'."""
        results = self.service.retrieve("AFOMIS", vault_id=1)
        page_ev = [r for r in results if r.page_id == 1 and r.claim_id is None]
        self.assertTrue(
            any(r.score_type == "exact_entity" for r in page_ev),
            "matched entity with page_id must yield exact_entity page evidence",
        )

    def test_claim_provenance_batched_populates_source_count(self):
        """E1-3 batched provenance: source_count and provenance_summary are populated."""
        results = self.service.retrieve("AFOMIS director", vault_id=1)
        rel = next((r for r in results if r.claim_id == 10), None)
        self.assertIsNotNone(rel)
        # Two sources: one document, one memory.
        self.assertEqual(rel.source_count, 2)
        self.assertIn("doc", rel.provenance_summary)
        self.assertIn("memory", rel.provenance_summary)

    def test_ranking_relation_predicate_ranks_before_exact_entity(self):
        """Ranking/sort (lines 335-340): relation+predicate match outranks exact_entity."""
        # Query 'AFOMIS director' produces BOTH a relation claim (predicate match)
        # and the exact-entity page evidence. The relation must sort first.
        results = self.service.retrieve("AFOMIS director", vault_id=1)
        self.assertGreaterEqual(len(results), 2)
        top = results[0]
        self.assertEqual(top.score_type, "relation")
        self.assertEqual(top.matched_predicate, "director")

    def test_superseded_claim_excluded_from_fts_search(self):
        """A6-3 regression: a superseded claim must not surface via FTS even when
        its text matches the query."""
        results = self.service.retrieve("disbanded", vault_id=1)
        claim_ids = {r.claim_id for r in results}
        self.assertNotIn(
            11, claim_ids,
            "superseded claim must be filtered out of FTS claim search by status predicate",
        )


if __name__ == "__main__":
    unittest.main()
