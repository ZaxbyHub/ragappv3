"""Contract tests for the Draft Room gold evaluation corpus.

This corpus is a release contract: later Draft Room issues gate on the
denominators, span offsets and hashes recorded in
``backend/tests/fixtures/draft_room/manifest.json``. These tests are the gate.
They read the manifest and the fixture bytes directly (not only through the
loader) so that a bug in the loader cannot mask a broken corpus.

No app module is imported. Nothing is written into the repository tree.
"""

import hashlib
import json
import math
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.draft_room.gold_corpus import (  # noqa: E402
    AUTHORITIES,
    CATEGORIES,
    REQUIRED_HIGH_STAKES_CATEGORIES,
    ROLES,
    STATUSES,
    ExactQuote,
    GoldCorpusError,
    NearQuoteTrap,
    Proposition,
    load_corpus,
    normalize_document_text,
    normalize_for_match,
    normalize_quotes,
    score_citation_accuracy,
    score_major_edit_outcome,
    score_proposition_preservation,
    score_quote_fidelity,
    score_unsupported_claim_rate,
    split_sentences,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
FIXTURES_DIR = BACKEND_ROOT / "tests" / "fixtures" / "draft_room"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"

MIN_PROPOSITIONS = 50
MIN_HIGH_STAKES = 20
EXPECTED_SCENARIO_IDS = tuple(f"S{index:02d}" for index in range(1, 11))
ALLOWED_EXTENSIONS = {".md", ".txt", ".json"}
ALLOWED_URL_HOSTS = ("example.com", "example.org", "example.net")
ALLOWED_EMAIL_DOMAIN = "example.com"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)\]\"'<>]+")
DOTTED_QUAD_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
INTERNAL_HOST_RE = re.compile(
    r"\b[A-Za-z0-9\-]+\.(?:local|internal|intranet|corp|lan|home|localdomain)\b",
    re.IGNORECASE,
)
POSIX_PATH_RE = re.compile(
    r"(?<![\w.\-])/(?:etc|usr|var|home|opt|root|srv|tmp|mnt|media|proc|sys|Users)(?:/|\b)"
)
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\\\?[A-Za-z0-9_\\]")
UNC_PATH_RE = re.compile(r"\\\\[A-Za-z0-9_\-]+\\")
TOKEN_RE = re.compile(r"[A-Za-z0-9+/=]{20,}")


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _load_manifest_json() -> dict:
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class GoldCorpusManifestTests(unittest.TestCase):
    """Structural contract over the manifest and the fixture bytes."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest_json()
        cls.corpus = load_corpus(FIXTURES_DIR)
        cls.texts = {}
        for entry in cls.manifest["documents"]:
            raw = (FIXTURES_DIR / entry["path"]).read_bytes()
            cls.texts[entry["id"]] = normalize_document_text(raw)

    # -- 1. manifest / file parity ---------------------------------------
    def test_manifest_and_fixture_files_are_in_exact_parity(self):
        declared = {entry["path"] for entry in self.manifest["documents"]}
        self.assertTrue(declared, "manifest declares no documents")

        on_disk = set()
        for path in sorted(FIXTURES_DIR.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "manifest.json" and path.parent == FIXTURES_DIR:
                continue
            rel = path.relative_to(FIXTURES_DIR).as_posix()
            on_disk.add(rel)

        self.assertEqual(
            declared,
            on_disk,
            f"manifest/file drift: only in manifest={sorted(declared - on_disk)}, "
            f"only on disk={sorted(on_disk - declared)}",
        )

    def test_every_document_sha256_matches_the_real_bytes(self):
        for entry in self.manifest["documents"]:
            with self.subTest(document=entry["id"]):
                raw = (FIXTURES_DIR / entry["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                self.assertEqual(len(raw), entry["byte_length"])
                self.assertEqual(len(normalize_document_text(raw)), entry["char_length"])

    def test_corpus_is_plain_text_only(self):
        for path in sorted(FIXTURES_DIR.rglob("*")):
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                self.assertIn(path.suffix, ALLOWED_EXTENSIONS)
                # A binary would raise here.
                path.read_bytes().decode("utf-8")

    # -- 2. id uniqueness -------------------------------------------------
    def test_all_ids_are_unique_within_and_across_families(self):
        families = {
            "documents": [d["id"] for d in self.manifest["documents"]],
            "expected_propositions": [p["id"] for p in self.manifest["expected_propositions"]],
            "exact_quotes": [q["id"] for q in self.manifest["exact_quotes"]],
            "near_quote_traps": [t["id"] for t in self.manifest["near_quote_traps"]],
            "scenarios": [s["id"] for s in self.manifest["scenarios"]],
            "injection_strings": [i["id"] for i in self.manifest["injection_strings"]],
            "locked_spans": [locked["id"] for locked in self.manifest["locked_spans"]],
            "expected_contradictions": [
                c["id"] for c in self.manifest["expected_contradictions"]
            ],
        }
        everything = []
        for name, ids in families.items():
            with self.subTest(family=name):
                self.assertEqual(len(ids), len(set(ids)), f"duplicate id in {name}")
                self.assertTrue(all(ids), f"empty id in {name}")
            everything.extend(ids)
        self.assertEqual(
            len(everything),
            len(set(everything)),
            "an id is reused across two different families",
        )

    def test_document_paths_are_unique_and_relative(self):
        paths = [d["path"] for d in self.manifest["documents"]]
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(Path(path).is_absolute())
                self.assertNotIn("..", Path(path).parts)

    # -- 3. span integrity ------------------------------------------------
    def _assert_span(self, entry, family):
        text = self.texts[entry["document_id"]]
        start, end = entry["start"], entry["end"]
        self.assertIsInstance(start, int)
        self.assertIsInstance(end, int)
        self.assertGreaterEqual(start, 0, f"{family} {entry['id']}: negative start")
        self.assertLess(start, end, f"{family} {entry['id']}: start >= end")
        self.assertLessEqual(end, len(text), f"{family} {entry['id']}: end past EOF")
        return text[start:end]

    def test_locked_spans_hash_the_real_substring(self):
        self.assertGreaterEqual(len(self.manifest["locked_spans"]), 3)
        for entry in self.manifest["locked_spans"]:
            with self.subTest(locked_span=entry["id"]):
                spanned = self._assert_span(entry, "locked span")
                self.assertEqual(
                    hashlib.sha256(spanned.encode("utf-8")).hexdigest(), entry["sha256"]
                )
                self.assertTrue(entry["reason"].strip())

    def test_exact_quotes_match_their_spans_and_hashes(self):
        self.assertTrue(self.manifest["exact_quotes"])
        for entry in self.manifest["exact_quotes"]:
            with self.subTest(quote=entry["id"]):
                spanned = self._assert_span(entry, "exact quote")
                self.assertEqual(spanned, entry["text"])
                self.assertEqual(
                    hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(), entry["sha256"]
                )

    def test_near_quote_traps_are_close_to_but_not_equal_to_their_quotes(self):
        quotes = {q["id"]: q["text"] for q in self.manifest["exact_quotes"]}
        self.assertTrue(self.manifest["near_quote_traps"])
        for entry in self.manifest["near_quote_traps"]:
            with self.subTest(trap=entry["id"]):
                spanned = self._assert_span(entry, "near-quote trap")
                self.assertEqual(spanned, entry["text"])
                self.assertIn(entry["trap_for_quote_id"], quotes)
                original = quotes[entry["trap_for_quote_id"]]
                # A trap must differ from its quote even after folding quote
                # glyphs — otherwise it is not a trap.
                self.assertNotEqual(normalize_quotes(entry["text"]), normalize_quotes(original))

    # -- 4. proposition denominators --------------------------------------
    def test_proposition_denominators_meet_the_release_contract(self):
        props = self.manifest["expected_propositions"]
        self.assertGreaterEqual(
            len(props), MIN_PROPOSITIONS, "corpus must carry at least 50 expected propositions"
        )
        high_stakes = [p for p in props if p["high_stakes"] is True]
        self.assertGreaterEqual(
            len(high_stakes), MIN_HIGH_STAKES, "corpus must carry at least 20 high-stakes propositions"
        )
        covered = {p["category"] for p in high_stakes}
        for category in REQUIRED_HIGH_STAKES_CATEGORIES:
            with self.subTest(category=category):
                self.assertIn(
                    category, covered, f"no high-stakes proposition covers {category!r}"
                )

    def test_every_proposition_is_well_formed(self):
        known_docs = {d["id"] for d in self.manifest["documents"]}
        for prop in self.manifest["expected_propositions"]:
            with self.subTest(proposition=prop["id"]):
                self.assertIn(prop["expected_status"], STATUSES)
                self.assertIn(prop["category"], CATEGORIES)
                self.assertIsInstance(prop["high_stakes"], bool)
                self.assertIsInstance(prop["required"], bool)
                self.assertTrue(prop["text"].strip())
                self.assertTrue(prop["document_ids"])
                self.assertTrue(prop["anchors"])
                for doc_id in prop["document_ids"]:
                    self.assertIn(doc_id, known_docs)

    def test_every_proposition_anchor_occurs_in_one_of_its_documents(self):
        """Anchors are the oracle. An anchor absent from its source is a broken oracle."""
        for prop in self.manifest["expected_propositions"]:
            haystacks = [normalize_for_match(self.texts[d]) for d in prop["document_ids"]]
            for anchor in prop["anchors"]:
                with self.subTest(proposition=prop["id"], anchor=anchor):
                    needle = normalize_for_match(anchor)
                    self.assertTrue(needle, "anchor normalises to the empty string")
                    self.assertTrue(
                        any(needle in haystack for haystack in haystacks),
                        f"anchor {anchor!r} not present in {prop['document_ids']}",
                    )

    def test_every_expected_status_is_exercised(self):
        seen = {p["expected_status"] for p in self.manifest["expected_propositions"]}
        self.assertEqual(seen, set(STATUSES))

    # -- 5. scenarios ------------------------------------------------------
    def test_all_ten_scenarios_are_present_and_wired(self):
        scenarios = {s["id"]: s for s in self.manifest["scenarios"]}
        self.assertEqual(tuple(sorted(scenarios)), EXPECTED_SCENARIO_IDS)
        known_docs = {d["id"] for d in self.manifest["documents"]}
        for scenario_id, scenario in scenarios.items():
            with self.subTest(scenario=scenario_id):
                self.assertTrue(scenario["description"].strip())
                self.assertTrue(scenario["document_ids"])
                for doc_id in scenario["document_ids"]:
                    self.assertIn(doc_id, known_docs)

    def test_scenario_membership_is_bidirectional(self):
        scenarios = {s["id"]: set(s["document_ids"]) for s in self.manifest["scenarios"]}
        for doc in self.manifest["documents"]:
            self.assertTrue(doc["scenario_ids"], f"{doc['id']} joins no scenario")
            for scenario_id in doc["scenario_ids"]:
                with self.subTest(document=doc["id"], scenario=scenario_id):
                    self.assertIn(scenario_id, scenarios)
                    self.assertIn(doc["id"], scenarios[scenario_id])

    def test_scenario_eight_uses_one_filename_in_two_vaults(self):
        scenario = next(s for s in self.manifest["scenarios"] if s["id"] == "S08")
        docs = [d for d in self.manifest["documents"] if d["id"] in scenario["document_ids"]]
        self.assertEqual(len(docs), 2)
        names = {Path(d["path"]).name for d in docs}
        self.assertEqual(len(names), 1, "cross-tenant fixtures must share a filename")
        self.assertEqual(len({d["vault"] for d in docs}), 2, "must belong to two vaults")
        self.assertNotEqual(
            self.texts[docs[0]["id"]],
            self.texts[docs[1]["id"]],
            "same-named cross-vault fixtures must differ in content",
        )

    # -- 6. enums ----------------------------------------------------------
    def test_roles_and_authorities_use_the_exact_enum_literals(self):
        for doc in self.manifest["documents"]:
            with self.subTest(document=doc["id"]):
                self.assertIn(doc["role"], ROLES)
                self.assertIn(doc["authority"], AUTHORITIES)
        self.assertEqual(tuple(self.manifest["enums"]["roles"]), ROLES)
        self.assertEqual(tuple(self.manifest["enums"]["authorities"]), AUTHORITIES)
        self.assertEqual(
            set(self.manifest["enums"]["proposition_statuses"]), set(STATUSES)
        )
        self.assertEqual(
            set(self.manifest["enums"]["proposition_categories"]), set(CATEGORIES)
        )

    def test_every_role_is_represented_by_at_least_one_document(self):
        present = {doc["role"] for doc in self.manifest["documents"]}
        self.assertEqual(present, set(ROLES))

    # -- 7. injections -----------------------------------------------------
    def test_every_injection_string_is_literally_present_in_its_document(self):
        for entry in self.manifest["injection_strings"]:
            with self.subTest(injection=entry["id"]):
                text = self.texts[entry["document_id"]]
                self.assertIn(entry["payload"], text)
                self.assertEqual(text[entry["start"]:entry["end"]], entry["payload"])
                doc = next(
                    d for d in self.manifest["documents"] if d["id"] == entry["document_id"]
                )
                self.assertEqual(entry["role"], doc["role"])

    def test_injections_exist_in_all_five_source_roles(self):
        roles = {entry["role"] for entry in self.manifest["injection_strings"]}
        self.assertEqual(roles, set(ROLES))

    def test_injection_payloads_are_distinct(self):
        payloads = [entry["payload"] for entry in self.manifest["injection_strings"]]
        self.assertEqual(len(payloads), len(set(payloads)), "injection payloads must be varied")

    # -- 8. absence of external / private data ------------------------------
    def test_no_fixture_contains_external_or_private_data(self):
        allowed_hashes = set()
        for family in ("documents", "locked_spans", "exact_quotes", "near_quote_traps"):
            for entry in self.manifest[family]:
                allowed_hashes.add(entry["sha256"])

        for path in sorted(FIXTURES_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(FIXTURES_DIR).as_posix()
            text = path.read_text(encoding="utf-8")
            with self.subTest(fixture=rel):
                for match in EMAIL_RE.findall(text):
                    domain = match.rsplit("@", 1)[1].lower()
                    self.assertEqual(
                        domain,
                        ALLOWED_EMAIL_DOMAIN,
                        f"{rel}: email address on a non-example domain: {match}",
                    )
                for url in URL_RE.findall(text):
                    host = url.split("://", 1)[1].split("/", 1)[0].split("@")[-1]
                    host = host.split(":", 1)[0].lower()
                    self.assertTrue(
                        host in ALLOWED_URL_HOSTS
                        or any(host.endswith("." + h) for h in ALLOWED_URL_HOSTS),
                        f"{rel}: URL to a non-example host: {url}",
                    )
                self.assertEqual(
                    DOTTED_QUAD_RE.findall(text), [], f"{rel}: contains an IP-like literal"
                )
                self.assertEqual(
                    INTERNAL_HOST_RE.findall(text),
                    [],
                    f"{rel}: contains an internal-looking hostname",
                )
                self.assertIsNone(
                    POSIX_PATH_RE.search(text), f"{rel}: contains an absolute POSIX path"
                )
                self.assertIsNone(
                    WINDOWS_PATH_RE.search(text), f"{rel}: contains an absolute Windows path"
                )
                self.assertIsNone(UNC_PATH_RE.search(text), f"{rel}: contains a UNC path")
                for token in TOKEN_RE.findall(text):
                    if token in allowed_hashes:
                        continue
                    has_digit = any(char.isdigit() for char in token)
                    has_alpha = any(char.isalpha() for char in token)
                    if not (has_digit and has_alpha):
                        continue
                    self.assertLess(
                        _shannon_entropy(token),
                        3.5,
                        f"{rel}: credential-like high-entropy token: {token[:12]}...",
                    )

    # -- 9. contradictions --------------------------------------------------
    def test_expected_contradictions_reference_real_ids_and_really_disagree(self):
        props = {p["id"]: p for p in self.manifest["expected_propositions"]}
        known_docs = {d["id"] for d in self.manifest["documents"]}
        self.assertTrue(self.manifest["expected_contradictions"])

        for entry in self.manifest["expected_contradictions"]:
            with self.subTest(contradiction=entry["id"]):
                self.assertGreaterEqual(len(entry["proposition_ids"]), 2)
                for prop_id in entry["proposition_ids"]:
                    self.assertIn(prop_id, props)
                for doc_id in entry["document_ids"]:
                    self.assertIn(doc_id, known_docs)
                self.assertTrue(entry["reason"].strip())

                statuses = {props[p]["expected_status"] for p in entry["proposition_ids"]}
                self.assertIn(
                    "supported",
                    statuses,
                    "a contradiction must name the claim the corpus actually supports",
                )
                self.assertTrue(
                    statuses & {"contradicted", "unsupported", "stale"},
                    "a contradiction must name the claim the corpus rejects",
                )

                # Positive proof that the two sources genuinely disagree: each
                # side quotes a real, distinct passage from a distinct document.
                evidence = entry["evidence"]
                self.assertGreaterEqual(len(evidence), 2)
                seen_docs = set()
                seen_quotes = set()
                for item in evidence:
                    self.assertIn(item["document_id"], known_docs)
                    text = self.texts[item["document_id"]]
                    self.assertIn(item["quote"], text)
                    self.assertEqual(text[item["start"]:item["end"]], item["quote"])
                    seen_docs.add(item["document_id"])
                    seen_quotes.add(normalize_for_match(item["quote"]))
                self.assertGreaterEqual(len(seen_docs), 2, "both sides came from one document")
                self.assertGreaterEqual(len(seen_quotes), 2, "both sides quoted the same text")

    def test_stale_and_fresh_sources_are_distinguished_by_as_of_date(self):
        by_id = {d["id"]: d for d in self.manifest["documents"]}
        stale = by_id["doc_reference_sensor_spec_v1"]
        fresh = by_id["doc_reference_sensor_spec_v2"]
        self.assertLess(stale["as_of_date"], fresh["as_of_date"])
        self.assertIn("180 days", self.texts[stale["id"]])
        self.assertIn("90 days", self.texts[fresh["id"]])

    # -- 10. no production dependency ---------------------------------------
    def test_no_production_code_references_the_gold_corpus(self):
        app_dir = BACKEND_ROOT / "app"
        self.assertTrue(app_dir.is_dir(), "backend/app not found")
        forbidden = ("gold_corpus", "manifest.json", "fixtures/draft_room")
        offenders = []
        for path in sorted(app_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} references {needle!r}")
        self.assertEqual(offenders, [], "production code must not reach the gold answers")

    # -- curation record -----------------------------------------------------
    def test_curation_record_names_two_reviewers_and_an_adjudicator(self):
        reviewers = self.manifest["reviewers"]
        self.assertEqual(reviewers["reviewer_a"]["id"], "reviewer_a")
        self.assertEqual(reviewers["reviewer_b"]["id"], "reviewer_b")
        self.assertEqual(reviewers["adjudicator"]["id"], "product_owner")
        self.assertEqual(
            sorted(reviewers["sign_off"]["curated_by"]), ["reviewer_a", "reviewer_b"]
        )
        self.assertEqual(reviewers["sign_off"]["adjudicated_by"], "product_owner")
        self.assertEqual(
            reviewers["sign_off"]["rubric_version"], self.manifest["rubric_version"]
        )
        prop_ids = {p["id"] for p in self.manifest["expected_propositions"]}
        self.assertTrue(reviewers["adjudicated_items"])
        for item in reviewers["adjudicated_items"]:
            with self.subTest(item=item["item_id"]):
                self.assertIn(item["item_id"], prop_ids)
                self.assertTrue(item["disagreement"].strip())
                self.assertTrue(item["adjudication"].strip())

    def test_manifest_declares_versions(self):
        self.assertTrue(self.manifest["schema_version"])
        self.assertTrue(self.manifest["rubric_version"])
        self.assertEqual(self.corpus.schema_version, self.manifest["schema_version"])
        self.assertEqual(self.corpus.rubric_version, self.manifest["rubric_version"])


class GoldCorpusLoaderTests(unittest.TestCase):
    """The loader must return typed records and reject a corrupted manifest."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(FIXTURES_DIR)

    def test_loader_returns_typed_records(self):
        self.assertTrue(all(isinstance(p, Proposition) for p in self.corpus.propositions))
        self.assertTrue(all(isinstance(q, ExactQuote) for q in self.corpus.exact_quotes))
        self.assertTrue(all(isinstance(t, NearQuoteTrap) for t in self.corpus.near_quote_traps))
        doc = self.corpus.document("doc_manuscript_field_report")
        self.assertEqual(doc.role, "manuscript")
        self.assertEqual(doc.authority, "primary")
        self.assertEqual(len(self.corpus.text(doc.id)), doc.char_length)

    def test_loader_lookups_and_filters(self):
        self.assertEqual(
            {d.id for d in self.corpus.documents_by_role("manuscript")},
            {"doc_manuscript_field_report", "doc_manuscript_regulatory_addendum"},
        )
        self.assertTrue(self.corpus.propositions_by_status("opinion"))
        self.assertGreaterEqual(len(self.corpus.high_stakes_propositions), MIN_HIGH_STAKES)
        self.assertTrue(self.corpus.required_propositions)
        with self.assertRaises(GoldCorpusError):
            self.corpus.document("doc_does_not_exist")
        with self.assertRaises(GoldCorpusError):
            self.corpus.text("doc_does_not_exist")
        with self.assertRaises(GoldCorpusError):
            self.corpus.proposition("p_does_not_exist")

    def test_loader_rejects_a_tampered_manifest(self):
        import shutil
        import tempfile

        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        shutil.copytree(FIXTURES_DIR, scratch / "corpus")
        target = scratch / "corpus" / "manifest.json"

        data = json.loads(target.read_text(encoding="utf-8"))
        data["documents"][0]["sha256"] = "0" * 64
        target.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(GoldCorpusError):
            load_corpus(scratch / "corpus")

        data = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
        data["exact_quotes"][0]["end"] = data["exact_quotes"][0]["end"] + 3
        target.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(GoldCorpusError):
            load_corpus(scratch / "corpus")

        data = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
        data["documents"][0]["role"] = "not_a_role"
        target.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(GoldCorpusError):
            load_corpus(scratch / "corpus")

        data = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
        data["expected_propositions"][1]["id"] = data["expected_propositions"][0]["id"]
        target.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(GoldCorpusError):
            load_corpus(scratch / "corpus")

    def test_loader_is_deterministic(self):
        again = load_corpus(FIXTURES_DIR)
        self.assertEqual(
            [p.id for p in again.propositions], [p.id for p in self.corpus.propositions]
        )
        self.assertEqual([q.sha256 for q in again.exact_quotes],
                         [q.sha256 for q in self.corpus.exact_quotes])


