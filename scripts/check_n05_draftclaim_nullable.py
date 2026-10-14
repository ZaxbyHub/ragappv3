#!/usr/bin/env python3
"""Frozen AC8 check for issue #789: DraftClaim nullability parity.

The backend response model documents ``DraftClaim.claim_sha256`` /
``span_start`` / ``span_end`` as ``Optional`` (NULL when the claim has no
verifiable span; issue #517 / DRAFT-010), but the generated TypeScript
interface in ``frontend/src/lib/api/draftRoom.ts`` declares them non-null.
This script compares the two declarations textually:

1. Parse the ``DraftClaim`` pydantic class in
   ``backend/app/api/routes/draft_room.py`` and collect which of the three
   fields are annotated ``Optional[...]``.
2. Parse the ``DraftClaim`` interface in ``frontend/src/lib/api/draftRoom.ts``
   and collect each field's declared type text.
3. For every backend-Optional field whose TS type lacks ``null``, print
   ``NON-NULL DraftClaim.<field>`` and exit 1. Otherwise exit 0 silently.

Run from the repo root::

    python scripts/check_n05_draftclaim_nullable.py

Exit codes: 0 = parity holds; 1 = at least one NON-NULL line printed;
3 = the class or interface could not be located (structure changed, so the
check can no longer certify anything).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND_FILE = REPO / "backend" / "app" / "api" / "routes" / "draft_room.py"
FRONTEND_FILE = REPO / "frontend" / "src" / "lib" / "api" / "draftRoom.ts"

#: The three fields whose backend Optional-ness must be mirrored as
#: ``| null`` in the TS interface (issue #517, DRAFT-010).
FIELDS = ("claim_sha256", "span_start", "span_end")

_PY_CLASS_RE = re.compile(
    r"^class\s+DraftClaim\s*\([^)]*\)\s*:", re.MULTILINE
)
_PY_MEMBER_RE = re.compile(r"^\s+(?P<name>\w+)\s*:\s*(?P<ann>[^\n=]+)", re.MULTILINE)
_TS_INTERFACE_RE = re.compile(r"^export\s+interface\s+DraftClaim\s*\{", re.MULTILINE)
_TS_MEMBER_RE = re.compile(
    r"^\s+(?P<name>\w+)\s*\??\s*:\s*(?P<type>[^;]+);", re.MULTILINE
)


def _backend_block(source: str) -> str:
    match = _PY_CLASS_RE.search(source)
    if match is None:
        return ""
    rest = source[match.end():]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _ts_block(source: str) -> str:
    match = _TS_INTERFACE_RE.search(source)
    if match is None:
        return ""
    rest = source[match.end():]
    end = rest.find("}")
    return rest[: end if end != -1 else len(rest)]


def main() -> int:
    try:
        backend_source = BACKEND_FILE.read_text(encoding="utf-8")
        frontend_source = FRONTEND_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ABORT: could not read a source file: {exc}", file=sys.stderr)
        return 3

    py_block = _backend_block(backend_source)
    ts_block = _ts_block(frontend_source)
    if not py_block or not ts_block:
        print(
            "ABORT: DraftClaim class/interface not found where expected "
            f"({BACKEND_FILE.relative_to(REPO).as_posix()}, "
            f"{FRONTEND_FILE.relative_to(REPO).as_posix()})",
            file=sys.stderr,
        )
        return 3

    annotations = {
        m.group("name"): m.group("ann").strip()
        for m in _PY_MEMBER_RE.finditer(py_block)
    }
    ts_types = {
        m.group("name"): m.group("type").strip() for m in _TS_MEMBER_RE.finditer(ts_block)
    }

    violations = []
    for field in FIELDS:
        if field not in annotations:
            print(
                f"ABORT: DraftClaim.{field} disappeared from the backend model",
                file=sys.stderr,
            )
            return 3
        if "Optional[" not in annotations[field]:
            continue  # backend says non-null: TS non-null is correct parity
        ts_type = ts_types.get(field)
        if ts_type is None:
            print(
                f"ABORT: DraftClaim.{field} disappeared from the TS interface",
                file=sys.stderr,
            )
            return 3
        if "null" not in ts_type:
            violations.append(field)

    for field in violations:
        print(f"NON-NULL DraftClaim.{field}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
