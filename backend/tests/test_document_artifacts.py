"""Tests for the parser-neutral document atom foundation (issue #460).

Covers: ordered atom extraction from parser elements, unknown-category safe
degradation, the pure text projection (byte-for-byte equivalence with the
pre-existing normalization), generation fingerprints, and generation validation.
These are pure unit tests with no DB/vector/filesystem side effects.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.document_artifacts import (
    AtomKind,
    DocumentAtom,
    compute_generation_hash,
    make_atom_id,
    parse_elements_to_atoms,
    project_text,
    raw_join,
    validate_generation_atoms,
)


class _Meta:
    def __init__(self, **kwargs):
        self.section = kwargs.get("section")
        self.page_number = kwargs.get("page_number")
        self.coordinates = kwargs.get("coordinates")


class _Coords:
    def __init__(self, points):
        self.points = points


class _Element:
    def __init__(self, category, text, **meta_kwargs):
        self.category = category
        self._text = text
        self.metadata = _Meta(**meta_kwargs)

    def __str__(self):
        return self._text


def _sample_elements():
    return [
        _Element("Title", "  Heading One  ", section="sec", page_number=1),
        _Element(
            "NarrativeText",
            "Body line\r\nwith trailing space \nand CRLF.",
            page_number=1,
            coordinates=_Coords([(0, 0), (100, 0), (100, 50), (0, 50)]),
        ),
        _Element("ListItem", "- a list item", section="sec", page_number=2),
        _Element("TotallyUnknown", "mystery", page_number=2),
    ]


@contextmanager
def _real_unstructured_graph():
    """Yield with the REAL unstructured importable, evicting the conftest
    stub graph (mirrors test_real_docs_parsing.py's eviction/restoration:
    an unconditional import would bind the synthetic conftest graph and
    prove nothing about the real category vocabulary)."""
    stub = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "unstructured" or name.startswith("unstructured.")
    }
    for name in stub:
        del sys.modules[name]
    try:
        import unstructured as real_unstructured
        from unstructured.documents import elements as real_elements  # noqa: F401

        if not getattr(real_unstructured, "__file__", None):
            raise ModuleNotFoundError("only a synthetic test stub is present")
        yield real_elements
    finally:
        for name in [
            n for n in sys.modules if n == "unstructured" or n.startswith("unstructured.")
        ]:
            del sys.modules[name]
        sys.modules.update(stub)


class TestRealUnstructuredVocabulary:
    """#745 AC10 (T1-25-KR-08): feed REAL unstructured.documents.elements
    classes (not hand-built _Element fakes) to parse_elements_to_atoms and
    assert the real category strings map to the documented atom kinds.
    Standard CI installs no unstructured (conftest stubs it) so this skips
    there; the nightly full-dependency job installs unstructured[all-docs]
    and runs it for real (nightly.yml full-deps job), with a no-skip gate
    step proving the run happened."""

    def test_real_element_categories_map_to_atom_kinds(self):
        try:
            with _real_unstructured_graph() as elements:
                Title = elements.Title
                NarrativeText = elements.NarrativeText
                ListItem = elements.ListItem
        except ImportError as exc:  # pragma: no cover - standard CI tier
            pytest.skip(
                "real unstructured not installed in this tier; the nightly "
                f"full-deps job runs this test for real ({type(exc).__name__}: {exc})"
            )
        real_els = [
            Title("Real Heading"),
            NarrativeText("Real body text."),
            ListItem("real list item"),
        ]
        atoms = parse_elements_to_atoms(
            real_els, file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        assert [a.kind for a in atoms] == [
            AtomKind.TITLE,
            AtomKind.TEXT,
            AtomKind.LIST,
        ], (
            "the REAL unstructured category vocabulary no longer maps onto "
            "the adapter's atom kinds — update the adapter (and its "
            "hand-built-fake sibling tests) to the real contract"
        )


class TestAdapter:
    def test_ordered_atoms_with_kinds_and_metadata(self):
        elements = _sample_elements()
        atoms = parse_elements_to_atoms(
            elements, file_id=7, generation_hash="gen", parser_fingerprint="p1"
        )
        assert [a.kind for a in atoms] == [
            AtomKind.TITLE,
            AtomKind.TEXT,
            AtomKind.LIST,
            AtomKind.UNKNOWN,
        ]
        assert [a.ordinal for a in atoms] == [0, 1, 2, 3]
        assert atoms[0].raw_text == "  Heading One  "
        assert atoms[0].page_number == 1
        assert atoms[0].section_path == ("sec",)
        # bbox normalized from coordinates
        assert atoms[1].bbox == {
            "left": 0,
            "top": 0,
            "right": 100,
            "bottom": 50,
        }
        assert atoms[1].bbox_coord_system == "unstructured_points"

    def test_unknown_category_degrades_safely_with_warning(self):
        elements = [_Element("MysteryType", "x")]
        atoms = parse_elements_to_atoms(
            elements, file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        assert atoms[0].kind is AtomKind.UNKNOWN
        assert any("unknown_category" in w for w in atoms[0].warnings)

    def test_atom_ids_stable_and_unique(self):
        elements = _sample_elements()
        a1 = parse_elements_to_atoms(
            elements, file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        a2 = parse_elements_to_atoms(
            elements, file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        assert [x.atom_id for x in a1] == [x.atom_id for x in a2]
        assert len({x.atom_id for x in a1}) == len(a1)

    def test_schema_version_set(self):
        atoms = parse_elements_to_atoms(
            [_Element("Title", "t")], file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        assert atoms[0].schema_version == 1


class TestProjection:
    def test_normalized_projection_matches_legacy_formula(self):
        elements = _sample_elements()

        def legacy(elems):
            text = "\n".join(str(e) for e in elems)
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            return "\n".join(line.rstrip() for line in normalized.split("\n"))

        atoms = parse_elements_to_atoms(
            elements, file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        # Byte-for-byte equivalence with the pre-existing DocumentExtractionService
        # formula (line-ending + trailing-whitespace behavior).
        assert project_text(atoms) == legacy(elements)
        assert project_text(atoms).endswith("mystery")
        assert "\r\n" not in project_text(atoms)

    def test_raw_join_preserves_raw_text_order(self):
        atoms = parse_elements_to_atoms(
            _sample_elements(), file_id=1, generation_hash="g", parser_fingerprint="p"
        )
        joined = raw_join(atoms)
        assert joined.startswith("  Heading One  ")
        assert "CRLF." in joined


class TestGenerationFingerprint:
    def test_deterministic(self):
        h1 = compute_generation_hash("abc", parser_name="unstructured")
        h2 = compute_generation_hash("abc", parser_name="unstructured")
        assert h1 == h2

    def test_changes_on_semantic_inputs(self):
        base = compute_generation_hash("abc", parser_version="1")
        diff_parser = compute_generation_hash("abc", parser_version="2")
        diff_schema = compute_generation_hash(
            "abc", parser_version="1", schema_version=2
        )
        diff_bytes = compute_generation_hash("abd", parser_version="1")
        assert base != diff_parser
        assert base != diff_schema
        assert base != diff_bytes


class TestMakeAtomId:
    def test_stable(self):
        assert make_atom_id(1, "g", 0) == make_atom_id(1, "g", 0)

    def test_distinguishes_ordinals(self):
        assert make_atom_id(1, "g", 0) != make_atom_id(1, "g", 1)


def _atom(ordinal, atom_id, kind=AtomKind.TEXT, parent=None, raw="x"):
    return DocumentAtom(
        atom_id=atom_id,
        schema_version=1,
        file_id=1,
        generation_hash="g",
        ordinal=ordinal,
        kind=kind,
        raw_text=raw,
        parent_atom_id=parent,
        parser_fingerprint="p",
    )


class TestValidateGeneration:
    def test_valid_generation_passes(self):
        atoms = [_atom(0, "a0"), _atom(1, "a1")]
        assert validate_generation_atoms(atoms) is None

    def test_accepts_any_order_input(self):
        # validate_generation_atoms takes an iterable; order is enforced by
        # ordinal (0..n-1).
        atoms = [_atom(0, "a0"), _atom(1, "a1")]
        assert validate_generation_atoms(atoms) is None

    def test_out_of_order_ordinal_fails(self):
        atoms = [_atom(1, "a1")]
        error = validate_generation_atoms(atoms)
        assert error is not None and "ordinal" in error

    def test_duplicate_id_fails(self):
        atoms = [_atom(0, "dup"), _atom(1, "dup")]
        assert validate_generation_atoms(atoms) is not None

    def test_parent_outside_generation_fails(self):
        atoms = [_atom(0, "a0", parent="ghost")]
        assert validate_generation_atoms(atoms) is not None

    def test_valid_parent_passes(self):
        atoms = [_atom(0, "a0"), _atom(1, "a1", parent="a0")]
        assert validate_generation_atoms(atoms) is None

    def test_empty_verbatim_kind_fails(self):
        atoms = [
            DocumentAtom(
                atom_id="t0",
                schema_version=1,
                file_id=1,
                generation_hash="g",
                ordinal=0,
                kind=AtomKind.TABLE,
                raw_text="   ",
                parser_fingerprint="p",
            )
        ]
        assert validate_generation_atoms(atoms) is not None
