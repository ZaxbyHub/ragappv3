"""Issue #513 AC12 (INGEST-014): embedding chunking must not drop short boundary text.

Under embedding-strategy semantic chunking, every input sentence must survive
in the emitted chunk text (modulo documented overlap/whitespace normalization)
for the short-prefix, short-suffix, and semantic-breakpoint cases. A
deterministic stub embedding service assigns one vector per topic so the
percentile breakpoint detector produces exact, reproducible boundaries; the
short sentences (< min_chunk_size) land alone in boundary chunks that the
pre-fix minimum-size filter silently discards.

DISCRIMINATING: at the pre-fix commit ``if len(chunk_text) >=
self.min_chunk_size`` drops those boundary chunks, so this script prints
``C12 CHECK: FAIL: ...`` and exits 1.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c12_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP

ALPHA_VEC = [1.0, 0.0]
OMEGA_VEC = [0.0, 1.0]
MID_VEC = [0.6, 0.8]  # distinct third topic: similarity < 1 against both others


class TopicEmbeddingStub:
    """Deterministic embedding seam: vector chosen by topic marker in the text."""

    async def embed_batch(self, texts, **_kwargs):
        out = []
        for text in texts:
            if "OMEGA" in text:
                out.append(OMEGA_VEC)
            elif "MIDTOPIC" in text:
                out.append(MID_VEC)
            else:
                out.append(ALPHA_VEC)
        return out


def _long(marker: str, topic: str) -> str:
    # > min_chunk_size (100 chars) so only the short boundary sentences can be
    # filtered, and well under max_chunk_size so no size-based splits occur.
    body = (f"{topic} filler sentence content {marker} " * 4).strip()
    assert len(body) > 120
    return body.rstrip() + "." if not body.endswith(".") else body


def _case_sentences():
    filler = "report narrative "
    long_a1 = f"ZQXA1 {filler}" + ("alpha detail. " * 10).strip()
    long_a2 = f"ZQXA2 {filler}" + ("alpha detail. " * 10).strip()
    long_a3 = f"ZQXA3 {filler}" + ("alpha detail. " * 10).strip()
    long_b1 = f"ZQXB1 {filler}" + ("omega detail. " * 10).strip()
    long_b2 = f"ZQXB2 {filler}" + ("omega detail. " * 10).strip()

    cases = {
        # Short leading sentence (own topic -> breakpoint right after it).
        "short-prefix": (
            ["ZQXPFX Keep this short OMEGA lead.", long_a1, long_a2, long_a3],
            ["ZQXPFX", "ZQXA1", "ZQXA2", "ZQXA3"],
        ),
        # Short trailing sentence (breakpoint right before it).
        "short-suffix": (
            [long_a1, long_a2, long_a3, "ZQXSFX Keep this short OMEGA tail."],
            ["ZQXA1", "ZQXA2", "ZQXA3", "ZQXSFX"],
        ),
        # Short sentence isolated by semantic breakpoints on both sides.
        "semantic-breakpoint": (
            [
                long_b1,
                long_b2,
                "ZQXMID Keep this short MIDTOPIC note.",
                _long("ZQXC1", "alpha"),
                _long("ZQXC2", "alpha"),
            ],
            ["ZQXB1", "ZQXB2", "ZQXMID", "ZQXC1", "ZQXC2"],
        ),
    }
    return cases


def main() -> int:
    from app.services.chunking import EmbeddingSemanticChunker, ThresholdType

    chunker = EmbeddingSemanticChunker(
        embedding_service=TopicEmbeddingStub(),
        threshold_type=ThresholdType.PERCENTILE,
        threshold_value=0.5,
        min_chunk_size=100,
        max_chunk_size=2000,
        window_size=1,  # one sentence per window -> exact similarity control
    )

    for name, (sentences, markers) in _case_sentences().items():
        text = " ".join(sentences)
        try:
            chunks = asyncio.run(chunker.chunk_text(text))
        except Exception as exc:  # noqa: BLE001 - verdict, not crash
            print(f"C12 CHECK: FAIL: {name} chunk_text raised {type(exc).__name__}: {exc}")
            return 1

        combined = "\n".join((getattr(c, "text", "") or "") for c in chunks)
        missing = [m for m in markers if m not in combined]
        if missing:
            print(
                f"C12 CHECK: FAIL: {name} case dropped source sentence(s) "
                f"{missing} from embedding-chunk output (min-size filter "
                f"discarded short boundary chunks)"
            )
            return 1

    print("C12 CHECK: PASS")
    return 0


def test_c12_short_boundary_sentences():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