class NormalizationHelperTests(unittest.TestCase):
    def test_normalize_quotes_folds_typographic_glyphs_only(self):
        self.assertEqual(normalize_quotes("“hi”"), '"hi"')
        self.assertEqual(normalize_quotes("it’s"), "it's")
        self.assertEqual(normalize_quotes("a b"), "a b")
        # Dashes and case survive untouched.
        self.assertEqual(normalize_quotes("A — B"), "A — B")

    def test_normalize_for_match_folds_case_dashes_and_whitespace(self):
        self.assertEqual(normalize_for_match("  A —\nB  "), "a - b")
        self.assertEqual(normalize_for_match("“X”"), '"x"')

    def test_split_sentences(self):
        self.assertEqual(
            split_sentences("One. Two! Three?\n\nFour; five."),
            ("One.", "Two!", "Three?", "Four;", "five."),
        )
        self.assertEqual(split_sentences("   \n  "), ())


class PropositionPreservationTests(unittest.TestCase):
    def _prop(self, pid, anchors, status="supported", required=True):
        return Proposition(
            id=pid,
            text=pid,
            document_ids=("doc",),
            expected_status=status,
            high_stakes=True,
            required=required,
            category="other",
            anchors=tuple(anchors),
        )

    def test_all_anchors_present_scores_one(self):
        props = [self._prop("a", ["42 probes"]), self._prop("b", ["90 days"])]
        result = score_proposition_preservation("We ran 42 probes for 90 days.", props)
        self.assertEqual(result.total, 2)
        self.assertEqual(result.preserved_ids, ("a", "b"))
        self.assertEqual(result.missing_ids, ())
        self.assertEqual(result.preservation_rate, 1.0)
        self.assertEqual(result.required_preservation_rate, 1.0)

    def test_missing_anchor_is_reported(self):
        props = [self._prop("a", ["42", "probes"]), self._prop("b", ["90 days"])]
        result = score_proposition_preservation("We ran 42 probes.", props)
        self.assertEqual(result.preserved_ids, ("a",))
        self.assertEqual(result.missing_ids, ("b",))
        self.assertEqual(result.preservation_rate, 0.5)
        self.assertEqual(result.required_missing_ids, ("b",))

    def test_partial_anchor_match_is_not_preservation(self):
        props = [self._prop("a", ["42", "probes"])]
        result = score_proposition_preservation("We ran 42 sensors.", props)
        self.assertEqual(result.missing_ids, ("a",))
        self.assertEqual(result.preservation_rate, 0.0)

    def test_case_and_line_wrapping_do_not_count_as_loss(self):
        props = [self._prop("a", ["ninety days ago"])]
        result = score_proposition_preservation("It was NINETY\n  DAYS ago.", props)
        self.assertEqual(result.preserved_ids, ("a",))

    def test_empty_input_scores_one(self):
        result = score_proposition_preservation("anything", [])
        self.assertEqual(result.preservation_rate, 1.0)
        self.assertEqual(result.required_preservation_rate, 1.0)
        self.assertEqual(result.required_total, 0)

    def test_required_subset_is_tracked_separately(self):
        props = [
            self._prop("a", ["alpha"], required=True),
            self._prop("b", ["beta"], required=False),
        ]
        result = score_proposition_preservation("only beta here", props)
        self.assertEqual(result.preserved_ids, ("b",))
        self.assertEqual(result.required_total, 1)
        self.assertEqual(result.required_missing_ids, ("a",))
        self.assertEqual(result.required_preservation_rate, 0.0)

    def test_against_the_real_corpus(self):
        corpus = load_corpus(FIXTURES_DIR)
        manuscript = corpus.text("doc_manuscript_field_report")
        props = [
            p
            for p in corpus.propositions_by_status("supported")
            if "doc_manuscript_field_report" in p.document_ids
        ]
        self.assertTrue(props)
        result = score_proposition_preservation(manuscript, props)
        # The manuscript is not the only source for every claim, so this is a
        # sanity floor, not an equality: the manuscript must carry most of what
        # it is cited for.
        self.assertGreater(result.preservation_rate, 0.6)
        empty = score_proposition_preservation("", props)
        self.assertEqual(empty.preservation_rate, 0.0)


