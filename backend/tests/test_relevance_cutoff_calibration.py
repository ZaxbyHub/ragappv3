"""Issue #36 — calibrated backend distance-cutoff checks (frozen Phase 2.5 spec).

Backed by backend/tests/eval/calibration_2026_09/ (dataset, distributions,
analysis): gold recall at the shipped default threshold 0.5 is 0.236 on the
non-reranked path; the calibrated default 0.75 yields 0.909 against a 0.927
no-filter ceiling with a 16.7% no-answer leak.

These tests are RED on master (default max_distance_threshold == 0.5) and
must be GREEN after the fix (default 0.75). Environment mirrors
backend/tests/conftest.py: ADMIN_SECRET_TOKEN / USERS_ENABLED are set there
(and by the repro wrapper) before app.config is imported.
"""

import asyncio

from app.config import Settings
from app.services.document_retrieval import DocumentRetrievalService


def _gold_record():
    """One retrieved chunk at a calibrated-relevant cosine distance (0.61).

    0.61 sits between the Highly Relevant (0.56) and Relevant (0.67) distance
    bands, i.e. squarely inside gold territory (gold q25..q75 = 0.559..0.671),
    and above the old uncalibrated 0.5 cutoff that was dropping most gold.
    """
    return [
        {
            "id": "10_93514a1c_768_0",
            "file_id": "10",
            "text": "The System User Manual v3.3 covers software release 1.1.65.6.",
            "_distance": 0.61,
            "metadata": {},
        }
    ]


def test_settings_default_max_distance_threshold_is_calibrated():
    assert Settings().max_distance_threshold == 0.75


def test_service_default_threshold_follows_calibrated_settings():
    # The real filter path consumes the settings default when no explicit
    # threshold is passed.
    service = DocumentRetrievalService(retrieval_window=0)
    assert service.max_distance_threshold == 0.75


def test_distance_filter_default_admits_calibrated_relevant_chunk():
    # reranked=False drives the real distance-filter branch in
    # DocumentRetrievalService.filter_relevant: with the calibrated default
    # (0.75) a gold-range chunk at _distance 0.61 must survive.
    service = DocumentRetrievalService(retrieval_window=0)
    kept = asyncio.run(service.filter_relevant(_gold_record(), reranked=False))
    assert len(kept) == 1
    assert kept[0].file_id == "10"
    assert service.no_match is False


def test_distance_filter_explicit_05_still_drops():
    # Contrast arm: the old uncalibrated cutoff 0.5 (now opt-in only) drops
    # the same chunk and signals no_match.
    service = DocumentRetrievalService(
        max_distance_threshold=0.5, retrieval_window=0
    )
    kept = asyncio.run(service.filter_relevant(_gold_record(), reranked=False))
    assert kept == []
    assert service.no_match is True


def _fallback_score_record(score):
    """A record with NO `_distance`: the similarity-score fallback branch
    (bare higher-is-better `score`, compared against FALLBACK_SCORE_FLOOR).
    This is the branch the CI round-4 regression hit — raising the shared
    constant to 0.75 silently dropped a 0.7-score fixture record in
    test_rag_engine_vault_isolation."""
    return [
        {
            "id": "f3",
            "file_id": "f3",
            "text": "shared topic",
            "score": score,
            "metadata": {},
        }
    ]


def test_fallback_score_floor_decoupled_from_distance_threshold():
    # The calibrated distance max (0.75) must NOT tighten the similarity-score
    # fallback: a 0.7-score record with no _distance survives at the shipped
    # default because FALLBACK_SCORE_FLOOR stays at the legacy 0.5.
    service = DocumentRetrievalService(retrieval_window=0)
    assert service.max_distance_threshold == 0.75
    kept = asyncio.run(
        service.filter_relevant(_fallback_score_record(0.7), reranked=False)
    )
    assert len(kept) == 1
    assert kept[0].file_id == "f3"


def test_fallback_score_floor_exact_boundary_is_kept():
    # Exact-boundary pin (reviewer round-4 L5-2): the comparison is
    # score < FALLBACK_SCORE_FLOOR, so a score of exactly 0.5 is KEPT.
    # Catches a '<' -> '<=' off-by-one that the 0.4/0.7 pair cannot see.
    service = DocumentRetrievalService(retrieval_window=0)
    kept = asyncio.run(
        service.filter_relevant(_fallback_score_record(0.5), reranked=False)
    )
    assert len(kept) == 1
    assert kept[0].file_id == "f3"


def test_fallback_score_floor_still_drops_below_legacy_floor():
    # Contrast arm: the decoupled floor keeps its legacy drop behavior —
    # a 0.4-score fallback record is below 0.5 and is dropped with no_match.
    service = DocumentRetrievalService(retrieval_window=0)
    kept = asyncio.run(
        service.filter_relevant(_fallback_score_record(0.4), reranked=False)
    )
    assert kept == []
    assert service.no_match is True
