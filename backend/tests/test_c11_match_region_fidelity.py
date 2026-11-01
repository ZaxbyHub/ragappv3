"""Issue #716 acceptance check C11-1 — match-region fidelity (format_chunk).

The distiller/wiki suppression pass (issue #510 RAG-004) removes a covered
SENTENCE from ``chunk.text`` and from the parent window, but the chunk's
``metadata["raw_text"]`` still contains the suppressed sentence — raw_text is
the pre-suppression original. ``format_chunk`` builds the ``[[MATCH:]]``
region from ``metadata["raw_text"]`` FIRST, so when that raw text is no
longer a substring of the (suppressed) parent window the fallback branch
re-appends the FULL raw text as an annotation — silently re-injecting the
suppressed sentence into the rendered prompt.

The rendered output must contain the suppressed sentinel 0 times.

Fixture pattern copied from tests/test_parent_retrieval.py
(TestPromptBuilderParentWindow::_make_source_with_parent + the patched
app.services.prompt_builder.settings context).
"""

from unittest.mock import patch

from app.services.document_retrieval import RAGSource

# The sentence the suppression pass removed. Distinctive so its presence in
# the rendered output is unambiguous.
SENTINEL = "SENTINELWIKISUPPRESSEDSENTENCE"


def _make_source_with_parent(
    chunk_text: str, parent_text: str, raw_text: str = None
) -> RAGSource:
    src = RAGSource(
        text=chunk_text,
        file_id="42",
        score=0.85,
        metadata={
            "source_file": "report.pdf",
            "raw_text": raw_text or chunk_text,
            "parent_window_text": parent_text,
        },
    )
    src.parent_window_text = parent_text
    return src


def test_suppressed_sentence_not_reinjected():
    """[issue #716 / C11-1] A sentence suppressed from chunk.text and from
    the parent window must not come back through the [[MATCH:]] fallback,
    which today renders metadata raw_text verbatim."""
    from app.services.prompt_builder import PromptBuilderService

    chunk_text = "Core operational facts remain on record."
    parent_text = (
        "Lead-in context for the passage. Core operational facts remain on "
        "record. Trailing context closes the window."
    )
    raw_text = f"Core operational facts remain on record. {SENTINEL}"

    src = _make_source_with_parent(chunk_text, parent_text, raw_text=raw_text)

    # Fixture sanity: the suppression state is exactly as the distiller
    # leaves it — the sentinel survives ONLY in metadata raw_text.
    assert SENTINEL in src.metadata["raw_text"]
    assert SENTINEL not in src.text
    assert SENTINEL not in src.parent_window_text

    with patch("app.services.prompt_builder.settings") as mock_settings:
        mock_settings.parent_retrieval_enabled = True
        mock_settings.anchor_best_chunk = False
        mock_settings.primary_evidence_count = 0
        mock_settings.context_max_tokens = 6000

        builder = PromptBuilderService()
        formatted = builder.format_chunk(src, source_index=1)

    assert formatted.count(SENTINEL) == 0
