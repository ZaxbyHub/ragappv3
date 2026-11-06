"""Issue #1070: the per-source "fts" score kind reaches the wire.

The widened ``filter_relevant`` read resolves FTS-only rows (LanceDB
``_score``, no ``_distance``) to their real higher-is-better BM25 value.
That value must not be misread through the lower-is-better distance
polarity, so the ``RAGSource`` carries a per-source score kind of "fts"
exactly when the value came from ``_score`` on the no-``_distance`` path,
dense ``_distance`` rows carry NO per-source kind (the global pipeline
score_type "distance"/"rerank" already types them), and a valid
``_rerank_score`` override reverts to the global typing.

These checks drive the REAL production seam end to end:
``filter_relevant`` -> context distillation (``ContextDistiller.distill``,
default-on per ``settings.context_distillation_enabled``, reconstructs every
``RAGSource`` field by field) -> ``to_source_metadata`` serialization — and
assert the per-source kind on the wire dict for FTS rows, its absence for
dense rows, and the rerank override clearing it.
"""

from typing import List
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.context_distiller import ContextDistiller
from app.services.document_retrieval import DocumentRetrievalService


def _service() -> DocumentRetrievalService:
    return DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )


class _OrthogonalEmbeddingService:
    """Minimal embedding stand-in: one orthogonal unit vector per sentence.

    Orthogonal embeddings keep cosine similarity at 0.0, below the dedup
    threshold, so every sentence survives distillation and the field-by-field
    RAGSource reconstruction is exercised on the full input set.
    """

    def __init__(self) -> None:
        self.embed_batch = AsyncMock(side_effect=self._embed_batch)

    @staticmethod
    async def _embed_batch(sentences: List[str]) -> List[List[float]]:
        return [
            [1.0 if i == j else 0.0 for j in range(len(sentences))]
            for i in range(len(sentences))
        ]


async def test_fts_only_sources_carry_the_kind_through_distillation_to_the_wire() -> None:
    """FTS-only hits serialize with per-source score_type "fts" and their real
    BM25 value, through default-on distillation."""
    assert settings.context_distillation_enabled is True, (
        "distillation must be default-on for this wire test to drive the "
        "production seam (config.py context_distillation_enabled)"
    )
    svc = _service()
    results = [
        {"id": "f1_0", "file_id": "f1", "text": "FTS passage one.", "_score": 0.8, "metadata": {}},
        {"id": "f2_0", "file_id": "f2", "text": "FTS passage two.", "_score": 0.65, "metadata": {}},
    ]
    kept = await svc.filter_relevant(results)
    assert len(kept) == 2, "both above-floor FTS records must survive"
    for source in kept:
        assert source.score_type == "fts", (
            f"an FTS-sourced RAGSource must carry the per-source 'fts' kind "
            f"(file_id={source.file_id!r}, got {source.score_type!r})"
        )

    distiller = ContextDistiller(_OrthogonalEmbeddingService())
    distilled = await distiller.distill("fts query", kept, "CONFIDENT")
    assert len(distilled.sources) == 2, "orthogonal sentences are never deduped"
    wire = [
        svc.to_source_metadata(chunk, source_index=idx + 1)
        for idx, chunk in enumerate(distilled.sources)
    ]
    for idx, (source, meta) in enumerate(zip(distilled.sources, wire)):
        assert source.score_type == "fts", (
            f"distillation's field-by-field reconstruction dropped the "
            f"per-source kind (source {idx}: {source.score_type!r})"
        )
        assert meta["score_type"] == "fts", (
            f"the wire dict must serialize the per-source 'fts' kind "
            f"(source {idx}: {meta.get('score_type')!r})"
        )
    assert wire[0]["score"] == pytest.approx(0.8), (
        "the FTS source's wire score must be its real _score value"
    )
    assert wire[1]["score"] == pytest.approx(0.65)


async def test_mixed_dense_and_fts_sources_type_only_the_fts_row() -> None:
    """In a mixed set the dense row keeps the global typing (no per-source
    field on the wire) while the FTS row carries "fts" — both through
    default-on distillation."""
    svc = _service()
    results = [
        {"id": "fd_0", "file_id": "fd", "text": "Dense passage.", "_distance": 0.3, "metadata": {}},
        {"id": "ff_0", "file_id": "ff", "text": "FTS passage.", "_score": 0.7, "metadata": {}},
    ]
    kept = await svc.filter_relevant(results)
    assert len(kept) == 2
    by_file = {s.file_id: s for s in kept}
    assert by_file["fd"].score_type is None, (
        "a dense _distance row must NOT carry a per-source score kind — the "
        "global pipeline score_type types it"
    )
    assert by_file["ff"].score_type == "fts"

    distiller = ContextDistiller(_OrthogonalEmbeddingService())
    distilled = await distiller.distill("mixed query", kept, "CONFIDENT")
    assert len(distilled.sources) == 2
    wire = {
        s.file_id: svc.to_source_metadata(s, source_index=idx + 1)
        for idx, s in enumerate(distilled.sources)
    }
    assert "score_type" not in wire["fd"], (
        "the dense row's wire dict must omit the per-source kind so the "
        "global done-payload score_type applies (existing client contract)"
    )
    assert wire["ff"]["score_type"] == "fts"
    assert wire["fd"]["score"] == pytest.approx(0.3)
    assert wire["ff"]["score"] == pytest.approx(0.7)


