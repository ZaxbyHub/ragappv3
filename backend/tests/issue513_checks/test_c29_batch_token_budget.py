"""Issue #513 AC29: embedding batching bounded by item count AND token cost.

``EmbeddingService.embed_batch`` is driven with a recording backend stub (no
network). With a count budget large enough to hold every text in one batch,
the assembled batches must still be bounded by an estimated token/char budget:
no assembled batch may exceed a generous char ceiling (128 KiB, i.e. ~32k
tokens at 4 chars/token — above every mainstream embedding context window),
every input text must be embedded exactly once with provenance order retained,
and a single oversized (near per-text-cap) item submitted alone must still be
sent as its own batch.

DISCRIMINATING: at the pre-fix commit batching is count-only, so all texts
land in ONE unbounded batch and this script prints ``C29 CHECK: FAIL: ...``
and exits 1.
"""

import asyncio
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c29_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP
# Public IP literal: passes the SSRF startup guard without any DNS/network
# lookup; every request is intercepted by the recording backend stub below.
SAFE_EMBEDDING_URL = "http://93.184.216.34:8080/v1/embeddings"
os.environ["OLLAMA_EMBEDDING_URL"] = SAFE_EMBEDDING_URL
os.environ["EMBEDDING_DOC_PREFIX"] = ""

# Generous ceiling any token-cost-bounded implementation must stay under:
# 32k tokens x 4 chars/token. A batch larger than this cannot be described as
# bounded by token cost for any mainstream embedding model context window.
BATCH_CHAR_CEILING = 128 * 1024

N_TEXTS = 160
TEXT_CHARS = 4000  # each far below the 8192 per-text validation cap


class RecordingBackend:
    """Replaces EmbeddingService._embed_batch_api; records assembled batches."""

    def __init__(self):
        self.batches: list = []

    async def __call__(self, texts, config=None):
        self.batches.append(list(texts))
        return [[0.0, 1.0]] * len(texts)


def _check_provenance(batches, texts):
    index_of = {t: i for i, t in enumerate(texts)}
    embedded = Counter()
    for batch in batches:
        indices = [index_of.get(t) for t in batch]
        if any(i is None for i in indices):
            return "a batch contained an unknown text"
        for i, t in zip(indices, batch):
            embedded[t] += 1
        if indices != sorted(indices):
            return "batch contents not in input order (provenance lost)"
        total_chars = sum(len(t) for t in batch)
        if total_chars > BATCH_CHAR_CEILING:
            return (
                f"assembled batch of {len(batch)} texts totals {total_chars} chars "
                f"(> {BATCH_CHAR_CEILING} char token-cost ceiling) — batching is "
                "not bounded by token/size cost"
            )
    duplicated = [t[:20] for t, n in embedded.items() if n != 1]
    if duplicated:
        return f"provenance broken: texts embedded != once: {duplicated[:3]}"
    missing = [t[:20] for t in texts if embedded.get(t, 0) < 1]
    if missing:
        return f"texts dropped from embedding output: {missing[:3]}"
    return None


def main() -> int:
    from app.config import settings
    from app.services.embeddings import EmbeddingService

    # Ensure the offline-safe URL and empty doc prefix regardless of whether
    # this module's env vars won the import race (pytest co-run).
    saved_url = settings.ollama_embedding_url
    saved_prefix = settings.embedding_doc_prefix
    settings.ollama_embedding_url = SAFE_EMBEDDING_URL
    settings.embedding_doc_prefix = ""

    try:
        try:
            svc = EmbeddingService()
        except Exception as exc:  # noqa: BLE001 - verdict, not crash
            print(f"C29 CHECK: FAIL: EmbeddingService init raised {type(exc).__name__}: {exc}")
            return 1
        recorder = RecordingBackend()
        svc._embed_batch_api = recorder  # noqa: SLF001 - the batch backend seam

        # Scenario A: count budget (512) holds every text, but total size is huge.
        texts = [f"TXTA{i:04d} " + ("a" * TEXT_CHARS) for i in range(N_TEXTS)]
        total_chars = sum(len(t) for t in texts)
        if total_chars <= BATCH_CHAR_CEILING:
            print("C29 CHECK: FAIL: test setup error - scenario total below ceiling")
            return 1
        try:
            out = asyncio.run(svc.embed_batch(texts, batch_size=512))
        except Exception as exc:  # noqa: BLE001 - verdict, not crash
            print(f"C29 CHECK: FAIL: embed_batch raised {type(exc).__name__}: {exc}")
            return 1
        if len(out) != N_TEXTS:
            print(f"C29 CHECK: FAIL: embed_batch returned {len(out)} embeddings for {N_TEXTS} texts")
            return 1
        if not recorder.batches:
            print("C29 CHECK: FAIL: no batches reached the embedding backend")
            return 1
        reason = _check_provenance(recorder.batches, texts)
        if reason:
            print(f"C29 CHECK: FAIL: {reason}")
            return 1
        if len(recorder.batches) < 2:
            print(
                f"C29 CHECK: FAIL: {N_TEXTS} texts totalling {total_chars} chars were "
                f"assembled into {len(recorder.batches)} unbounded batch(es) — "
                "batching is count-only with no token/size budget"
            )
            return 1

        # Scenario B: one near-cap oversized text submitted alone must be sent
        # alone (single-item input -> single-item batch), not dropped or split.
        big = "BIGMARKER " + ("b" * (EmbeddingService.MAX_TEXT_LENGTH - 20))
        recorder.batches = []
        try:
            asyncio.run(svc.embed_batch([big], batch_size=512))
        except Exception as exc:  # noqa: BLE001 - verdict, not crash
            print(f"C29 CHECK: FAIL: oversized single-text call raised {type(exc).__name__}: {exc}")
            return 1
        if len(recorder.batches) != 1 or recorder.batches[0] != [big]:
            print(
                "C29 CHECK: FAIL: oversized single text was not sent alone as its own "
                f"batch (batches={[len(b) for b in recorder.batches]})"
            )
            return 1
    finally:
        settings.ollama_embedding_url = saved_url
        settings.embedding_doc_prefix = saved_prefix

    print("C29 CHECK: PASS")
    return 0


def test_c29_batch_token_budget():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
