"""Issue #513 AC13 (INGEST-015): contextual prefix must not break embeddability.

A canonical chunk sized just under the embedding input validation bound
(``EmbeddingService.MAX_TEXT_LENGTH`` minus any doc prefix) is enriched by
``ContextualChunker`` with a stub LLM returning a non-trivial context prefix.
After contextualization the constructed chunk text must still satisfy the
exact per-text length validation ``embed_batch`` applies (so the chunk stays
embeddable), while the canonical raw text is preserved untruncated.

DISCRIMINATING: at the pre-fix commit the prefix is prepended unbounded
(``chunk.text = f"{context}\\n\\n{chunk.text}"``) and only ``doc_prefix`` is
budgeted in validation, so this script prints ``C13 CHECK: FAIL: ...`` and
exits 1.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c13_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP

CONTEXT_LEN = 600


class StubContextLLM:
    """Offline LLM seam returning a fixed non-trivial context string."""

    async def chat_completion(self, messages, temperature=0.3, max_tokens=150):
        return "CTXMARKER " + ("c" * CONTEXT_LEN)


def main() -> int:
    from app.config import settings
    from app.services.chunking import ProcessedChunk
    from app.services.contextual_chunking import ContextualChunker
    from app.services.embeddings import EmbeddingService

    # Mirror embeddings.embed_batch input validation exactly (embeddings.py):
    #   effective_max = EmbeddingService.MAX_TEXT_LENGTH - len(doc_prefix)
    prefix_len = len(getattr(settings, "embedding_doc_prefix", "") or "")
    effective_max = EmbeddingService.MAX_TEXT_LENGTH - prefix_len

    canonical = "d" * (effective_max - 100)  # near-limit, embeddable on its own
    if len(canonical) > effective_max:
        print("C13 CHECK: FAIL: test setup error - canonical text already over bound")
        return 1

    chunk = ProcessedChunk(text=canonical, metadata={}, chunk_index=0)
    chunker = ContextualChunker(StubContextLLM())

    try:
        asyncio.run(
            chunker.contextualize_chunks(
                document_text="Doc level context " + ("x" * 400),
                chunks=[chunk],
                source_filename="near_limit.txt",
            )
        )
    except Exception as exc:  # noqa: BLE001 - verdict, not crash
        print(f"C13 CHECK: FAIL: contextualize_chunks raised {type(exc).__name__}: {exc}")
        return 1

    if chunk.metadata.get("contextualized") is not True:
        print(
            "C13 CHECK: FAIL: contextualization did not run "
            f"(metadata contextualized={chunk.metadata.get('contextualized')!r})"
        )
        return 1

    if len(chunk.text) > effective_max:
        print(
            f"C13 CHECK: FAIL: context prefix pushed the near-limit chunk past the "
            f"embedding input bound (constructed {len(chunk.text)} chars > "
            f"validation bound {effective_max}); embed_batch would reject it"
        )
        return 1

    raw = getattr(chunk, "raw_text", None)
    if raw != canonical:
        print(
            "C13 CHECK: FAIL: canonical raw text not preserved untruncated "
            f"(raw_text {'is None' if raw is None else f'len={len(raw)}'} "
            f"vs canonical len={len(canonical)})"
        )
        return 1

    print("C13 CHECK: PASS")
    return 0


def test_c13_contextual_prefix_budget():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