async def test_valid_rerank_score_override_reverts_to_global_typing() -> None:
    """A valid ``_rerank_score`` on a reranked FTS row replaces the reported
    value, so the per-source "fts" kind must NOT be set — the global "rerank"
    typing applies to that score."""
    svc = _service()
    results = [
        {
            "id": "fr_0",
            "file_id": "fr",
            "text": "Reranked FTS passage.",
            "_score": 0.7,
            "_rerank_score": 0.9,
            "metadata": {},
        },
    ]
    kept = await svc.filter_relevant(results, reranked=True)
    assert len(kept) == 1
    assert kept[0].score == pytest.approx(0.9), (
        "the reranker's sigmoid score must win over the FTS _score value"
    )
    assert kept[0].score_type is None, (
        "a rerank-overridden value must not claim the per-source 'fts' kind — "
        f"the global 'rerank' typing types it (got {kept[0].score_type!r})"
    )
    wire = svc.to_source_metadata(kept[0], source_index=1)
    assert "score_type" not in wire


async def test_present_but_none_distance_resolves_by_score_polarity() -> None:
    """A record with ``_distance: None`` AND a real ``_score`` must resolve
    through the higher-is-better score tier, never gated by distance
    polarity (both swarm-review shards on PR #1077 flagged the latent
    inversion: no current producer emits the shape, but the polarity would
    silently invert if one ever did)."""
    svc = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )
    # _score 0.1 is BELOW the 0.5 fallback floor: score polarity DROPS it
    # (distance polarity would have KEPT it — 0.1 < 0.75 is not >).
    dropped = await svc.filter_relevant(
        [{"_distance": None, "_score": 0.1, "text": "weak", "file_id": "f1"}]
    )
    assert dropped == [], (
        "a present-but-None _distance must gate the FTS _score by the SCORE "
        f"floor, not by distance polarity (kept: {dropped!r})"
    )
    assert svc.no_match is True

    # _score 0.8 is ABOVE the floor: kept, and its per-source kind rides.
    kept = await svc.filter_relevant(
        [{"_distance": None, "_score": 0.8, "text": "strong", "file_id": "f2"}]
    )
    assert len(kept) == 1, f"an above-floor _score must be kept (got {kept!r})"
    assert kept[0].score_type == "fts", (
        f"the kept source must carry the per-source fts kind (got {kept[0].score_type!r})"
    )
    assert kept[0].score == pytest.approx(0.8)


async def test_score_gated_no_match_names_the_floor(caplog) -> None:
    """PRR-015 (PR #1077 review): when every drop came through the score
    floor, the no_match log must name THAT gate — not
    ``max_distance_threshold`` (wrong constant, wrong polarity)."""
    import logging as _logging

    svc = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )
    with caplog.at_level(_logging.INFO, logger="app.services.document_retrieval"):
        out = await svc.filter_relevant(
            [{"_score": 0.1, "text": "weak", "file_id": "f1"}]
        )
    assert out == [] and svc.no_match is True
    no_match_recs = [r for r in caplog.records if "no_match flag" in r.getMessage()]
    assert no_match_recs, f"the no_match line must fire; records: {[r.getMessage() for r in caplog.records]}"
    msg = no_match_recs[-1].getMessage()
    assert "score floor" in msg and "floor=0.500" in msg, (
        f"the score-gated no_match line must name the score floor; got: {msg!r}"
    )
    assert "exceeded max_distance_threshold" not in msg, (
        f"the distance-gate wording must not fire for score-only drops; got: {msg!r}"
    )


async def test_summary_reports_kinds_separately(caplog) -> None:
    """PRR-015 r2: the INFO summary must never fold score-tier values into a
    distance-kind statistic — mixed pools report both kinds separately, and
    score-only pools print no distance-kind numbers at all."""
    import logging as _logging

    svc = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )
    with caplog.at_level(_logging.INFO, logger="app.services.document_retrieval"):
        await svc.filter_relevant(
            [
                {"_distance": 0.2, "text": "dense", "file_id": "f1"},
                {"_score": 1.87, "text": "fts", "file_id": "f2"},
            ]
        )
    mixed = [r for r in caplog.records if "distance min=" in r.getMessage()]
    assert mixed, f"mixed-pool summary line missing; records: {[r.getMessage() for r in caplog.records]}"
    msg = mixed[-1].getMessage()
    assert "(n=1, fts=1)" in msg, f"score bucket must be n=1 with fts=1; got: {msg!r}"
    # The distance bucket must be distance-ONLY: its max is the dense value,
    # not the larger FTS magnitude (the r2 fold-the-scales defect).
    assert "distance min=0.200, max=0.200, mean=0.200 (n=1)" in msg, f"got: {msg!r}"

    caplog.clear()
    svc2 = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )
    with caplog.at_level(_logging.INFO, logger="app.services.document_retrieval"):
        await svc2.filter_relevant(
            [{"_score": 1.87, "text": "fts-only", "file_id": "f3"}]
        )
    score_only = [r for r in caplog.records if "Vector search" in r.getMessage()]
    assert score_only, "score-only summary line missing"
    msg2 = score_only[-1].getMessage()
    assert "distance min" not in msg2, (
        f"score-only pool must not print distance-kind stats; got: {msg2!r}"
    )
    assert "score min=" in msg2 and "(n=1" in msg2, f"got: {msg2!r}"
