"""Current-format (reupload-safe hash) chunk identity through the retrieval
window-expansion path (issue #510 RAG-002).

Production writes reupload-safe vector records with id
``{file_id}_{hash8}_{chunk_scale}_{chunk_index}`` while the chunk *metadata*
``chunk_uid`` stays in the legacy ``{file_id}_{scale}_{index}`` /
``{file_id}_{index}`` shape. Window expansion must therefore:

- derive the original source's dedup uid from the PERSISTED record identity
  (``metadata._chunk_id`` / ``chunk_uid``), not from a reconstructed legacy
  uid, so the center passage is not duplicated by the adjacent-chunk fetch;
- carry the exact stored record id onto adjacent chunks so every serialized
  source ``id`` resolves against the real LanceDB row set;
- keep the historical legacy-format and multi-scale dedup semantics intact.
"""

import pytest

from app.services.document_retrieval import (
    DocumentRetrievalService,
    RAGSource,
    _normalize_uid_for_dedup,
    _strip_reupload_hash,
)

_HASH = "ab12cd34"
_OTHER_HASH = "ef56ab90"


class FakeUidStore:
    """Fake vector store serving rows keyed by their exact record id."""

    def __init__(self, rows):
        self._rows = {row["id"]: row for row in rows}
        self.fetch_calls = []

    @property
    def id_set(self):
        return set(self._rows)

    async def get_chunks_by_uid(self, chunk_uids):
        self.fetch_calls.append(list(chunk_uids))
        return [dict(self._rows[uid]) for uid in chunk_uids if uid in self._rows]


def _current_row(file_id: str, idx: int, text: str, hash8: str = _HASH):
    """One current-format (reupload-safe) vector record for a default-scale chunk."""
    return {
        "id": f"{file_id}_{hash8}_default_{idx}",
        "file_id": file_id,
        "text": text,
        "_distance": 0.2,
        "metadata": {
            "chunk_uid": f"{file_id}_default_{idx}",
            "chunk_index": f"default_{idx}",
            "chunk_scale": "default",
        },
    }


def _current_hit(file_id: str, idx: int, text: str, hash8: str = _HASH):
    """A top-k hit record for ``_current_row`` (id column + nested metadata)."""
    row = _current_row(file_id, idx, text, hash8=hash8)
    row["_distance"] = 0.1
    return row


def _legacy_row(file_id: str, idx: int, text: str):
    return {
        "id": f"{file_id}_{idx}",
        "file_id": file_id,
        "text": text,
        "_distance": 0.2,
        "metadata": {"chunk_uid": f"{file_id}_{idx}", "chunk_index": idx},
    }


def _legacy_hit(file_id: str, idx: int, text: str):
    row = _legacy_row(file_id, idx, text)
    row["_distance"] = 0.1
    return row


def _service(store, window=1, top_k=100):
    return DocumentRetrievalService(
        vector_store=store,
        max_distance_threshold=1.0,
        retrieval_top_k=top_k,
        retrieval_window=window,
    )