class UnsupportedClaimRateTests(unittest.TestCase):
    def _prop(self, pid, anchors, status):
        return Proposition(
            id=pid,
            text=pid,
            document_ids=("doc",),
            expected_status=status,
            high_stakes=False,
            required=False,
            category="other",
            anchors=tuple(anchors),
        )

    def test_clean_candidate_scores_zero(self):
        props = [
            self._prop("good", ["90 days"], "supported"),
            self._prop("bad", ["120 days"], "contradicted"),
        ]
        result = score_unsupported_claim_rate("Calibration every 90 days.", props)
        self.assertEqual(result.asserted_supported_ids, ("good",))
        self.assertEqual(result.asserted_distractor_ids, ())
        self.assertEqual(result.unsupported_claim_rate, 0.0)
        self.assertEqual(result.total_asserted, 1)

    def test_half_bad_candidate_scores_one_half(self):
        props = [
            self._prop("good", ["90 days"], "supported"),
            self._prop("bad", ["120 days"], "contradicted"),
        ]
        result = score_unsupported_claim_rate("Either 90 days or 120 days.", props)
        self.assertEqual(result.unsupported_claim_rate, 0.5)
        self.assertEqual(result.asserted_by_status["contradicted"], ("bad",))

    def test_every_distractor_status_is_bucketed(self):
        props = [
            self._prop("c", ["cee"], "contradicted"),
            self._prop("u", ["you"], "unsupported"),
            self._prop("s", ["ess"], "stale"),
            self._prop("o", ["oh"], "opinion"),
        ]
        result = score_unsupported_claim_rate("cee you ess oh", props)
        self.assertEqual(result.unsupported_claim_rate, 1.0)
        self.assertEqual(result.asserted_by_status["contradicted"], ("c",))
        self.assertEqual(result.asserted_by_status["unsupported"], ("u",))
        self.assertEqual(result.asserted_by_status["stale"], ("s",))
        self.assertEqual(result.asserted_by_status["opinion"], ("o",))

    def test_ambiguous_propositions_are_excluded_entirely(self):
        props = [
            self._prop("amb", ["maybe"], "ambiguous"),
            self._prop("good", ["yes"], "supported"),
        ]
        result = score_unsupported_claim_rate("maybe yes", props)
        self.assertEqual(result.total_asserted, 1)
        self.assertEqual(result.asserted_supported_ids, ("good",))
        self.assertEqual(result.unsupported_claim_rate, 0.0)

    def test_silent_candidate_scores_zero(self):
        props = [self._prop("bad", ["120 days"], "contradicted")]
        self.assertEqual(score_unsupported_claim_rate("", props).unsupported_claim_rate, 0.0)

    def test_style_exemplar_false_facts_are_caught_on_the_real_corpus(self):
        corpus = load_corpus(FIXTURES_DIR)
        candidate = (
            "The TX-40 has been certified in 63 countries since 2018 and Halloway Ridge "
            "runs 400 probes across 30 plots."
        )
        result = score_unsupported_claim_rate(candidate, corpus.propositions)
        self.assertIn("p054", result.asserted_distractor_ids)
        self.assertIn("p056", result.asserted_distractor_ids)
        self.assertEqual(result.unsupported_claim_rate, 1.0)


