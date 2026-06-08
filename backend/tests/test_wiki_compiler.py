"""Tests for WikiCompiler deterministic extraction and memory promotion."""

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_env():
    from app.models.database import run_migrations
    from app.services.wiki_compiler import WikiCompiler
    from app.services.wiki_store import WikiStore

    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Test')")
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler


AFOMIS_TEXT = (
    "AFOMIS stands for Air Force Operational Medicine Information Systems. "
    "Justice Sakyi is the AFOMIS Chief and Major Justin Woods is his deputy."
)


class TestDeterministicExtraction(unittest.TestCase):

    def test_acronym_extracted(self):
        from app.services.wiki_compiler import extract_entities_from_text

        result = extract_entities_from_text(AFOMIS_TEXT)
        acronyms = {a["acronym"] for a in result.acronyms}
        self.assertIn("AFOMIS", acronyms)

    def test_full_name_extracted(self):
        from app.services.wiki_compiler import extract_entities_from_text

        result = extract_entities_from_text(AFOMIS_TEXT)
        full_names = {a["full_name"] for a in result.acronyms}
        self.assertTrue(
            any("Air Force Operational Medicine" in fn for fn in full_names),
            f"Expected full name in: {full_names}",
        )

    def test_person_entities_extracted(self):
        from app.services.wiki_compiler import extract_entities_from_text

        result = extract_entities_from_text(AFOMIS_TEXT)
        persons = set(result.persons)
        self.assertTrue(
            any("Sakyi" in p for p in persons),
            f"Justice Sakyi not found in {persons}",
        )
        self.assertTrue(
            any("Woods" in p for p in persons),
            f"Major Justin Woods not found in {persons}",
        )

    def test_role_claims_extracted(self):
        from app.services.wiki_compiler import extract_entities_from_text

        result = extract_entities_from_text(AFOMIS_TEXT)
        self.assertGreater(len(result.role_claims), 0)
        predicates = {rc["predicate"].lower() for rc in result.role_claims}
        self.assertTrue(any("chief" in p for p in predicates), f"chief not in {predicates}")

    def test_pronoun_resolved_via_org_context(self):
        from app.services.wiki_compiler import extract_entities_from_text

        result = extract_entities_from_text(AFOMIS_TEXT)
        deputy_claims = [
            rc for rc in result.role_claims if "deputy" in rc["predicate"].lower()
        ]
        self.assertGreater(len(deputy_claims), 0, "No deputy claim created")
        deputy_claim = deputy_claims[0]
        # Subject should be AFOMIS (resolved from org context)
        self.assertEqual(deputy_claim["subject"], "AFOMIS")