class TestCurrentFormatExpandWindow:
    """filter_relevant + expand_window over hash-prefixed current-format rows."""

    @pytest.mark.asyncio
    async def test_center_passage_appears_exactly_once(self):
        """The center chunk must not be re-added as its own 'adjacent' fetch.

        Reconstructing a hash-less dedup uid for a hash-prefixed record (the
        pre-#510 behavior) never collides with the adjacent-fetch uid of the
        same row, duplicating the center passage in the expanded list.
        """
        rows = [
            _current_row("42", 0, "File 42 chunk zero talks about intake."),
            _current_row("42", 1, "File 42 chunk one is the matched center."),
            _current_row("42", 2, "File 42 chunk two wraps up the section."),
        ]
        store = FakeUidStore(rows)
        service = _service(store, window=1)

        sources = await service.filter_relevant([_current_hit("42", 1, rows[1]["text"])])

        center_texts = [s.text for s in sources if "matched center" in s.text]
        assert len(center_texts) == 1, (
            f"Center passage duplicated after window expansion: "
            f"{[s.text for s in sources]}"
        )
        # Window=1 around chunk 1: indices 0 and 2 are fetched too.
        texts = [s.text for s in sources]
        assert any("chunk zero" in t for t in texts)
        assert any("chunk two" in t for t in texts)

    @pytest.mark.asyncio
    async def test_serialized_ids_resolve_against_exact_store_id_set(self):
        """Every to_source_metadata()['id'] must be a real row id in the store."""
        rows = [_current_row("42", i, f"File 42 chunk {i} body text.") for i in range(3)]
        store = FakeUidStore(rows)
        service = _service(store, window=1)

        sources = await service.filter_relevant([_current_hit("42", 1, rows[1]["text"])])
        assert sources

        for idx, src in enumerate(sources, start=1):
            meta = service.to_source_metadata(src, source_index=idx)
            assert meta["id"] in store.id_set, (
                f"Serialized source id {meta['id']!r} does not resolve to any "
                f"stored row id (store ids: {sorted(store.id_set)})"
            )
            assert meta["source_label"] == f"S{idx}"

    @pytest.mark.asyncio
    async def test_equal_text_sources_from_different_files_stay_distinct(self):
        """Two files sharing byte-identical chunk text must both survive."""
        shared = "Identical boilerplate passage present in two separate files."
        rows = (
            [_current_row("42", i, f"File 42 unique neighbor {i}.") for i in (0, 2)]
            + [_current_row("43", i, f"File 43 unique neighbor {i}.", hash8=_OTHER_HASH)
               for i in (0, 2)]
            + [
                _current_row("42", 1, shared),
                _current_row("43", 1, shared, hash8=_OTHER_HASH),
            ]
        )
        store = FakeUidStore(rows)
        service = _service(store, window=1)

        sources = await service.filter_relevant(
            [
                _current_hit("42", 1, shared),
                _current_hit("43", 1, shared, hash8=_OTHER_HASH),
            ]
        )

        shared_sources = [s for s in sources if s.text == shared]
        assert len(shared_sources) == 2, (
            f"Expected both files' shared-text chunks to survive, got "
            f"{len(shared_sources)}: {[(s.file_id, s.text) for s in sources]}"
        )
        assert {s.file_id for s in shared_sources} == {"42", "43"}
        serialized = [
            service.to_source_metadata(s, source_index=i + 1)["id"]
            for i, s in enumerate(sources)
        ]
        assert f"42_{_HASH}_default_1" in serialized
        assert f"43_{_OTHER_HASH}_default_1" in serialized

    @pytest.mark.asyncio
    async def test_legacy_format_still_expands_and_dedups(self):
        """Legacy {file_id}_{index} rows keep the pre-existing behavior."""
        rows = [_legacy_row("7", i, f"Legacy file 7 chunk {i}.") for i in range(3)]
        store = FakeUidStore(rows)
        service = _service(store, window=1)

        sources = await service.filter_relevant([_legacy_hit("7", 1, rows[1]["text"])])

        center_texts = [s.text for s in sources if "chunk 1" in s.text]
        assert len(center_texts) == 1
        for idx, src in enumerate(sources, start=1):
            meta = service.to_source_metadata(src, source_index=idx)
            assert meta["id"] in store.id_set

    @pytest.mark.asyncio
    async def test_multi_scale_current_format_dedups(self):
        """Hash-prefixed multi-scale rows ({id}_{hash}_{scale}_{idx}) dedup."""
        rows = [
            {
                "id": f"9_{_HASH}_512_{i}",
                "file_id": "9",
                "text": f"Multi-scale file 9 chunk {i} body.",
                "_distance": 0.2,
                "metadata": {
                    "chunk_uid": f"9_512_{i}",
                    "chunk_index": f"512_{i}",
                    "chunk_scale": "512",
                },
            }
            for i in (2, 3, 4)
        ]
        store = FakeUidStore(rows)
        service = _service(store, window=1)

        results = [dict(rows[1], _distance=0.1)]
        sources = await service.filter_relevant(results)

        center_texts = [s.text for s in sources if "chunk 3" in s.text]
        assert len(center_texts) == 1
        assert len(sources) == 3  # indices 2, 3, 4
        for idx, src in enumerate(sources, start=1):
            meta = service.to_source_metadata(src, source_index=idx)
            assert meta["id"] in store.id_set

    @pytest.mark.asyncio
    async def test_mixed_format_integration(self):
        """A current-format file and a legacy file expand independently."""
        current_rows = [
            _current_row("42", i, f"Current file 42 chunk {i}.") for i in range(3)
        ]
        legacy_rows = [
            _legacy_row("7", i, f"Legacy file 7 chunk {i}.") for i in range(3)
        ]
        store = FakeUidStore(current_rows + legacy_rows)
        service = _service(store, window=1)

        sources = await service.filter_relevant(
            [
                _current_hit("42", 1, current_rows[1]["text"]),
                _legacy_hit("7", 1, legacy_rows[1]["text"]),
            ]
        )

        # Each file contributes its center + both neighbors (window=1).
        assert len(sources) == 6
        per_file = {}
        for src in sources:
            per_file.setdefault(src.file_id, []).append(src.text)
        assert len(per_file["42"]) == 3
        assert len(per_file["7"]) == 3
        # Exactly one center per file.
        assert sum(1 for t in per_file["42"] if "chunk 1" in t) == 1
        assert sum(1 for t in per_file["7"] if "chunk 1" in t) == 1
        for idx, src in enumerate(sources, start=1):
            meta = service.to_source_metadata(src, source_index=idx)
            assert meta["id"] in store.id_set


