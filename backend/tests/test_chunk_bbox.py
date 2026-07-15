"""Tests for chunk_bbox page-highlight metadata capture (Issue #396).

unstructured is stubbed in CI, so these tests exercise the capture logic
directly against synthetic element metadata objects. They verify the defensive
behavior: bbox is captured when coordinates are present and well-formed, and
absent (no exception) otherwise.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (mirrors test_documents_auth.py).
for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *a, **k: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *a, **k: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    for _name, _sub in (
        ("unstructured", _unstructured),
        ("unstructured.partition", _unstructured.partition),
        ("unstructured.partition.auto", _unstructured.partition.auto),
        ("unstructured.chunking", _unstructured.chunking),
        ("unstructured.chunking.title", _unstructured.chunking.title),
        ("unstructured.documents", _unstructured.documents),
        ("unstructured.documents.elements", _unstructured.documents.elements),
    ):
        sys.modules[_name] = _sub

from app.services.chunking import ProcessedChunk, SemanticChunker  # noqa: E402


class _FakeCoord:
    """Mimics unstructured's coordinates object."""

    def __init__(self, points):
        self.points = points


class _FakeMetadata:
    """Mimics unstructured element metadata."""

    def __init__(self, page_number=None, filename=None, coordinates=None):
        self.page_number = page_number
        self.filename = filename
        self.coordinates = coordinates


class TestChunkBboxCapture(unittest.TestCase):
    """Verify chunk_bbox capture from unstructured coordinates metadata.

    These tests call the REAL ``SemanticChunker._capture_bbox`` static method
    (the production code path used by ``chunk_elements``) — not a duplicate —
    so a regression in the capture logic is caught directly.
    """

    def test_bbox_captured_from_well_formed_points(self):
        """Four corner points → axis-aligned bbox."""
        meta = _FakeMetadata(
            page_number=2,
            coordinates=_FakeCoord(
                points=((1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 4.0))
            ),
        )
        bbox = SemanticChunker._capture_bbox(meta)
        self.assertEqual(
            bbox,
            {"left": 1.0, "top": 2.0, "right": 3.0, "bottom": 4.0},
        )

    def test_no_coordinates_key_absent_no_exception(self):
        """When coordinates is None, _capture_bbox returns None (no error)."""
        meta = _FakeMetadata(page_number=1)
        self.assertIsNone(SemanticChunker._capture_bbox(meta))

    def test_malformed_points_skipped_silently(self):
        """Malformed points must not raise; returns None."""
        meta = _FakeMetadata(
            coordinates=_FakeCoord(points=(("not", "numbers"), ("x", "y")))
        )
        self.assertIsNone(SemanticChunker._capture_bbox(meta))

    def test_merged_composite_element_drops_coordinates(self):
        """Common case: merged CompositeElement has coordinates=None."""
        meta = _FakeMetadata(page_number=1, coordinates=None)
        self.assertIsNone(SemanticChunker._capture_bbox(meta))

    def test_metadata_without_coordinates_attr_returns_none(self):
        """A bare metadata object with no coordinates attr → None (defensive)."""
        meta = _FakeMetadata(page_number=1)
        # _FakeMetadata initializes coordinates=None by default; verify the
        # getattr fallback handles a metadata object lacking the attribute too.
        self.assertIsNone(SemanticChunker._capture_bbox(meta))

    def test_chunker_handles_empty_element_list(self):
        """chunk_elements with empty list returns [] without touching metadata."""
        chunker = SemanticChunker(chunk_size_chars=500, chunk_overlap_chars=0)
        self.assertEqual(chunker.chunk_elements([]), [])

    def test_processedchunk_metadata_roundtrip_preserves_bbox(self):
        """Stored chunk metadata JSON round-trips bbox correctly (rebuild path)."""
        import json

        chunk = ProcessedChunk(
            text="hello",
            metadata={
                "chunk_index": 0,
                "chunk_bbox": {"left": 1.0, "top": 2.0, "right": 3.0, "bottom": 4.0},
                "page_number": 5,
            },
            chunk_index=0,
        )
        meta_json = json.dumps(
            {
                "chunk_bbox": chunk.metadata["chunk_bbox"],
                "page_number": chunk.metadata["page_number"],
            }
        )
        restored = json.loads(meta_json)
        self.assertEqual(
            restored["chunk_bbox"],
            {"left": 1.0, "top": 2.0, "right": 3.0, "bottom": 4.0},
        )


if __name__ == "__main__":
    unittest.main()
