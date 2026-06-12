"""Tests for RRF recency scoring in fusion.py."""

import pytest

from app.utils.fusion import rrf_fuse


class TestRRFRecency:
    """Tests for RRF recency scoring feature."""

    def test_rrf_fuse_basic_no_recency(self):
        """Existing behavior unchanged when no recency provided."""
        result_lists = [
            [{"id": "a", "text": "doc a"}, {"id": "b", "text": "doc b"}],
            [{"id": "b", "text": "doc b"}, {"id": "c", "text": "doc c"}],
        ]
        result = rrf_fuse(result_lists, k=60)

        # Should deduplicate and sort by RRF score
        ids = [r["id"] for r in result]
        assert ids == ["b", "a", "c"]
        # b appears in both lists, gets highest score
        assert result[0]["_rrf_score"] > result[1]["_rrf_score"]
        assert result[1]["_rrf_score"] > result[2]["_rrf_score"]

    def test_rrf_fuse_recency_boosts_recent(self):
        """Recent docs score higher when recency_weight > 0."""
        result_lists = [
            [{"id": "old", "text": "old doc"}, {"id": "new", "text": "new doc"}],
        ]
        # Old doc has low recency, new doc has high recency
        recency_scores = {"old": 0.1, "new": 0.9}
        result = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=0.5
        )

        ids = [r["id"] for r in result]
        # New should rank higher due to recency boost
        assert ids.index("new") < ids.index("old")

    def test_rrf_fuse_recency_weight_zero_ignores_recency(self):
        """weight=0.0 ignores recency_scores completely."""
        result_lists = [
            [{"id": "a", "text": "doc a"}, {"id": "b", "text": "doc b"}],
        ]
        # recency_scores would boost 'a' if weight > 0
        recency_scores = {"a": 1.0, "b": 0.0}

        result_with_recency = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=0.0
        )
        result_without_recency = rrf_fuse(result_lists, k=60)

        # Should be identical since weight=0
        ids_with = [r["id"] for r in result_with_recency]
        ids_without = [r["id"] for r in result_without_recency]
        assert ids_with == ids_without
        assert (
            result_with_recency[0]["_rrf_score"]
            == result_without_recency[0]["_rrf_score"]
        )

    def test_rrf_fuse_recency_weight_one_pure_recency(self):
        """weight=1.0 ignores RRF scores, uses pure recency."""
        result_lists = [
            [{"id": "old", "text": "old doc"}, {"id": "new", "text": "new doc"}],
        ]
        recency_scores = {"old": 0.1, "new": 0.9}

        result = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=1.0
        )

        ids = [r["id"] for r in result]
        # Sorted purely by recency score
        assert ids == ["new", "old"]
        # Score should be exactly the recency score
        assert result[0]["_rrf_score"] == 0.9
        assert result[1]["_rrf_score"] == 0.1

    def test_rrf_fuse_missing_recency_gets_neutral(self):
        """Records not in recency_scores get neutral 0.5."""
        result_lists = [
            [{"id": "a", "text": "doc a"}, {"id": "b", "text": "doc b"}],
        ]
        recency_scores = {"a": 1.0}  # only 'a' has recency

        result = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=1.0
        )

        # 'a' gets its recency score, 'b' gets neutral 0.5
        scores = {r["id"]: r["_rrf_score"] for r in result}
        assert scores["a"] == 1.0
        assert scores["b"] == 0.5

    def test_rrf_fuse_empty_recency_scores(self):
        """Empty dict behaves like no recency."""
        result_lists = [
            [{"id": "a", "text": "doc a"}],
        ]
        recency_scores = {}

        result_with_empty = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=0.5
        )
        result_without = rrf_fuse(result_lists, k=60)

        # Should be identical - empty dict like no recency
        assert result_with_empty[0]["_rrf_score"] == result_without[0]["_rrf_score"]

    def test_rrf_fuse_recency_none_disabled(self):
        """recency_scores=None behaves like no recency."""
        result_lists = [
            [{"id": "a", "text": "doc a"}, {"id": "b", "text": "doc b"}],
        ]

        result_with_none = rrf_fuse(
            result_lists, k=60, recency_scores=None, recency_weight=0.5
        )
        result_without = rrf_fuse(result_lists, k=60)

        ids_with = [r["id"] for r in result_with_none]
        ids_without = [r["id"] for r in result_without]
        assert ids_with == ids_without
        assert result_with_none[0]["_rrf_score"] == result_without[0]["_rrf_score"]

    def test_rrf_fuse_recency_normalizes_non_max_score(self):
        """The non-max document's blended score uses its RRF *normalized* against
        the strongest hit, not its raw RRF.

        The other recency tests only pin the max-score doc (which normalizes to
        1.0 trivially) or assert ordering. This pins the rank-1 doc's exact score,
        which is what distinguishes the normalized blend from the old raw-RRF
        blend: under the old formula 'b' would score ``(1/62)*(1-w) + rec*w``;
        under normalization it scores ``((1/62)/(1/61))*(1-w) + rec*w``.
        """
        result_lists = [
            # a: rank 0 -> 1/61 (the max); b: rank 1 -> 1/62
            [{"id": "a", "text": "doc a"}, {"id": "b", "text": "doc b"}],
        ]
        recency_scores = {"a": 0.2, "b": 0.2}  # equal recency → ordering is by RRF
        w = 0.5

        result = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=w
        )
        scores = {r["id"]: r["_rrf_score"] for r in result}

        max_rrf = 1 / 61
        b_norm = (1 / 62) / max_rrf
        assert scores["a"] == pytest.approx(1.0 * (1 - w) + 0.2 * w)
        assert scores["b"] == pytest.approx(b_norm * (1 - w) + 0.2 * w)
        # 'a' still ranks first: equal recency, higher normalized RRF.
        assert [r["id"] for r in result] == ["a", "b"]

    def test_rrf_fuse_recency_blend_correctness(self):
        """Verify exact formula with known values.

        RRF scores are normalized against the strongest hit before blending, so a
        single result (which is the max) contributes a normalized relevance of 1.0.
        The blended score is therefore ``1.0 * (1 - w) + recency * w``. (Previously
        the raw RRF score ~1/61 was blended directly, which let recency dominate.)
        """
        result_lists = [
            [{"id": "doc", "text": "test doc"}],  # rank 0; sole hit -> normalized 1.0
        ]
        recency_score = 0.8
        recency_weight = 0.25

        expected_score = 1.0 * (1 - recency_weight) + recency_score * recency_weight

        result = rrf_fuse(
            result_lists,
            k=60,
            recency_scores={"doc": recency_score},
            recency_weight=recency_weight,
        )

        assert len(result) == 1
        assert result[0]["_rrf_score"] == pytest.approx(expected_score)

    def test_rrf_fuse_dedup_with_recency(self):
        """Same record across lists gets blended recency."""
        # 'a' appears in both lists at different ranks
        result_lists = [
            [{"id": "a", "text": "doc a"}],  # rank 0 -> 1/61
            [
                {"id": "a", "text": "doc a"},
                {"id": "b", "text": "doc b"},
            ],  # rank 0 -> 1/61, rank 1 -> 1/62
        ]

        # With recency, 'a' should be ranked by combined RRF + recency
        recency_scores = {"a": 0.9, "b": 0.5}
        result = rrf_fuse(
            result_lists, k=60, recency_scores=recency_scores, recency_weight=0.5
        )

        ids = [r["id"] for r in result]
        assert ids[0] == "a"  # 'a' has higher combined score
        assert ids[1] == "b"

        # Verify 'a' got blended score. 'a' is the strongest RRF hit (it appears in
        # both lists), so its RRF normalizes to 1.0 before blending:
        # 1.0 * (1 - 0.5) + 0.9 * 0.5.
        a_record = next(r for r in result if r["id"] == "a")
        expected_blended = 1.0 * 0.5 + 0.9 * 0.5
        assert a_record["_rrf_score"] == pytest.approx(expected_blended)