# ---------------------------------------------------------------------------
# Unit tests: _strip_reupload_hash / _normalize_uid_for_dedup
# ---------------------------------------------------------------------------

# The 17 pre-existing normalize semantics (mirrors test_normalize_uid_dedup.py,
# which must keep passing — the hash leg runs AHEAD of the numeric leg and must
# not change any of these outcomes).
_LEGACY_NORMALIZE_CASES = [
    ("doc1_512_3", "doc1_3"),          # multi-scale strips scale
    ("doc1_3", "doc1_3"),              # default uid unchanged
    ("my_file_v2_3", "my_file_v2_3"),  # non-numeric middle unchanged
    ("doc1_abc", "doc1_abc"),          # non-numeric last unchanged
    ("", ""),                          # empty
    ("doc1", "doc1"),                  # single segment
    ("file_1024_99", "file_99"),       # large scale
    ("file_5_10", "file_10"),          # two numeric segments
    ("myfile_5", "myfile_5"),          # two-part uid
    ("123_512_456", "123_456"),        # numeric file id
    ("doc_0_0", "doc_0"),              # zeros
    ("doc_-5_10", "doc_10"),           # negative middle parses as numeric
    ("doc_999999_1", "doc_1"),         # very large scale
    ("my_file_name_512_3", "my_file_name_3"),  # underscore in file id
    ("_", "_"),                        # only underscores
    ("doc_", "doc_"),                  # trailing underscore
    ("_doc_512_3", "_doc_3"),          # leading underscore
]


class TestStripReuploadHash:
    def test_strips_hash_from_current_format(self):
        assert _strip_reupload_hash(f"42_{_HASH}_default_0") == "42_default_0"
        assert _strip_reupload_hash(f"42_{_HASH}_512_3") == "42_512_3"

    def test_legacy_ids_unchanged(self):
        assert _strip_reupload_hash("42_default_0") == "42_default_0"
        assert _strip_reupload_hash("42_0") == "42_0"
        assert _strip_reupload_hash("42_512_3") == "42_512_3"

    def test_non_hex8_second_segment_unchanged(self):
        # "zz12cd34" is not 8-hex — no reupload hash leg to strip.
        assert _strip_reupload_hash("42_zz12cd34_default_0") == "42_zz12cd34_default_0"

    def test_too_few_segments_unchanged(self):
        assert _strip_reupload_hash(f"42_{_HASH}_0") == f"42_{_HASH}_0"
        assert _strip_reupload_hash("") == ""


class TestNormalizeUidForDedupLegacySemantics:
    @pytest.mark.parametrize("uid,expected", _LEGACY_NORMALIZE_CASES)
    def test_legacy_case(self, uid, expected):
        assert _normalize_uid_for_dedup(uid) == expected


class TestNormalizeUidForDedupHashSemantics:
    def test_hash_prefixed_default_collapses_with_legacy(self):
        assert (
            _normalize_uid_for_dedup(f"42_{_HASH}_default_0")
            == _normalize_uid_for_dedup("42_default_0")
            == "42_default_0"
        )

    def test_hash_prefixed_multi_scale_collapses_with_legacy(self):
        assert (
            _normalize_uid_for_dedup(f"42_{_HASH}_512_3")
            == _normalize_uid_for_dedup("42_512_3")
            == "42_3"
        )

    def test_hash_leg_does_not_break_underscore_file_ids(self):
        assert (
            _normalize_uid_for_dedup(f"my_file_{_HASH}_512_3")
            == _normalize_uid_for_dedup("my_file_512_3")
            == "my_file_3"
        )

    def test_non_hex8_segment_falls_through_to_numeric_leg(self):
        # "512" IS the scale here; "zz12cd34" is not hex8 so nothing strips.
        assert _normalize_uid_for_dedup("42_zz12cd34_default_0") == "42_zz12cd34_default_0"

    def test_idempotent_on_current_format(self):
        once = _normalize_uid_for_dedup(f"42_{_HASH}_default_0")
        assert _normalize_uid_for_dedup(once) == once


import unittest  # noqa: E402


class TestStripReuploadHashNegativeCases(unittest.TestCase):
    """PRR-008: a purely numeric 8-char legacy scale segment must never be
    mistaken for a reupload hash (regex requires at least one [a-f] letter)."""

    def test_all_digit_eight_char_scale_segment_not_stripped(self):
        from app.services.document_retrieval import _strip_reupload_hash

        self.assertEqual(
            _strip_reupload_hash("file1_12345678_99_3"), "file1_12345678_99_3"
        )

    def test_real_hash8_segment_still_stripped(self):
        from app.services.document_retrieval import _strip_reupload_hash

        self.assertEqual(
            _strip_reupload_hash("file1_ab12cd34_default_0"),
            "file1_default_0",
        )
