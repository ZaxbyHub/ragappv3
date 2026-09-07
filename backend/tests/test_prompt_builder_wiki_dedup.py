"""Wiki-overlap suppression in PromptBuilderService (issue #510 RAG-004).

A document chunk that shares a sentence with a compiled wiki claim must keep
its UNIQUE sentences (with the chunk's positional [S#] label); only a chunk
whose text AND parent window are both fully covered is dropped entirely.
"""

import pytest

from app.config import settings
from app.services.document_retrieval import RAGSource
from app.services.prompt_builder import PromptBuilderService
from app.services.wiki_retrieval import WikiEvidence

# A wiki claim long enough (> 40 chars) to trigger sentence-level suppression.
_SHARED_CLAIM = "The deployment window for production systems closes at midnight sharp"
_SHARED_SENTENCE = (
    f"{_SHARED_CLAIM}, so schedule your releases well before then."
)
_UNIQUE_SENTENCE = "The admin API key rotates every 30 days by policy."


def _wiki_evidence(claim_text: str) -> WikiEvidence:
    return WikiEvidence(
        label_placeholder="W1",
        page_id=1,
        claim_id=10,
        title="Deployment Runbook",
        slug="deployment-runbook",
        page_type="overview",
        claim_text=claim_text,
        excerpt=claim_text,
        confidence=0.9,
        page_status="verified",
        claim_status="active",
        score=0.85,
        score_type="claim_fts",
        freshness=None,
        source_count=2,
        provenance_summary="2 docs",
    )


def _chunk(text: str, parent: str = None) -> RAGSource:
    return RAGSource(
        text=text,
        file_id="file1",
        score=0.9,
        metadata={"source_file": "runbook.pdf"},
        parent_window_text=parent,
    )


def _build(chunks, wiki=None):
    builder = PromptBuilderService(system_prompt="You are a test assistant.")
    return builder.build_messages(
        "When do deployments close?",
        [],
        chunks,
        [],
        wiki_evidence=wiki,
    )


def _user_content(messages):
    return messages[-1]["content"]


@pytest.fixture
def no_anchor(monkeypatch):
    """Disable the best-chunk anchor so suppressed text cannot re-enter via it."""
    monkeypatch.setattr(settings, "anchor_best_chunk", False)
    return monkeypatch


class TestSentenceLevelSuppression:
    def test_unique_fact_and_label_survive_anchor_on(self):
        """With anchor_best_chunk=True (default), the unique sentence and the
        chunk's [S1] label must both be present in the prompt."""
        monkeypatched = pytest.MonkeyPatch()
        monkeypatched.setattr(settings, "anchor_best_chunk", True)
        try:
            chunk = _chunk(f"{_SHARED_SENTENCE} {_UNIQUE_SENTENCE}")
            messages = _build([chunk], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        finally:
            monkeypatched.undo()
        content = _user_content(messages)
        assert _UNIQUE_SENTENCE in content, (
            "The unique answer-fact sentence must survive wiki-overlap suppression"
        )
        assert "[S1]" in content, "The chunk's positional source label must survive"

    def test_unique_fact_and_label_survive_anchor_off(self, no_anchor):
        chunk = _chunk(f"{_SHARED_SENTENCE} {_UNIQUE_SENTENCE}")
        messages = _build([chunk], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        content = _user_content(messages)
        assert _UNIQUE_SENTENCE in content
        assert "[S1]" in content
        # The covered sentence itself is suppressed from the evidence section.
        assert "schedule your releases well before then" not in content

    def test_fully_covered_chunk_dropped(self, no_anchor):
        """A chunk whose every sentence is covered by wiki claims is dropped."""
        chunk = _chunk(_SHARED_SENTENCE)
        messages = _build([chunk], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        content = _user_content(messages)
        assert "[S1]" not in content
        assert "schedule your releases well before then" not in content
        assert "No relevant documents found for this query." in content

    def test_text_covered_but_parent_unique_keeps_chunk(self, no_anchor, monkeypatch):
        """A chunk is dropped ONLY when text AND parent window are covered."""
        monkeypatch.setattr(settings, "parent_retrieval_enabled", True)
        parent = f"{_SHARED_SENTENCE} Escalation contact is on-call-eng@corp."
        chunk = _chunk(_SHARED_SENTENCE, parent=parent)
        messages = _build([chunk], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        content = _user_content(messages)
        assert "[S1]" in content, "Unique parent-window content must keep the chunk"
        assert "Escalation contact is on-call-eng@corp." in content

    def test_text_and_parent_both_covered_drops_chunk(self, no_anchor, monkeypatch):
        monkeypatch.setattr(settings, "parent_retrieval_enabled", True)
        parent = _SHARED_SENTENCE
        chunk = _chunk(_SHARED_SENTENCE, parent=parent)
        messages = _build([chunk], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        content = _user_content(messages)
        assert "[S1]" not in content
        assert "No relevant documents found for this query." in content

    def test_labels_stay_positional_over_full_chunk_list(self, no_anchor):
        """A dropped fully-covered chunk leaves a label GAP, not a renumber."""
        covered = _chunk(_SHARED_SENTENCE)
        unique = _chunk(_UNIQUE_SENTENCE + " Backup window opens at 3am weekly.")
        messages = _build([covered, unique], wiki=[_wiki_evidence(_SHARED_CLAIM)])
        content = _user_content(messages)
        assert "[S2]" in content, "The surviving chunk keeps its positional label"
        assert "[S1]" not in content, "The dropped chunk's label must not appear"

    def test_no_wiki_case_unchanged(self, no_anchor):
        """Without wiki evidence the chunk renders in full."""
        chunk = _chunk(f"{_SHARED_SENTENCE} {_UNIQUE_SENTENCE}")
        messages = _build([chunk], wiki=None)
        content = _user_content(messages)
        assert _SHARED_SENTENCE in content
        assert _UNIQUE_SENTENCE in content
        assert "[S1]" in content
