"""Issue #698 acceptance checks (Workstream B PR 9) — settings→ingest contract.

AC1 (test_chunk_size_setting_honored_or_disclosed_under_multiscale): with
multi-scale indexing enabled and ``chunk_size_chars=300``, either the
configured chunk size must reach the ``SemanticChunker`` constructions the
multi-scale branch performs, or the settings UI must disclose that
``chunk_size_chars`` is inert under multi-scale. ``int(honored or disclosed)``
must be 1.

AC2 (test_context_budget_accounts_for_auto_qwen_prefix): the context-prefix
budget in ``ContextualChunker`` must charge the 82-char auto Qwen document
prefix against ``EmbeddingService.MAX_TEXT_LENGTH``, so an enriched chunk
stays embeddable. ``int(fits)`` must be 1.

AC3 (test_embed_batch_fail_fast_false_does_not_raise_on_oversized_text):
``embed_batch(..., fail_fast=False)`` must degrade oversized-text failures to
``failed_batch_indices`` entries instead of raising ``EmbeddingError`` from
the input-validation guard.

AC4 (test_max_text_length_comment_not_misattributed): the
``MAX_TEXT_LENGTH`` source comment must not claim derivation from
``chunk_size_chars=8192`` (the bound is the embedder's, not the chunker's).

AC5 (test_chunk_size_chars_upper_bound_rejected_at_config_time):
``Settings(chunk_size_chars=MAX_TEXT_LENGTH + 1)`` must be rejected at
config time with a pydantic ``ValidationError``.

AC6 (test_windows_launcher_tei_batch_cap_covers_default_batch_size): the
Windows launcher (``start-services.ps1``) must start TEI with a
``--max-client-batch-size`` cap at least as large as the
``embedding_batch_size`` default, or client-side batching silently exceeds
the server cap.

AC7 (test_contextualizer_prompt_bounded_by_model_context): the
contextualizer prompt must be bounded by ``model_context_tokens`` (4 chars
per token budget) instead of embedding the whole 60k-char document.

AC8 (test_contextualize_chunks_cancels_siblings_on_error): when one
per-chunk LLM call raises a non-LLMError, the still-pending sibling tasks
must be cancelled instead of continuing to mutate chunk texts after the
caller has caught the raise. ``changed`` must be 0.

AC9 (test_cache_identity_binds_embedding_endpoint): the embedding cache
identity must bind the embedding endpoint URL — changing
``ollama_embedding_url`` must change the identity. ``int(same)`` must be 0.

AC10 (test_embedding_cache_store_runs_off_event_loop): the synchronous
embedding-cache store must run OFF the event-loop thread.
``int(on_loop)`` must be 0.

AC11/AC12 are preserved by existing suites: ``issue513_checks/
test_c13_contextual_prefix_budget.py::test_c13_contextual_prefix_budget``
and ``test_contextual_chunking.py::TestContextualizeChunks::
test_chunks_are_contextualized``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_B09_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True, scope="module")
def _b09_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards.

    Mirrors backend/tests/conftest.py and the B08 modules' pattern: every app
    import in this module is function-local, so this module-scoped fixture
    has run (and the env keys are set) before the first ``app.*`` module is
    imported. All previous values are restored on teardown.
    """
    saved = {key: os.environ.get(key) for key in _B09_ENV}
    os.environ.update(_B09_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def test_chunk_size_setting_honored_or_disclosed_under_multiscale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (issue #698): chunk_size_chars honored or disclosed under multi-scale."""
    import app.services.document_processor as dp_module
    from app.config import settings

    recorded: list[int] = []

    class RecorderChunker:
        def __init__(self, chunk_size_chars=None, chunk_overlap_chars=None, **kw):
            recorded.append(chunk_size_chars)

        def chunk_elements(self, elements):
            return []

    class StubParser:
        def parse(self, path):
            return ["alpha beta gamma delta " * 5, "epsilon zeta eta theta " * 5]

    monkeypatch.setattr(dp_module, "SemanticChunker", RecorderChunker)
    monkeypatch.setattr(dp_module, "parse_elements_to_atoms", lambda *a, **k: [])
    monkeypatch.setattr(settings, "multi_scale_indexing_enabled", True)
    monkeypatch.setattr(settings, "chunk_size_chars", 300)
    monkeypatch.setattr(settings, "multi_scale_chunk_sizes", "768,1536")

    proc = dp_module.DocumentProcessor.__new__(dp_module.DocumentProcessor)
    proc.parser = StubParser()
    # The advisory _persist_extraction_diagnostics failure on this bare
    # instance is swallowed by the caller (logged warning only).
    await proc._process_document_file("probe.txt", file_id=1)
    honored = 300 in recorded

    tsx_text = (
        REPO / "frontend/src/components/settings/DocumentProcessingSettings.tsx"
    ).read_text(encoding="utf-8")
    disclosed = re.search(r"multi-?scale", tsx_text, re.I) is not None
    assert int(honored or disclosed) == 1


async def test_context_budget_accounts_for_auto_qwen_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (issue #698): context budget charges the auto Qwen doc prefix."""
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker
    from app.services.embeddings import _QWEN_DOC_PREFIX, EmbeddingService

    monkeypatch.setattr(settings, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setattr(settings, "embedding_doc_prefix", "")

    class StubLLM:
        async def chat_completion(self, **kw):
            return "c" * 300

    chunk = ProcessedChunk(text="d" * 8000, metadata={}, chunk_index=0)
    await ContextualChunker(StubLLM()).contextualize_chunks("d" * 400, [chunk], "f.txt")
    fits = len(chunk.text) + len(_QWEN_DOC_PREFIX) <= EmbeddingService.MAX_TEXT_LENGTH
    assert int(fits) == 1


async def test_embed_batch_fail_fast_false_does_not_raise_on_oversized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (issue #698): fail_fast=False degrades oversized text, not raises."""
    from app.config import settings
    from app.services.embeddings import EmbeddingService

    monkeypatch.setattr(settings, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setattr(settings, "embedding_doc_prefix", "")
    # EmbeddingService() SSRF-validates the URL at construction; the
    # oversized-text defect raises in input validation before any HTTP call.
    monkeypatch.setattr(
        "app.services.embeddings.assert_url_safe", lambda *a, **k: None
    )
    svc = EmbeddingService()
    svc._client = MagicMock()
    raised = "none"
    try:
        await svc.embed_batch(["x" * 8192, "short"], fail_fast=False)
    except Exception as e:
        raised = type(e).__name__
    assert raised == "none"


def test_max_text_length_comment_not_misattributed() -> None:
    """AC4 (issue #698): MAX_TEXT_LENGTH comment is not chunker-derived."""
    from app.services.embeddings import EmbeddingService

    stale = "derived from chunk_size_chars=8192" in inspect.getsource(EmbeddingService)
    assert int(stale) == 0


def test_chunk_size_chars_upper_bound_rejected_at_config_time() -> None:
    """AC5 (issue #698): chunk_size_chars over MAX_TEXT_LENGTH rejected."""
    from pydantic import ValidationError

    from app.config import Settings
    from app.services.embeddings import EmbeddingService

    rejected = False
    try:
        Settings(chunk_size_chars=EmbeddingService.MAX_TEXT_LENGTH + 1)
    except ValidationError:
        rejected = True
    assert int(rejected) == 1


def test_windows_launcher_tei_batch_cap_covers_default_batch_size() -> None:
    """AC6 (issue #698): TEI client batch cap >= embedding_batch_size default."""
    from app.config import Settings

    ps1_text = (REPO / "start-services.ps1").read_text(encoding="utf-8")
    match = re.search(r'"--max-client-batch-size"\s*,\s*"(\d+)"', ps1_text)
    cap = int(match.group(1)) if match else 32  # 32 = TEI server default
    default = Settings.model_fields["embedding_batch_size"].default
    assert cap >= default


async def test_contextualizer_prompt_bounded_by_model_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 (issue #698): contextualizer prompt honors model_context_tokens."""
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    monkeypatch.setattr(settings, "model_context_tokens", 4096)
    lengths: list[int] = []

    class StubLLM:
        async def chat_completion(self, messages=None, **kw):
            lengths.append(sum(len(m["content"]) for m in messages))
            return "ctx"

    doc = "z" * 60_000
    chunk = ProcessedChunk(text="y" * 100, metadata={}, chunk_index=0)
    await ContextualChunker(StubLLM()).contextualize_chunks(doc, [chunk], "f.txt")
    bounded = max(lengths) <= 4 * 4096
    assert int(bounded) == 1


async def test_contextualize_chunks_cancels_siblings_on_error() -> None:
    """AC8 (issue #698): sibling tasks are cancelled on a non-LLMError raise."""
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker

    class StubLLMRacy:
        def __init__(self):
            self.calls = 0

        async def chat_completion(self, messages=None, **kw):
            self.calls += 1
            if self.calls == 1:
                raise AttributeError("boom")
            await asyncio.sleep(0.2)
            return "ctx-marker"

    chunks = [
        ProcessedChunk(text=f"t{i}", metadata={}, chunk_index=i) for i in range(3)
    ]
    chunker = ContextualChunker(StubLLMRacy())
    # Keep ONE loop alive across the raise (the ingest caller logs a warning
    # and proceeds on the same loop), so still-pending sibling tasks keep
    # running after the caller catches — a fresh asyncio.run would cancel
    # them at shutdown and mask the defect.
    try:
        await chunker.contextualize_chunks("doc text", chunks, "f.txt")
    except AttributeError:
        pass
    await asyncio.sleep(0.5)
    changed = sum(1 for c in chunks if c.text != f"t{c.chunk_index}")
    assert changed == 0


def test_cache_identity_binds_embedding_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9 (issue #698): cache identity binds the embedding endpoint URL."""
    import app.services.document_processor as dp_module
    from app.config import settings

    fake_self = SimpleNamespace(
        embedding_service=SimpleNamespace(
            embedding_model="m",
            provider_mode="openai",
            embedding_doc_prefix="p",
            embedding_dim=4,
        )
    )
    monkeypatch.setattr(settings, "ollama_embedding_url", "http://a:8080/v1/embeddings")
    id1 = dp_module.DocumentProcessor._embedding_cache_identity(fake_self)
    monkeypatch.setattr(settings, "ollama_embedding_url", "http://b:8080/v1/embeddings")
    id2 = dp_module.DocumentProcessor._embedding_cache_identity(fake_self)
    same = id1 == id2
    assert int(same) == 0


async def test_embedding_cache_store_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC10 (issue #698): the synchronous cache store runs off the loop."""
    import app.services.document_processor as dp_module

    class FakeProc(dp_module.DocumentProcessor):
        def __init__(self):
            pass

    observed: dict = {}
    loop_thread: dict = {}
    loop_thread["t"] = threading.current_thread()

    async def fake_embed(texts, fail_fast=True):
        return ([[0.0, 0.0]] * len(texts), [])

    proc = FakeProc()
    proc.embedding_service = SimpleNamespace(embed_batch=fake_embed)
    monkeypatch.setattr(dp_module, "embedding_cache_lookup", lambda keys: {})
    monkeypatch.setattr(
        dp_module,
        "embedding_cache_store",
        lambda stored: observed.update(
            on_loop=threading.current_thread() is loop_thread["t"]
        ),
    )
    await dp_module.DocumentProcessor._embed_with_cache(proc, ["a"])
    on_loop = observed.get("on_loop")
    assert int(on_loop) == 0
