"""Issue #1070 AC8: the real LanceDB FTS key ``_score`` drives the
no-``_distance`` relevance decision and the reported ``RAGSource.score``.

At base d00bc160 ``filter_relevant``'s no-``_distance`` branch reads the
legacy ``score`` key (no production producer writes it) and pins ``1.0``
when it is absent, so an FTS-only hit's real BM25 ``_score`` (the key
LanceDB 0.40.0 FTS attaches and the only key on FTS-only rows after
``rrf_fuse``) never reaches either the higher-is-better floor decision or
``RAGSource.score`` — a pass-everything default that ignores real
relevance (executed probe in the trace's 02-reproduction.md: below-floor
``_score`` records survive with score=1.0 in BOTH threshold modes).

These checks drive the REAL ``DocumentRetrievalService.filter_relevant``
(the same object ``RAGEngine._filter_relevant`` delegates to) with records
keyed the way real FTS-only rows arrive — ``_score`` present, ``_distance``
absent — in both threshold modes:

- legacy mode (``max_distance_threshold=None``, floor resolved from
  ``relevance_threshold``): ``_score`` 0.5 and 0.6 against a 0.9 floor are
  DROPPED and the service flags ``no_match``;
- default mode (``max_distance_threshold=0.75``, floor
  ``FALLBACK_SCORE_FLOOR`` = 0.5): ``_score`` 0.2 is DROPPED and the
  service flags ``no_match``;
- a passing record's ``RAGSource.score`` equals its REAL ``_score`` value
  (0.8 in default mode, 0.95 in legacy mode), never the fabricated 1.0;
- precedence, both keys present: a record carrying BOTH ``_score`` and a
  legacy ``score`` that imply different outcomes resolves by the REAL
  ``_score`` — ``_score=0.2`` (below the 0.5 floor) with ``score=0.9``
  must DROP (RED at base: the legacy read keeps it);
- precedence, ``_score: None``: a present-but-None ``_score`` must NOT
  shadow a usable legacy ``score`` — ``score=0.2`` below the floor still
  DROPs (green at base, which already reads the legacy key; goes RED only
  if the widened read resolves ``_score`` with a dict-get default or
  otherwise lets None beat the legacy fallback into the fabricated 1.0).

expand_window sibling limit: the adjacent-chunk reader in the same file
resolves its score with the identical legacy read (``_score`` ignored,
1.0 default), but its production input rows (``get_chunks_by_uid`` fetches)
carry NEITHER key, so the 1.0 default there is unconditionally dead in
production. The minimal sibling leg below pins only the reader contract a
stub can make bite — an adjacent record carrying ``_score`` yields that
value as the expanded source's score — and asserts nothing about
production adjacency scoring, which no fixture can exercise honestly until
a producer writes the key.

Contract asserted at BOTH ends: RED at base (below-floor records survive
with fabricated 1.0 scores — the executed base defect), GREEN after the
fix (the real ``_score`` drives the floor decision and the wire score).
"""

import pytest

from app.services.document_retrieval import (
    DocumentRetrievalService,
    RAGSource,
)


def _service() -> DocumentRetrievalService:
    svc = DocumentRetrievalService(
        vector_store=None,
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=0,
    )
    return svc


async def test_legacy_mode_below_floor_fts_scores_are_dropped() -> None:
    """RED at base, GREEN after the fix. Legacy mode (explicit
    ``max_distance_threshold=None``) resolves the floor from
    ``relevance_threshold``; ``_score`` values 0.5 and 0.6 sit below the 0.9
    floor and must be dropped with ``no_match`` flagged."""
    svc = _service()
    svc.max_distance_threshold = None
    svc.relevance_threshold = 0.9
    results = [
        {"_score": 0.5, "text": "doc1", "file_id": "f1"},
        {"_score": 0.6, "text": "doc2", "file_id": "f2"},
    ]
    kept = await svc.filter_relevant(results)
    assert kept == [], (
        "legacy-mode FTS records below the relevance floor survived: the "
        f"no-_distance branch ignored the real _score values (kept {len(kept)} "
        "of 2 records with real _scores 0.5/0.6 against a 0.9 floor)"
    )
    assert svc.no_match is True, (
        "dropping every below-floor FTS record must flag no_match"
    )


async def test_default_mode_below_floor_fts_score_is_dropped() -> None:
    """RED at base, GREEN after the fix. Default mode floors no-``_distance``
    records at ``FALLBACK_SCORE_FLOOR`` (0.5); an ``_score`` of 0.2 sits far
    below and must be dropped with ``no_match`` flagged."""
    svc = _service()
    assert svc.max_distance_threshold == 0.75
    results = [
        {"_score": 0.2, "text": "doc1", "file_id": "f1"},
    ]
    kept = await svc.filter_relevant(results)
    assert kept == [], (
        "default-mode FTS record below the 0.5 fallback floor survived: the "
        f"no-_distance branch ignored the real _score value (kept {len(kept)} "
        "record with real _score 0.2 against the 0.5 floor)"
    )
    assert svc.no_match is True, (
        "dropping the below-floor FTS record must flag no_match"
    )


