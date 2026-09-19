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