class QuoteFidelityTests(unittest.TestCase):
    def _quote(self, qid, text):
        return ExactQuote(
            id=qid,
            document_id="doc",
            text=text,
            start=0,
            end=len(text),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            reason="unit test",
        )

    def _trap(self, tid, text, quote_id):
        return NearQuoteTrap(
            id=tid,
            document_id="doc",
            text=text,
            start=0,
            end=len(text),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trap_for_quote_id=quote_id,
            reason="unit test",
        )

    def test_exact_reproduction_scores_one(self):
        quotes = [self._quote("q1", "the crust is the cause.")]
        result = score_quote_fidelity("He wrote: the crust is the cause. Then stopped.", quotes)
        self.assertEqual(result.matched_ids, ("q1",))
        self.assertEqual(result.fidelity_rate, 1.0)

    def test_typographic_quote_glyphs_are_tolerated(self):
        quotes = [self._quote("q1", "“ninety days,” she said.")]
        result = score_quote_fidelity('He recorded "ninety days," she said.', quotes)
        self.assertEqual(result.matched_ids, ("q1",))

    def test_changed_punctuation_is_a_miss(self):
        quotes = [self._quote("q1", "crust — not firmware — caused it.")]
        result = score_quote_fidelity("crust, not firmware, caused it.", quotes)
        self.assertEqual(result.missing_ids, ("q1",))
        self.assertEqual(result.fidelity_rate, 0.0)

    def test_near_quote_traps_are_reported(self):
        quotes = [self._quote("q1", "ninety days ago.")]
        traps = [self._trap("t1", "90 days ago.", "q1")]
        result = score_quote_fidelity("It was 90 days ago.", quotes, traps)
        self.assertEqual(result.missing_ids, ("q1",))
        self.assertEqual(result.trap_hit_ids, ("t1",))

    def test_no_quotes_scores_one(self):
        self.assertEqual(score_quote_fidelity("anything", []).fidelity_rate, 1.0)

    def test_real_corpus_quotes_and_traps_do_not_collide(self):
        corpus = load_corpus(FIXTURES_DIR)
        source = corpus.text("doc_reference_quotes_source")
        result = score_quote_fidelity(source, corpus.exact_quotes, corpus.near_quote_traps)
        self.assertEqual(result.fidelity_rate, 1.0)
        self.assertEqual(result.trap_hit_ids, (), "a trap must not match the certified source")

        trap_doc = corpus.text("doc_reference_near_quote_trap")
        trapped = score_quote_fidelity(trap_doc, corpus.exact_quotes, corpus.near_quote_traps)
        self.assertEqual(trapped.matched_ids, (), "traps must not satisfy quote fidelity")
        self.assertEqual(len(trapped.trap_hit_ids), len(corpus.near_quote_traps))


class CitationMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(FIXTURES_DIR)

    def test_a_real_passage_in_the_cited_document_resolves(self):
        result = score_citation_accuracy(
            self.corpus,
            [("doc_reference_sensor_spec_v2", "Calibration interval shortened from 180 days")],
        )
        self.assertEqual(result.accuracy, 1.0)
        check = result.checks[0]
        self.assertTrue(check.document_resolved)
        self.assertTrue(check.passage_found)
        self.assertGreaterEqual(check.exact_offset, 0)

    def test_a_passage_from_a_different_document_fails(self):
        result = score_citation_accuracy(
            self.corpus,
            [("doc_reference_sensor_spec_v2", "Harrowgate recommends a calibration interval")],
        )
        self.assertEqual(result.accuracy, 0.0)
        self.assertTrue(result.checks[0].document_resolved)
        self.assertFalse(result.checks[0].passage_found)

    def test_an_unknown_label_fails_without_raising(self):
        result = score_citation_accuracy(self.corpus, [("no_such_doc", "anything")])
        self.assertEqual(result.accuracy, 0.0)
        self.assertFalse(result.checks[0].document_resolved)
        self.assertIsNone(result.checks[0].resolved_document_id)

    def test_labels_resolve_by_path_and_by_title(self):
        by_path = score_citation_accuracy(
            self.corpus, [("reference_safety_datasheet.md", "Units must never be incinerated.")]
        )
        self.assertEqual(by_path.accuracy, 1.0)
        by_title = score_citation_accuracy(
            self.corpus,
            [("Torvane TX-40 Field Safety Datasheet", "Units must never be incinerated.")],
        )
        self.assertEqual(by_title.accuracy, 1.0)

    def test_line_wrapping_in_the_citation_is_tolerated(self):
        result = score_citation_accuracy(
            self.corpus,
            [("doc_reference_safety_datasheet", "If an electrolyte odour is detected, evacuate and ventilate the enclosure before approaching.")],
        )
        self.assertTrue(result.checks[0].passage_found)
        self.assertEqual(result.checks[0].exact_offset, -1)

    def test_empty_citation_list_scores_one(self):
        self.assertEqual(score_citation_accuracy(self.corpus, []).accuracy, 1.0)

    def test_mixed_batch_reports_a_fraction(self):
        result = score_citation_accuracy(
            self.corpus,
            [
                ("doc_background_glossary", "A mineral deposit that forms on a frit"),
                ("doc_background_glossary", "Harrowgate recommends"),
            ],
        )
        self.assertEqual(result.total, 2)
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.accuracy, 0.5)


