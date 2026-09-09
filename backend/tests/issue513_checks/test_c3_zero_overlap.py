"""Issue #513 AC3 (INGEST-003): live ``chunk_overlap_chars=0`` must be honored.

``DocumentProcessor._get_chunker`` reads the live settings values and falls
back to the constructor args when unset. An explicitly configured zero overlap
must yield a chunker with zero overlap (an optional setting's zero value is a
value, not "unset"), while leaving the settings unset must still use the
constructor fallback.

DISCRIMINATING: at the pre-fix commit ``settings.chunk_overlap_chars or
self._chunk_overlap_fallback`` treats 0 as unset, so this script prints
``C3 CHECK: FAIL: ...`` and exits 1.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (this file: ROOT/backend/tests/issue513_checks/)
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="issue513_c3_")
os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
os.environ["USERS_ENABLED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only-min-32-chars"
os.environ["REDIS_URL"] = ""
os.environ["DATA_DIR"] = _TMP

FALLBACK_SIZE = 2000
FALLBACK_OVERLAP = 200
LIVE_SIZE = 1500


def main() -> int:
    from app.config import settings
    from app.services.document_processor import DocumentProcessor

    saved = (settings.chunk_size_chars, settings.chunk_overlap_chars)
    try:
        processor = DocumentProcessor(
            chunk_size_chars=FALLBACK_SIZE, chunk_overlap_chars=FALLBACK_OVERLAP
        )

        # Unset settings must keep using the constructor fallback.
        settings.chunk_size_chars = None
        settings.chunk_overlap_chars = None
        fallback_chunker = processor._get_chunker()  # noqa: SLF001 - unit under test
        if getattr(fallback_chunker, "chunk_overlap", None) != FALLBACK_OVERLAP:
            print(
                "C3 CHECK: FAIL: unset chunk_overlap_chars did not fall back to the "
                f"constructor value (got {getattr(fallback_chunker, 'chunk_overlap', None)!r}, "
                f"expected {FALLBACK_OVERLAP})"
            )
            return 1

        # Live zero overlap must be honored, not treated as unset.
        settings.chunk_size_chars = LIVE_SIZE
        settings.chunk_overlap_chars = 0
        live_chunker = processor._get_chunker()  # noqa: SLF001 - unit under test
        overlap = getattr(live_chunker, "chunk_overlap", None)
        if overlap != 0:
            print(
                "C3 CHECK: FAIL: live settings chunk_overlap_chars=0 was ignored "
                f"(chunker overlap is {overlap!r}; falsy-zero fell through to the "
                f"fallback {FALLBACK_OVERLAP})"
            )
            return 1
        if getattr(live_chunker, "chunk_size", None) != LIVE_SIZE:
            print(
                "C3 CHECK: FAIL: live settings chunk_size_chars was not applied "
                f"(got {getattr(live_chunker, 'chunk_size', None)!r}, expected {LIVE_SIZE})"
            )
            return 1
    finally:
        settings.chunk_size_chars, settings.chunk_overlap_chars = saved

    print("C3 CHECK: PASS")
    return 0


def test_c3_zero_overlap():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
