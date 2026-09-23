"""Contract tests for the deterministic retrieval gold corpus (issue #658).

Four families, mirroring the draft-room gold-corpus contract shape:

* ``RetrievalGoldManifestTests`` - manifest/fixture parity under the
  declared ``crlf-to-lf`` normalization policy (the policy that makes the
  corpus hold identically on LF and CRLF checkouts).
* ``RetrievalGoldCaseIntegrityTests`` - case-set structure, verbatim spans,
  real-text traps, the probe-case shared-offset property, and the
  cross-document chunk tie guard.
* ``RetrievalGoldLoaderTests`` - typed loader contract plus the
  ``test_tamper_*`` tamper-sensitivity proofs (selectable via ``-k tamper``).
* ``RetrievalDiscriminationTests`` - runs the repo's real retrieval path
  (real LanceDB ``VectorStore`` with dense + BM25 FTS hybrid search, real
  ``rrf_fuse``, real ``DocumentRetrievalService.filter_relevant``) over the
  corpus with a deterministic token-hash embedding injected at the
  ``embedding_service`` seam (selectable via ``-k discrimination``).

The deterministic embedding replaces only the network-bound embedding
provider. It is content-correlated (token-hash bag-of-words) so retrieval
discrimination is meaningful offline; the corpus docs state what this does
and does not prove. Reranking and query transformation are pinned off
(``patch.object``), and the family asserts the lexical (FTS) leg actually
participated rather than silently degrading to dense-only.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.retrieval_gold.gold_corpus import (  # noqa: E402
    CASE_KINDS,
    SCALE_LIMITS,
    RetrievalGoldCorpus,
    RetrievalGoldCorpusError,
    chunk_text,
    load_corpus,
)

FIXTURES_DIR = Path(
    os.environ.get("RETRIEVAL_GOLD_ROOT", BACKEND_ROOT / "tests" / "fixtures" / "retrieval_gold")
)

EMBED_DIM = 256
HYBRID_ALPHA = 0.6
DIRECT_TOP_K = 10
ENGINE_TOP_K = 8

MIN_CASES = 20
MAX_CASES = 50

_TOKEN_SPLIT = re.compile(r"[^0-9a-zA-Z]+")

# Fixed English stopword list (part of the documented tokenizer policy):
# removing function words keeps unrelated text near-orthogonal in the
# token-hash embedding space, which is what lets the production 0.75 cosine
# cutoff separate in-corpus answers from out-of-domain queries.
_STOPWORDS = frozenset(
    """
    a an the is are was were be been being of in on at to for from by with as
    and or not no nor does do did done what which who whom whose when where
    why how this that these those it its they them their we you your our ours
    i me my he she his her hers him us can could should would may might must
    shall will has have had than then so if about into over under between
    each all any some such only own same too very just also up down out off
    again more most other versus vs
    """.split()
)


def _tokens(text):
    """Documented tokenizer: split on non-alphanumeric runs, casefold, drop
    empties and the fixed stopword list above."""
    return [
        token
        for token in _TOKEN_SPLIT.split(text.lower())
        if token and token not in _STOPWORDS
    ]


def _embed_text(text):
    """Deterministic, content-correlated token-hash embedding (L2-normalized)."""
    vector = [0.0] * EMBED_DIM
    for token in _tokens(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBED_DIM
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        vector[0] = 1.0
        norm = 1.0
    return [value / norm for value in vector]


class _DeterministicEmbeddingService:
    """Embedding-service stand-in recording calls; optional per-query overrides."""

    def __init__(self, overrides=None):
        self.calls = []
        self._overrides = dict(overrides or {})

    async def embed_single(self, text):
        self.calls.append(text)
        return self._overrides.get(text, _embed_text(text))

    async def embed_passage(self, text):
        return await self.embed_single(text)


class RetrievalGoldManifestTests(unittest.TestCase):
    """Manifest/fixture parity, independent of the loader (a loader bug must
    not be able to mask a broken corpus)."""

    @classmethod
    def setUpClass(cls):
        with (FIXTURES_DIR / "manifest.json").open("r", encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        cls.texts = {}
        for entry in cls.manifest["documents"]:
            raw = (FIXTURES_DIR / entry["path"]).read_bytes()
            cls.texts[entry["path"]] = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def test_every_document_sha256_matches_the_normalized_bytes(self):
        for entry in self.manifest["documents"]:
            with self.subTest(document=entry["path"]):
                text = self.texts[entry["path"]]
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                self.assertEqual(entry["sha256"], digest)
                self.assertEqual(entry["char_length"], len(text))

    def test_manifest_declares_the_crlf_to_lf_normalization_policy(self):
        self.assertEqual(self.manifest["normalization"]["policy"], "crlf-to-lf")

    def test_corpus_loads_identically_when_fixtures_are_crlf_rewritten(self):
        """The policy is what makes the suite CRLF-checkout-proof: rewriting
        every fixture to CRLF must not change any hash or break loading."""
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        shutil.copytree(FIXTURES_DIR, scratch / "corpus")
        for doc_file in (scratch / "corpus").iterdir():
            if doc_file.suffix in (".md", ".txt"):
                with doc_file.open("rb") as fh:
                    raw = fh.read()
                rewritten = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                doc_file.write_bytes(rewritten)
        corpus = load_corpus(scratch / "corpus")
        self.assertEqual(len(corpus.documents), len(self.manifest["documents"]))

    def test_document_paths_are_unique_and_relative(self):
        paths = [entry["path"] for entry in self.manifest["documents"]]
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            self.assertFalse(Path(path).is_absolute())
            self.assertNotIn("..", Path(path).parts)

    def test_every_document_is_plain_text_within_the_word_band(self):
        for entry in self.manifest["documents"]:
            with self.subTest(document=entry["path"]):
                text = self.texts[entry["path"]]
                self.assertTrue(text.isascii())
                words = len(text.split())
                self.assertGreaterEqual(words, 300)
                self.assertLessEqual(words, 1500)

    def test_no_fixture_contains_secrets_or_external_addresses(self):
        forbidden = ("http://", "https://", "password", "secret", "api_key", "@")
        for entry in self.manifest["documents"]:
            with self.subTest(document=entry["path"]):
                text = self.texts[entry["path"]].lower()
                for needle in forbidden:
                    self.assertNotIn(needle, text)
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "\n".join(self.texts.values())):
            self.fail(f"IP address pattern in corpus: {ip}")


class RetrievalGoldCaseIntegrityTests(unittest.TestCase):
    """Case-set structure: counts, trap floors, span rules, tie guard."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(FIXTURES_DIR)

    def test_case_count_is_within_the_issue_band(self):
        self.assertGreaterEqual(len(self.corpus.cases), MIN_CASES)
        self.assertLessEqual(len(self.corpus.cases), MAX_CASES)

    def test_case_ids_are_unique(self):
        ids = [case.id for case in self.corpus.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_trap_cases_reach_the_one_third_floor(self):
        traps = self.corpus.trap_cases
        self.assertGreaterEqual(len(traps), len(self.corpus.cases) / 3)

    def test_trap_span_cases_are_the_clear_majority_of_traps(self):
        traps = self.corpus.trap_cases
        with_span = [case for case in traps if case.trap_span is not None]
        self.assertGreater(len(with_span) * 2, len(traps))

    def test_every_expected_span_is_verbatim_in_its_document(self):
        for case in self.corpus.cases:
            if case.kind == "not_in_corpus":
                continue
            with self.subTest(case=case.id):
                text = self.corpus.text(case.expected_doc)
                self.assertEqual(
                    text[case.expected_span.start : case.expected_span.end],
                    case.expected_span.text,
                )
                self.assertEqual(
                    case.expected_span.sha256,
                    hashlib.sha256(case.expected_span.text.encode("utf-8")).hexdigest(),
                )

    def test_every_trap_span_is_real_text_in_its_own_document(self):
        for case in self.corpus.trap_cases:
            if case.trap_span is None:
                continue
            with self.subTest(case=case.id):
                text = self.corpus.text(case.trap_span.document)
                self.assertEqual(
                    text[case.trap_span.start : case.trap_span.end], case.trap_span.text
                )
                self.assertNotEqual(case.trap_span.text, case.expected_span.text)
                self.assertNotIn(case.expected_span.text, text)

    def test_trap_documents_differ_from_expected_documents(self):
        for case in self.corpus.trap_cases:
            for trap in case.must_not_match:
                self.assertNotEqual(trap, case.expected_doc, case.id)

    def test_probe_case_trap_document_carries_the_span_at_identical_offsets(self):
        """Contract-guarantees the frozen ranking-degradation probe's swap is
        loader-clean: the first trap case's expected span exists in its trap
        document at the SAME offsets."""
        probe = self.corpus.probe_case()
        trap_text = self.corpus.text(probe.must_not_match[0])
        self.assertEqual(
            trap_text.find(probe.expected_span.text), probe.expected_span.start, probe.id
        )

    def test_at_least_one_not_in_corpus_case_exists(self):
        self.assertTrue(self.corpus.cases_by_kind("not_in_corpus"))

    def test_all_case_kinds_are_within_the_declared_vocabulary(self):
        for case in self.corpus.cases:
            self.assertIn(case.kind, CASE_KINDS, case.id)

    def test_every_span_bearing_paragraph_fits_one_chunk_unit_at_both_scales(self):
        targets = []  # (case, document, span_text)
        for case in self.corpus.cases:
            if case.kind == "not_in_corpus":
                continue
            targets.append((case, case.expected_doc, case.expected_span.text))
            if case.trap_span is not None:
                targets.append((case, case.trap_span.document, case.trap_span.text))
        for case, document, span_text in targets:
            text = self.corpus.text(document)
            with self.subTest(case=case.id, document=document):
                for limit in SCALE_LIMITS.values():
                    units = chunk_text(text, limit)
                    self.assertTrue(
                        any(span_text in unit for unit in units),
                        f"span of {case.id} spans multiple {limit}-char chunk units",
                    )

    def test_no_chunk_unit_is_text_identical_across_documents(self):
        """Tie guard: identical chunk units would make ranking assertions
        tie-break dependent; the corpus forbids them at both scales."""
        seen = {}
        for document in self.corpus.documents:
            for scale, limit in SCALE_LIMITS.items():
                for unit in chunk_text(document.text, limit):
                    # A unit may legitimately repeat across the two scales of
                    # the SAME document (different chunk ids); only
                    # cross-document identity is a tie.
                    if unit in seen:
                        self.assertEqual(
                            seen[unit],
                            document.path,
                            f"tie guard: unit shared across {seen[unit]} and {document.path}",
                        )
                    seen[unit] = document.path


class RetrievalGoldLoaderTests(unittest.TestCase):
    """Typed loader contract and tamper sensitivity (``-k tamper``)."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(FIXTURES_DIR)

    def _scratch_copy(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        shutil.copytree(FIXTURES_DIR, scratch / "corpus")
        return scratch / "corpus"

    @staticmethod
    def _rewrite_manifest(corpus_dir, mutate):
        manifest_path = corpus_dir / "manifest.json"
        with manifest_path.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        mutate(manifest)
        with manifest_path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2)

    @staticmethod
    def _rewrite_cases(corpus_dir, mutate):
        cases_path = corpus_dir / "cases.json"
        with cases_path.open("r", encoding="utf-8") as fh:
            cases = json.load(fh)
        mutate(cases)
        with cases_path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(cases, fh, indent=2)

    def test_loader_returns_typed_records(self):
        self.assertIsInstance(self.corpus, RetrievalGoldCorpus)
        first = self.corpus.documents[0]
        self.assertTrue(first.path.endswith((".md", ".txt")))
        self.assertTrue(first.sha256)
        case = self.corpus.case("rg-001")
        self.assertTrue(case.query)
        self.assertTrue(case.expected_span.text)

    def test_loader_lookups_and_filters(self):
        self.assertEqual(
            len(self.corpus.documents_by_role("controlling_spec")), 1
        )
        self.assertTrue(self.corpus.cases_by_kind("revision_marker"))
        with self.assertRaises(RetrievalGoldCorpusError):
            self.corpus.document("no_such_document.md")
        with self.assertRaises(RetrievalGoldCorpusError):
            self.corpus.case("no-such-case")

    def test_loader_is_deterministic(self):
        second = load_corpus(FIXTURES_DIR)
        self.assertEqual(self.corpus, second)

    def test_tamper_altered_fixture_byte_fails_load(self):
        corpus_dir = self._scratch_copy()
        target = corpus_dir / "aeris_m_safety_datasheet.md"
        raw = target.read_bytes()
        target.write_bytes(raw.replace(b"DSV-44", b"DSV-45"))
        with self.assertRaises(RetrievalGoldCorpusError):
            load_corpus(corpus_dir)

    def test_tamper_manifest_hash_fails_load(self):
        corpus_dir = self._scratch_copy()
        self._rewrite_manifest(
            corpus_dir, lambda m: m["documents"][0].update(sha256="0" * 64)
        )
        with self.assertRaises(RetrievalGoldCorpusError):
            load_corpus(corpus_dir)

    def test_tamper_span_text_not_verbatim_fails_load(self):
        corpus_dir = self._scratch_copy()

        def mutate(cases):
            for case in cases["cases"]:
                if case["id"] == "rg-002":
                    case["expected_span"]["text"] = (
                        "Calibration interval: 91 days under normal service."
                    )

        self._rewrite_cases(corpus_dir, mutate)
        with self.assertRaises(RetrievalGoldCorpusError):
            load_corpus(corpus_dir)

    def test_tamper_span_out_of_bounds_fails_load(self):
        corpus_dir = self._scratch_copy()
        document_length = len(self.corpus.text("aeris_m_spec_rev2.md"))

        def mutate(cases):
            for case in cases["cases"]:
                if case["id"] == "rg-002":
                    case["expected_span"]["end"] = document_length + 500

        self._rewrite_cases(corpus_dir, mutate)
        with self.assertRaises(RetrievalGoldCorpusError):
            load_corpus(corpus_dir)

    def test_tamper_unknown_role_fails_load(self):
        corpus_dir = self._scratch_copy()
        self._rewrite_manifest(
            corpus_dir, lambda m: m["documents"][0].update(role="not_a_role")
        )
        with self.assertRaises(RetrievalGoldCorpusError):
            load_corpus(corpus_dir)


class RetrievalGoldPurityTests(unittest.TestCase):
    """Test-support purity: the loader never imports production code, and the
    RETRIEVAL_GOLD_ROOT seam redirects fixture resolution."""

    def test_no_production_code_references_the_gold_corpus_loader(self):
        import ast

        source_path = BACKEND_ROOT / "tests" / "retrieval_gold" / "gold_corpus.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.split(".")[0] == "app",
                        f"loader imports production code: {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertFalse(
                    module.split(".")[0] == "app",
                    f"loader imports production code: {module}",
                )
        app_tree = BACKEND_ROOT / "app"
        for py_file in app_tree.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8", errors="ignore") as fh:
                self.assertNotIn("retrieval_gold", fh.read(), py_file)

    def test_retrieval_gold_root_seam_redirects_the_default_fixtures_dir(self):
        from tests.retrieval_gold import gold_corpus

        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        shutil.copytree(FIXTURES_DIR, scratch / "corpus")
        with patch.dict(os.environ, {"RETRIEVAL_GOLD_ROOT": str(scratch / "corpus")}):
            root = Path(os.environ["RETRIEVAL_GOLD_ROOT"])
            corpus = gold_corpus.load_corpus(root)
            self.assertEqual(len(corpus.documents), 10)



class RetrievalDiscriminationTests(unittest.IsolatedAsyncioTestCase):
    """Run the repo's real retrieval path over the corpus.

    Real machinery: LanceDB ``VectorStore`` (dense ANN + tantivy BM25 FTS,
    hybrid RRF fusion), ``DocumentRetrievalService.filter_relevant`` cutoff
    and group-aware dedup, and the ``RAGEngine.retrieve_eval_results``
    retrieval-only seam. Substituted/pinned (disclosed in the eval docs):
    the deterministic token-hash embedding at the ``embedding_service``
    seam, the harness paragraph chunker at both scales, reranking off, and
    query transformation off via ``patch.object``.
    """

    def _base_dir(self):
        base = Path(tempfile.mkdtemp(prefix="retrieval-gold-"))
        self.addCleanup(shutil.rmtree, base, True)
        return base

    async def _build_store(self, corpus, embedder):
        from app.services.vector_store import VectorStore

        base = self._base_dir()
        store = VectorStore(db_path=base / "lancedb")
        self.addCleanup(store.close)
        await store.init_table(EMBED_DIM)
        records = []
        for document in corpus.documents:
            for scale, limit in SCALE_LIMITS.items():
                for index, unit in enumerate(chunk_text(document.text, limit)):
                    records.append(
                        {
                            "id": f"{document.path}|{scale}|{index}",
                            "text": unit,
                            "file_id": document.path,
                            "vault_id": "1",
                            "chunk_index": index,
                            "chunk_scale": scale,
                            "metadata": "{}",
                            "embedding": await embedder.embed_single(unit),
                        }
                    )
        await store.add_chunks(records)
        return store

    def _make_engine(self, corpus, store, embedder):
        from app.config import settings
        from app.services.document_retrieval import DocumentRetrievalService
        from app.services.rag_engine import RAGEngine

        engine = RAGEngine.__new__(RAGEngine)
        engine.embedding_service = embedder
        engine.vector_store = store
        engine.llm_client = None
        engine._thinking_client_override = None
        engine._instant_client_override = None
        engine.reranking_enabled = False
        engine.reranking_service = None
        engine.reranker_top_n = ENGINE_TOP_K
        engine.retrieval_top_k = ENGINE_TOP_K
        engine.initial_retrieval_top_k = ENGINE_TOP_K
        engine.hybrid_search_enabled = True
        engine.hybrid_alpha = HYBRID_ALPHA
        engine.max_distance_threshold = 0.75
        engine.relevance_threshold = settings.rag_relevance_threshold
        engine.retrieval_window = 0
        engine.vector_metric = "cosine"
        engine.maintenance_mode = False
        engine.document_retrieval = DocumentRetrievalService(
            vector_store=store,
            max_distance_threshold=0.75,
            retrieval_top_k=ENGINE_TOP_K,
            retrieval_window=0,
        )
        engine._get_indexed_file_ids = lambda vault_id: {
            document.path for document in corpus.documents
        }
        return engine

    @staticmethod
    def _span_unit_ids(corpus, document_path, span_text):
        unit_ids = set()
        for scale, limit in SCALE_LIMITS.items():
            for index, unit in enumerate(chunk_text(corpus.text(document_path), limit)):
                if span_text in unit:
                    unit_ids.add(f"{document_path}|{scale}|{index}")
        return unit_ids

    @staticmethod
    def _unit_text(corpus, unit_id):
        document_path, scale, index = unit_id.split("|")
        units = chunk_text(corpus.text(document_path), SCALE_LIMITS[scale])
        return units[int(index)]

    async def test_discrimination_over_all_cases(self):
        from app.config import settings

        corpus = load_corpus(FIXTURES_DIR)
        embedder = _DeterministicEmbeddingService()
        store = await self._build_store(corpus, embedder)
        engine = self._make_engine(corpus, store, embedder)

        saw_nonempty = False
        with patch.object(settings, "query_transformation_enabled", False), patch.object(
            settings, "context_max_tokens", 0
        ):
            for case in corpus.cases:
                with self.subTest(case=case.id):
                    calls_before = len(embedder.calls)
                    outcome = await engine.retrieve_eval_results(
                        case.query, vault_id=None, top_k=ENGINE_TOP_K
                    )
                    self.assertEqual(
                        len(embedder.calls) - calls_before,
                        1,
                        f"{case.id}: exactly the original query variant must be embedded",
                    )
                    self.assertEqual(outcome.status, "ok", f"{case.id}: retrieval unavailable")
                    ranked = list(outcome.retrieved_ids[:ENGINE_TOP_K])
                    if case.kind == "not_in_corpus":
                        self.assertEqual(
                            ranked,
                            [],
                            f"{case.id}: out-of-domain query must return no confident hit",
                        )
                        continue
                    self.assertIn(
                        case.expected_doc,
                        ranked,
                        f"{case.id}: expected document not in top-{ENGINE_TOP_K}",
                    )
                    saw_nonempty = True
                    for extra in case.also_expected:
                        self.assertIn(extra, ranked, f"{case.id}: also-expected document missing")

                    direct = await store.search(
                        _embed_text(case.query),
                        limit=DIRECT_TOP_K,
                        query_text=case.query,
                        hybrid=True,
                        hybrid_alpha=HYBRID_ALPHA,
                    )
                    span_units = self._span_unit_ids(
                        corpus, case.expected_doc, case.expected_span.text
                    )
                    positions = [
                        index
                        for index, record in enumerate(direct)
                        if record["id"] in span_units
                    ]
                    self.assertTrue(
                        positions,
                        f"{case.id}: no span-bearing chunk of {case.expected_doc} in the "
                        f"direct top-{DIRECT_TOP_K}",
                    )
                    baseline = min(positions)
                    for trap in case.must_not_match:
                        # The trap comparison is span-vs-span: the near-trap
                        # PASSAGE must never outrank the expected passage
                        # (metadata chunks of either document are noise, not
                        # traps). For tier-1 traps the trap's span-bearing
                        # chunks carry the shared expected-span text.
                        trap_span_text = (
                            case.trap_span.text
                            if case.trap_span is not None
                            else case.expected_span.text
                        )
                        trap_units = self._span_unit_ids(corpus, trap, trap_span_text)
                        trap_positions = [
                            index
                            for index, record in enumerate(direct)
                            if record["id"] in trap_units
                        ]
                        if not trap_positions:
                            continue
                        self.assertGreater(
                            min(trap_positions),
                            baseline,
                            f"{case.id}: trap document {trap} ranks its near-trap passage "
                            "above the expected document's span-bearing chunk",
                        )

        self.assertTrue(
            saw_nonempty, "control failed: no case retrieved anything (store empty?)"
        )
        self.assertEqual(
            store.get_fts_exceptions(),
            0,
            "the BM25 FTS leg silently degraded to dense-only during the run",
        )
        probe_query = corpus.case("rg-001").query
        probe_direct = await store.search(
            _embed_text(probe_query),
            limit=DIRECT_TOP_K,
            query_text=probe_query,
            hybrid=True,
            hybrid_alpha=HYBRID_ALPHA,
        )
        self.assertTrue(probe_direct)
        self.assertEqual(
            {record.get("_fts_status") for record in probe_direct},
            {"ok"},
            "hybrid search records must carry _fts_status 'ok' (lexical leg ran)",
        )

    async def test_right_reason_probe_trap_chunk_vector_inverts_ranking(self):
        """In-suite right-reason probe: substituting the TRAP chunk's
        embedding for one case's query must invert the ranking so that the
        trap document's near-trap passage outranks the expected span-bearing
        chunk -- i.e. the trap assertion above would fail. The probe case
        (rg-013, measurement depth) was selected empirically: its trap is
        absent from the direct top-10 in the normal orientation and the
        substitution flips the ordering decisively."""
        corpus = load_corpus(FIXTURES_DIR)
        probe_case = corpus.case("rg-013")
        trap = probe_case.must_not_match[0]
        trap_units = self._span_unit_ids(corpus, trap, probe_case.trap_span.text)
        self.assertTrue(trap_units)
        trap_vector = _embed_text(self._unit_text(corpus, sorted(trap_units)[0]))
        embedder = _DeterministicEmbeddingService(overrides={probe_case.query: trap_vector})
        store = await self._build_store(corpus, embedder)

        direct = await store.search(
            trap_vector,
            limit=DIRECT_TOP_K,
            query_text=probe_case.query,
            hybrid=True,
            hybrid_alpha=HYBRID_ALPHA,
        )
        expected_units = self._span_unit_ids(
            corpus, probe_case.expected_doc, probe_case.expected_span.text
        )
        expected_positions = [
            index for index, record in enumerate(direct) if record["id"] in expected_units
        ]
        self.assertTrue(
            expected_positions,
            "probe setup: expected span-bearing chunk missing from direct results",
        )
        baseline = min(expected_positions)
        trap_positions = [
            index for index, record in enumerate(direct) if record["id"] in trap_units
        ]
        self.assertTrue(trap_positions, "probe setup: trap chunk missing from results")
        self.assertLess(
            min(trap_positions),
            baseline,
            "right-reason probe: trap-chunk-vector substitution must place the trap "
            "document above the expected span-bearing chunk (the trap assertion would "
            "fail), proving the discrimination assertions bite",
        )


if __name__ == "__main__":
    unittest.main()