class MajorEditOutcomeTests(unittest.TestCase):
    def test_identical_text_has_no_major_edits(self):
        text = "Alpha stands. Beta stands. Gamma stands."
        result = score_major_edit_outcome(text, text)
        self.assertEqual(result.total_sentences, 3)
        self.assertEqual(result.changed_sentences, 0)
        self.assertEqual(result.fraction_changed, 0.0)

    def test_one_of_two_sentences_rewritten(self):
        source = "The probe was calibrated on Tuesday. The frit was clean."
        candidate = "The probe was calibrated on Tuesday. Everything else was replaced wholesale."
        result = score_major_edit_outcome(source, candidate)
        self.assertEqual(result.total_sentences, 2)
        self.assertEqual(result.changed_sentences, 1)
        self.assertEqual(result.changed_indexes, (1,))
        self.assertEqual(result.fraction_changed, 0.5)

    def test_total_rewrite_changes_everything(self):
        result = score_major_edit_outcome(
            "Alpha one. Beta two.", "Completely unrelated prose about weather."
        )
        self.assertEqual(result.fraction_changed, 1.0)

    def test_reformatting_is_not_a_major_edit(self):
        source = "The probe was calibrated on Tuesday."
        candidate = "the   probe was\ncalibrated on Tuesday."
        self.assertEqual(score_major_edit_outcome(source, candidate).fraction_changed, 0.0)

    def test_empty_manuscript_scores_zero(self):
        result = score_major_edit_outcome("", "anything at all")
        self.assertEqual(result.total_sentences, 0)
        self.assertEqual(result.fraction_changed, 0.0)

    def test_threshold_is_validated_and_honoured(self):
        with self.assertRaises(ValueError):
            score_major_edit_outcome("a. b.", "a. b.", similarity_threshold=0.0)
        with self.assertRaises(ValueError):
            score_major_edit_outcome("a. b.", "a. b.", similarity_threshold=1.5)
        source = "The frit was clean."
        candidate = "The frit was cleaned."
        lenient = score_major_edit_outcome(source, candidate, similarity_threshold=0.8)
        strict = score_major_edit_outcome(source, candidate, similarity_threshold=1.0)
        self.assertEqual(lenient.changed_sentences, 0)
        self.assertEqual(strict.changed_sentences, 1)

    def test_against_the_real_manuscript(self):
        corpus = load_corpus(FIXTURES_DIR)
        manuscript = corpus.text("doc_manuscript_field_report")
        self.assertEqual(score_major_edit_outcome(manuscript, manuscript).fraction_changed, 0.0)
        self.assertEqual(score_major_edit_outcome(manuscript, "").fraction_changed, 1.0)


if __name__ == "__main__":
    unittest.main()
