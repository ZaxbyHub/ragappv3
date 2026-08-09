"""Tests for multi-modal artifact identity extraction (issue #462).

Covers the pure helpers that lift artifact identity out of proxy metadata
(atom_id / modality / asset_id / bounded bbox) and the artifact-aware dedup key
that prevents identity collapse for byte-identical artifact proxy texts.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.document_retrieval import (
    DocumentRetrievalService,
    RAGSource,
    _bounded_bbox,
    artifact_fields_from_metadata,
    artifact_id_from_metadata,
    artifact_modality_from_metadata,
    source_dedup_key,
)

ART = {
    "atom_id": "atom-1",
    "artifact_id": "legacy-alias",  # ignored in favor of atom_id
    "atom_kind": "image",
    "asset_id": "asset-1",
    "bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
    "description": "A chart of revenue",
    "some_other": "keep",
}


class TestArtifactIdFromMetadata(unittest.TestCase):
    def test_atom_id_preferred(self) -> None:
        self.assertEqual(artifact_id_from_metadata(ART), "atom-1")

    def test_artifact_id_alias(self) -> None:
        m = dict(ART)
        m.pop("atom_id")
        self.assertEqual(artifact_id_from_metadata(m), "legacy-alias")

    def test_none_absent(self) -> None:
        self.assertIsNone(artifact_id_from_metadata({"other": 1}))
        self.assertIsNone(artifact_id_from_metadata(None))
        self.assertIsNone(artifact_id_from_metadata(""))

    def test_json_string_metadata_parsed(self) -> None:
        self.assertEqual(artifact_id_from_metadata(json.dumps(ART)), "atom-1")
        self.assertIsNone(artifact_id_from_metadata("not json"))


class TestArtifactModalityFromMetadata(unittest.TestCase):
    def test_atom_kind_preferred(self) -> None:
        self.assertEqual(artifact_modality_from_metadata(ART), "image")

    def test_modality_alias(self) -> None:
        m = {"artifact_id": "a", "modality": "chart"}
        self.assertEqual(artifact_modality_from_metadata(m), "chart")

    def test_none_absent(self) -> None:
        self.assertIsNone(artifact_modality_from_metadata({}))


class TestBoundedBbox(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(_bounded_bbox({"x0": 1, "y0": 2, "x1": 3, "y1": 4}),
                         {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0})

    def test_extra_keys_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox({"x0": 0, "y0": 0, "x1": 1, "y1": 1, "z": 9}))

    def test_negative_width_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox({"x0": 5, "y0": 0, "x1": 1, "y1": 1}))

    def test_non_numeric_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox({"x0": "a", "y0": 0, "x1": 1, "y1": 1}))

    def test_nan_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox({"x0": float("nan"), "y0": 0, "x1": 1, "y1": 1}))

    def test_oversize_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox({"x0": 1e9, "y0": 0, "x1": 1, "y1": 1}))

    def test_non_dict_rejected(self) -> None:
        self.assertIsNone(_bounded_bbox("[1,2,3,4]"))
        self.assertIsNone(_bounded_bbox(None))


class TestArtifactFieldsFromMetadata(unittest.TestCase):
    def test_extracts_fields(self) -> None:
        fields = artifact_fields_from_metadata(ART)
        self.assertEqual(fields["artifact_id"], "atom-1")
        self.assertEqual(fields["modality"], "image")
        self.assertEqual(fields["asset_id"], "asset-1")
        self.assertEqual(fields["bbox"], {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0})
        self.assertEqual(fields["description"], "A chart of revenue")

    def test_absent_fields_are_none(self) -> None:
        fields = artifact_fields_from_metadata({})
        for k in ("artifact_id", "modality", "asset_id", "bbox", "description"):
            self.assertIsNone(fields[k])

    def test_json_string(self) -> None:
        fields = artifact_fields_from_metadata(json.dumps(ART))
        self.assertEqual(fields["artifact_id"], "atom-1")


class TestSourceDedupKey(unittest.TestCase):
    def test_artifact_proxy_kept_distinct_from_chunk(self) -> None:
        # Same (file_id, text) but one is an artifact proxy: keys must differ.
        chunk_key = source_dedup_key("f1", "same text", {})
        art_key = source_dedup_key("f1", "same text", {"atom_id": "a1"})
        self.assertNotEqual(chunk_key, art_key)

    def test_two_artifacts_same_text_stay_distinct(self) -> None:
        k1 = source_dedup_key("f1", "same", {"atom_id": "a1"})
        k2 = source_dedup_key("f1", "same", {"atom_id": "a2"})
        self.assertNotEqual(k1, k2)

    def test_plain_chunk_falls_back(self) -> None:
        self.assertEqual(
            source_dedup_key("f1", "abc", {}),
            source_dedup_key("f1", "abc", None),
        )


class TestRAGSourceIdentity(unittest.TestCase):
    def _chunk(self, **overrides):
        fields = {"text": "t", "file_id": "f1", "score": 0.5, "metadata": {}}
        fields.update(overrides)
        return RAGSource(**fields)

    def test_artifact_identity_key(self) -> None:
        src = self._chunk(artifact_id="a1")
        self.assertEqual(src.artifact_identity_key(), ("artifact", "a1", "t"))

    def test_chunk_identity_key(self) -> None:
        self.assertEqual(self._chunk().artifact_identity_key(), ("chunk", "f1", "t"))


class TestSourceMetadataSerialization(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = DocumentRetrievalService.__new__(DocumentRetrievalService)

    def _chunk(self):
        c = RAGSource(
            text="t", file_id="f1", score=0.5,
            metadata={"source_file": "doc.pdf", "chunk_index": "0"},
        )
        c.artifact_id = "a1"
        c.modality = "chart"
        c.asset_id = "asset-1"
        c.bbox = {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0}
        c.description = "desc"
        c.vision_status = "used"
        c.vision_observation = "internal secret"
        return c

    def test_artifact_fields_emitted(self) -> None:
        out = self.svc.to_source_metadata(self._chunk(), source_index=2)
        self.assertEqual(out["artifact_id"], "a1")
        self.assertEqual(out["modality"], "chart")
        self.assertEqual(out["asset_id"], "asset-1")
        self.assertEqual(out["bbox"], {"x0": 0.0, "y0": 0.0, "x1": 5.0, "y1": 5.0})
        self.assertEqual(out["vision_status"], "used")

    def test_observation_never_serialized(self) -> None:
        """V2/V4: internal observation must never reach the wire/source JSON."""
        out = self.svc.to_source_metadata(self._chunk(), source_index=2)
        self.assertNotIn("vision_observation", out)
        self.assertNotIn("_vision_observation", out)
        self.assertNotIn("internal secret", json.dumps(out))

    def test_plain_chunk_has_no_artifact_fields(self) -> None:
        out = self.svc.to_source_metadata(
            RAGSource(text="t", file_id="f1", score=0.5, metadata={}),
            source_index=1,
        )
        self.assertNotIn("artifact_id", out)
        self.assertNotIn("modality", out)
        self.assertNotIn("vision_status", out)


if __name__ == "__main__":
    unittest.main()