class TestPromoteMemory(unittest.TestCase):

    def setUp(self):
        self.conn, self.store, self.compiler = _make_env()

    def tearDown(self):
        self.conn.close()

    def _insert_memory(self, content: str, vault_id: int | None = 1) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
            (content, vault_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_afomis_promotion_creates_page(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        self.assertIsNotNone(result["page"])
        self.assertIn("afomis", result["page"].slug.lower())

    def test_afomis_promotion_creates_entities(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        entity_names = {e.canonical_name for e in result["entities"]}
        self.assertIn("AFOMIS", entity_names)

    def test_afomis_promotion_creates_person_entities(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        entity_names = {e.canonical_name for e in result["entities"]}
        self.assertTrue(
            any("Sakyi" in n for n in entity_names),
            f"Justice Sakyi not in {entity_names}",
        )
        self.assertTrue(
            any("Woods" in n for n in entity_names),
            f"Major Justin Woods not in {entity_names}",
        )

    def test_afomis_promotion_creates_claims(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        self.assertGreater(len(result["claims"]), 0)

    def test_afomis_promotion_claims_have_provenance(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        for claim in result["claims"]:
            self.assertGreater(
                len(claim.sources), 0,
                f"Claim {claim.id} '{claim.claim_text}' has no sources",
            )
            src = claim.sources[0]
            self.assertEqual(src.source_kind, "memory")
            self.assertEqual(src.memory_id, mem_id)

    def test_afomis_promotion_creates_relations(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        self.assertGreater(len(result["relations"]), 0)

    def test_promote_creates_chief_relation(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        predicates = {r.predicate.lower() for r in result["relations"]}
        self.assertTrue(any("chief" in p for p in predicates), f"chief not in {predicates}")

    def test_promote_creates_deputy_relation(self):
        mem_id = self._insert_memory(AFOMIS_TEXT)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        predicates = {r.predicate.lower() for r in result["relations"]}
        self.assertTrue(any("deputy" in p for p in predicates), f"deputy not in {predicates}")

    def test_vault_scope_enforcement(self):
        """Memory with vault_id=2 must not be promoted to vault_id=1."""
        # Insert memory in a different vault
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'Other')")
        self.conn.commit()
        mem_id = self._insert_memory(AFOMIS_TEXT, vault_id=2)
        with self.assertRaises(PermissionError):
            self.compiler.promote_memory(memory_id=mem_id, vault_id=1)

    def test_promote_null_vault_memory_uses_request_vault(self):
        """Memory with vault_id=NULL should be promotable to any vault."""
        mem_id = self._insert_memory(AFOMIS_TEXT, vault_id=None)
        result = self.compiler.promote_memory(memory_id=mem_id, vault_id=1)
        self.assertEqual(result["page"].vault_id, 1)

    def test_promote_nonexistent_memory_raises(self):
        with self.assertRaises(ValueError):
            self.compiler.promote_memory(memory_id=99999, vault_id=1)


class TestBuildCuratorChunks(unittest.TestCase):
    """Coverage for ``WikiCompiler._build_curator_chunks``.

    The curator path is feature-flag gated and off by default, but the
    chunk_id format MUST match the deterministic pass
    (``f"{file_id}_{idx}"``) so that quote verification against
    ``chunk_id`` succeeds in the curator. Any divergence here is a
    silent break of the curator's correctness contract — these tests
    pin the chunking invariants.
    """

    def _build(self, text, file_id):
        from app.services.wiki_compiler import WikiCompiler

        return WikiCompiler._build_curator_chunks(text=text, file_id=file_id)

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(self._build("", file_id=1), [])

    def test_whitespace_only_text_still_yields_one_chunk(self):
        """A whitespace-only ``text`` is still truthy, so the
        ``if not text`` guard does not short-circuit it. The slicer
        runs and yields a single small chunk — pinning this so a
        future 'normalize whitespace' guard can't silently drop
        legitimate single-chunk input."""
        chunks = self._build("   \n\t", file_id=1)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source_text, "   \n\t")
        self.assertEqual(chunks[0].chunk_id, "1_0")

    def test_text_exactly_one_chunk(self):
        """A 2 000-character text is a single chunk whose chunk_id is
        ``"{file_id}_0"`` and whose source_text is the full input."""
        from app.services.wiki_compiler import _COMPILE_CHUNK_SIZE
        from app.services.wiki_curator import CuratorChunk

        text = "a" * _COMPILE_CHUNK_SIZE
        chunks = self._build(text, file_id=42)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(
            chunks[0],
            CuratorChunk(
                chunk_id="42_0",
                source_text=text,
                file_id=42,
                source_label="file:42",
            ),
        )

    def test_chunk_id_format_with_file_id(self):
        """chunk_id must be ``"{file_id}_{idx}"`` — the format the
        deterministic pass also uses — for file-backed sources."""
        text = "x" * 5000  # 3 chunks of 2000/2000/1000
        chunks = self._build(text, file_id=7)

        self.assertEqual([c.chunk_id for c in chunks], ["7_0", "7_1", "7_2"])

    def test_chunk_id_format_without_file_id(self):
        """When ``file_id`` is None, chunk_id must be ``"text_{idx}"``
        and ``source_label`` must be ``None`` (no ``file:None`` leak)."""
        text = "y" * 4500  # 3 chunks
        chunks = self._build(text, file_id=None)

        self.assertEqual([c.chunk_id for c in chunks], ["text_0", "text_1", "text_2"])
        for c in chunks:
            self.assertIsNone(c.file_id)
            self.assertIsNone(c.source_label)

    def test_source_label_matches_file_id(self):
        """For file-backed sources, source_label is ``"file:{file_id}"``."""
        chunks = self._build("z" * 100, file_id=99)
        self.assertEqual(chunks[0].source_label, "file:99")
        self.assertEqual(chunks[0].file_id, 99)

    def test_window_size_never_exceeds_chunk_size(self):
        """Each window is bounded by ``_COMPILE_CHUNK_SIZE`` even when
        the text is much longer. The tail window may be shorter; no
        window may exceed 2 000 chars. Concat of windows must equal
        the bound_text (which is itself ``text[:min(len(text), budget)]``,
        so we derive the expected bound dynamically)."""
        from app.config import settings
        from app.services.wiki_compiler import _COMPILE_CHUNK_SIZE

        original = settings.wiki_llm_curator_max_input_chars
        try:
            # Force a known budget that we can compute against.
            settings.wiki_llm_curator_max_input_chars = 6000
            budget = max(_COMPILE_CHUNK_SIZE, 6000)  # 6000

            text = "A" * 10_000  # > budget
            chunks = self._build(text, file_id=1)

            self.assertGreater(len(chunks), 1)
            for c in chunks:
                self.assertLessEqual(len(c.source_text), _COMPILE_CHUNK_SIZE)
            # Concat of windows must equal bound_text exactly.
            self.assertEqual(
                "".join(c.source_text for c in chunks),
                text[:budget],
            )
        finally:
            settings.wiki_llm_curator_max_input_chars = original

    def test_sentence_spanning_2000_char_boundary(self):
        """A sentence that straddles the 2 000-char window boundary
        must be split across two consecutive chunks (no special
        boundary detection — this pins the 'naive fixed-window' contract
        so a future 'smart' boundary heuristic can't silently shift
        chunk boundaries and break quote verification)."""
        from app.services.wiki_compiler import _COMPILE_CHUNK_SIZE

        # Build text where a long sentence starts at 1 999 and continues.
        prefix = "p" * 1999
        sentence = "The quick brown fox jumps over the lazy dog. " * 50
        text = prefix + sentence

        chunks = self._build(text, file_id=1)
        self.assertGreaterEqual(len(chunks), 3)

        # The first window ends mid-sentence (last char is 'e' from "prefix...e").
        first_window = chunks[0].source_text
        # Find the 'p' run end — first non-'p' char in the window marks
        # the sentence start, which is INSIDE window 0.
        first_non_prefix = next(
            (i for i, ch in enumerate(first_window) if ch != "p"), len(first_window)
        )
        # Sentence begins at offset 1999 (the last char of window 0).
        self.assertEqual(first_non_prefix, 1999)
        # That character must be the first char of "The quick...".
        self.assertEqual(first_window[1999], "T")
        # And the next character (start of window 1) is the second char.
        self.assertEqual(chunks[1].source_text[0], "h")

    def test_repeated_sentences_yield_repeated_chunks(self):
        """Repeating the same sentence N times produces N identical
        chunk contents in order — determinism guard."""
        sentence = "AFOMIS stands for Air Force Operational Medicine Information Systems. "
        text = sentence * 4
        chunks = self._build(text, file_id=1)

        joined = "".join(c.source_text for c in chunks)
        self.assertEqual(joined[: len(sentence)], sentence)
        # All chunk_ids present and unique.
        self.assertEqual([c.chunk_id for c in chunks], sorted({c.chunk_id for c in chunks}))

    def test_non_ascii_text_is_preserved_verbatim(self):
        """Non-ASCII (CJK, emoji, accented) text is passed through
        unchanged — no encoding normalization that would shift
        char offsets and break chunk_id↔offset correlation.
        Slicing operates on Python str length (codepoint count)."""
        from app.services.wiki_compiler import _COMPILE_CHUNK_SIZE

        # CJK paragraph exactly _COMPILE_CHUNK_SIZE codepoints long.
        text = "語" * _COMPILE_CHUNK_SIZE
        chunks = self._build(text, file_id=1)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source_text, text)
        self.assertEqual(chunks[0].chunk_id, "1_0")

        # Emoji: each 🎉 is one Python str codepoint. 5000 codepoints,
        # budget default 6000 — bound_text is the full 5000 chars.
        # 5000 / 2000 = 2 full windows of 2000 + 1 tail of 1000.
        text2 = "🎉" * 5000
        chunks2 = self._build(text2, file_id=2)
        self.assertEqual(len(chunks2), 3)
        self.assertEqual(len(chunks2[0].source_text), 2000)
        self.assertEqual(len(chunks2[1].source_text), 2000)
        self.assertEqual(len(chunks2[2].source_text), 1000)
        # Join reconstructs bound_text exactly.
        self.assertEqual("".join(c.source_text for c in chunks2), text2)
        # No codepoint corruption.
        for c in chunks2:
            self.assertGreater(len(c.source_text), 0)

    def test_budget_cap_respects_settings_max_input(self):
        """``bound_text`` is bounded by ``max(_COMPILE_CHUNK_SIZE, max_input)``.
        When ``wiki_llm_curator_max_input_chars`` is small, fewer chunks
        are produced and the total source_text length is bounded by it."""
        from app.config import settings
        from app.services.wiki_compiler import _COMPILE_CHUNK_SIZE

        original = settings.wiki_llm_curator_max_input_chars
        try:
            # Cap at 5 000 chars -> exactly 3 windows of 2000/2000/1000.
            settings.wiki_llm_curator_max_input_chars = 5000
            text = "q" * 50_000
            chunks = self._build(text, file_id=1)
            total = sum(len(c.source_text) for c in chunks)
            self.assertEqual(total, 5000)
            # 5 000 / 2 000 == 2.5 -> 3 chunks.
            self.assertEqual(len(chunks), 3)
            self.assertEqual(
                [c.chunk_id for c in chunks],
                ["1_0", "1_1", "1_2"],
            )
            # Last window is the tail (shorter than _COMPILE_CHUNK_SIZE).
            self.assertLess(len(chunks[-1].source_text), _COMPILE_CHUNK_SIZE)
        finally:
            settings.wiki_llm_curator_max_input_chars = original

    def test_budget_floor_is_chunk_size(self):
        """``budget = max(_COMPILE_CHUNK_SIZE, max_input)``: a
        pathological ``max_input`` smaller than 2 000 must not
        under-budget the slicer (would yield zero chunks for a 2 000
        char input)."""
        from app.config import settings

        original = settings.wiki_llm_curator_max_input_chars
        try:
            settings.wiki_llm_curator_max_input_chars = 100  # << 2 000
            chunks = self._build("r" * 2000, file_id=1)
            # Floor protects us — we still get the one full chunk.
            self.assertEqual(len(chunks), 1)
            self.assertEqual(len(chunks[0].source_text), 2000)
        finally:
            settings.wiki_llm_curator_max_input_chars = original

    def test_chunk_id_indices_are_contiguous_from_zero(self):
        """The numeric suffix in chunk_id runs 0..N-1 with no gaps —
        required so a downstream consumer iterating ``chunk_id`` parses
        can rely on a dense index sequence."""
        text = "s" * 6500  # 4 chunks of 2000/2000/2000/500
        chunks = self._build(text, file_id=5)
        indices = [int(c.chunk_id.split("_")[1]) for c in chunks]
        self.assertEqual(indices, list(range(len(chunks))))
