#!/usr/bin/env python3
"""Issue #692 acceptance check AC6 (B03): upload-migration startup timeout.

`backend/app/lifespan.py` claims the upload migration runs "before accepting
requests" but drives it through `asyncio.wait_for(asyncio.to_thread(
migrate_uploads, ...), timeout=15)`. `asyncio.to_thread` is NOT
cancellable-synchronous: a timeout fires, the request loop proceeds, and the
thread keeps copying/renaming files concurrently with serving traffic — the
claim and the mechanism contradict each other.

This check is fix-agnostic: removing EITHER the uncancellable timeout or the
"before accepting requests" claim makes it pass. Run from the repo root:

    python scripts/check_b03_upload_migration_timeout.py

Exit codes: 0 = OK, 1 = the contradiction is still present.
"""

import sys
from pathlib import Path

CLAIM_SUBSTRING = "before accepting requests"
CALL_SUBSTRING = "wait_for(asyncio.to_thread(migrate_uploads"

FAIL_LINE = (
    'FAIL: lifespan.py claims "before accepting requests" but uses '
    "wait_for(asyncio.to_thread(migrate_uploads ...))"
)


def main() -> int:
    src_path = Path(__file__).resolve().parents[1] / "backend" / "app" / "lifespan.py"
    try:
        source = src_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL: could not read {src_path}: {exc}")
        return 1

    if CLAIM_SUBSTRING in source and CALL_SUBSTRING in source:
        print(FAIL_LINE)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