async def test_both_keys_present_real_score_wins_over_legacy() -> None:
    """RED at base, GREEN after the fix. A record carrying BOTH the real FTS
    ``_score`` and a legacy ``score`` whose two values imply different
    outcomes must resolve by the ``_score`` value: ``_score=0.2`` sits below
    the 0.5 floor (DROP) while the legacy ``score=0.9`` would clear it
    (KEEP). At base the legacy read wins and the record survives."""
    svc = _service()
    assert svc.max_distance_threshold == 0.75
    results = [
        {"_score": 0.2, "score": 0.9, "text": "doc1", "file_id": "f1"},
    ]
    kept = await svc.filter_relevant(results)
    assert kept == [], (
        "precedence loss: a record carrying both keys resolved by the legacy "
        f"score (kept {len(kept)} record with real _score 0.2 below the 0.5 "
        "floor because the legacy score 0.9 won)"
    )
    assert svc.no_match is True, (
        "dropping the record whose real _score sits below the floor must "
        "flag no_match"
    )


async def test_none_score_falls_back_to_usable_legacy_score() -> None:
    """Green at base and after the CORRECT widened read; RED only under a
    naive widening. A present-but-None ``_score`` must not shadow a usable
    legacy ``score``: the legacy value (0.2, below the 0.5 floor) is used
    and the record DROPs. Base already reads the legacy key here, so this
    leg pins the widened read's fallback ORDER — a ``dict.get``-default or
    truthiness chain that resolves ``_score`` first and lets None beat the
    legacy value into the fabricated 1.0 would keep the record and fail."""
    svc = _service()
    assert svc.max_distance_threshold == 0.75
    results = [
        {"_score": None, "score": 0.2, "text": "doc1", "file_id": "f1"},
    ]
    kept = await svc.filter_relevant(results)
    assert kept == [], (
        "precedence loss: a present-but-None _score shadowed the usable "
        f"legacy score (kept {len(kept)} record whose legacy score 0.2 sits "
        "below the 0.5 floor)"
    )
    assert svc.no_match is True, (
        "dropping the below-floor record must flag no_match"
    )


async def test_passing_fts_score_is_reported_verbatim() -> None:
    """RED at base, GREEN after the fix. A record whose ``_score`` clears the
    floor is kept and its ``RAGSource.score`` equals the REAL ``_score``
    value — never the fabricated 1.0 default."""
    svc = _service()
    kept_default = await svc.filter_relevant(
        [{"_score": 0.8, "text": "doc1", "file_id": "f1"}]
    )
    assert len(kept_default) == 1, "an above-floor FTS record must be kept"
    assert kept_default[0].score == pytest.approx(0.8), (
        "RAGSource.score must carry the real _score value (0.8), not the "
        f"fabricated 1.0 default (got {kept_default[0].score!r})"
    )

    svc.max_distance_threshold = None
    svc.relevance_threshold = 0.9
    kept_legacy = await svc.filter_relevant(
        [{"_score": 0.95, "text": "doc2", "file_id": "f2"}]
    )
    assert len(kept_legacy) == 1, "an above-floor legacy-mode record must be kept"
    assert kept_legacy[0].score == pytest.approx(0.95), (
        "legacy-mode RAGSource.score must carry the real _score value "
        f"(0.95), not the fabricated 1.0 default (got {kept_legacy[0].score!r})"
    )


class _StubVectorStore:
    """Minimal ``get_chunks_by_uid`` stand-in returning ONE adjacent chunk
    keyed the way an FTS-scored row would be (``_score``, no ``_distance``,
    no legacy ``score``)."""

    def __init__(self, chunks) -> None:  # noqa: ANN001
        self._chunks = chunks

    async def get_chunks_by_uid(self, uids):  # noqa: ANN001
        return self._chunks


async def test_expand_window_adjacent_fts_score_is_reported_verbatim() -> None:
    """RED at base, GREEN after the fix — minimal sibling-reader leg.

    LIMITS (documented per the census): production ``expand_window`` input
    rows come from a plain ``get_chunks_by_uid`` fetch and carry NEITHER
    ``_score`` NOR ``_distance``/``score``, so the legacy read is
    unconditionally dead there (every adjacent chunk scores the 1.0 default
    in production). This leg therefore pins only the reader contract a stub
    can make bite: an adjacent record that DOES carry ``_score`` resolves its
    expanded ``RAGSource.score`` from that value instead of the fabricated
    1.0 — the same widened-read fix ``filter_relevant`` needs."""
    svc = DocumentRetrievalService(
        vector_store=_StubVectorStore(
            [
                {
                    "id": "f1_1",
                    "file_id": "f1",
                    "text": "adjacent passage",
                    "_score": 0.42,
                }
            ]
        ),
        max_distance_threshold=0.75,
        retrieval_top_k=5,
        retrieval_window=1,
    )
    center = RAGSource(
        text="center passage",
        file_id="f1",
        score=0.9,
        metadata={"chunk_index": 0},
    )
    expanded = await svc.expand_window([center])
    adjacent = [s for s in expanded if s.text == "adjacent passage"]
    assert len(adjacent) == 1, (
        f"the stubbed adjacent chunk must be expanded in (got {len(adjacent)})"
    )
    assert adjacent[0].score == pytest.approx(0.42), (
        "the expanded adjacent source must carry the adjacent record's real "
        f"_score value (0.42), not the fabricated 1.0 default (got "
        f"{adjacent[0].score!r})"
    )
