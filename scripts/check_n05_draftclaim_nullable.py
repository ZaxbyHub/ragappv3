#!/usr/bin/env python3
"""Frozen AC8 check for issue #789: DraftClaim nullability parity.

The backend response model documents ``DraftClaim`` fields as ``Optional``
(NULL when the claim has no verifiable span; issue #517 / DRAFT-010) while the
hand-maintained TypeScript interface in
``frontend/src/lib/api/draftRoom.ts`` must mirror that nullability. This
script compares the two declarations textually, in BOTH directions:

1. Parse the ``DraftClaim`` pydantic class in
   ``backend/app/api/routes/draft_room.py`` and collect every field's
   annotation, treating ``Optional[...]`` and PEP 604 ``... | None`` alike.
2. Parse the ``DraftClaim`` interface in ``frontend/src/lib/api/draftRoom.ts``
   and collect each field's declared type text.
3. Compare nullability per field and report the mismatch:
   ``NON-NULL`` (backend nullable, TS not), ``OVER-NULL`` (TS nullable, backend
   not), or ``UNMIRRORED`` (the field exists on only one side). Any of them
   exits 1; parity exits 0 silently.

Hardened in the PR's review round (issue #789 F-005): the check no longer
inspects a frozen three-field list, so a newly added backend field cannot slip
past it, and a missing pinned field aborts rather than passing.

Run from the repo root::

    python scripts/check_n05_draftclaim_nullable.py

Exit codes: 0 = parity holds; 1 = at least one mismatch line printed;
3 = a pinned field, the class, or the interface could not be located (the
structure changed, so the check can no longer certify anything).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND_FILE = REPO / "backend" / "app" / "api" / "routes" / "draft_room.py"
FRONTEND_FILE = REPO / "frontend" / "src" / "lib" / "api" / "draftRoom.ts"

#: The three fields whose backend Optional-ness must be mirrored as
#: ``| null`` in the TS interface (issue #517, DRAFT-010). They are the drift
#: that actually happened; the check below is not limited to them.
FIELDS = ("claim_sha256", "span_start", "span_end")

#: Fields that must keep existing on BOTH sides. Losing one is a structural
#: break, not a nullability finding, so it aborts (exit 3) instead of passing.
REQUIRED_FIELDS = FIELDS

_PY_CLASS_RE = re.compile(
    r"^class\s+DraftClaim\s*\([^)]*\)\s*:", re.MULTILINE
)
_PY_MEMBER_RE = re.compile(r"^\s+(?P<name>\w+)\s*:\s*(?P<ann>[^\n=]+)", re.MULTILINE)
_TS_INTERFACE_RE = re.compile(r"^export\s+interface\s+DraftClaim\s*\{", re.MULTILINE)
_TS_MEMBER_RE = re.compile(
    r"^\s+(?P<name>\w+)\s*\??\s*:\s*(?P<type>[^;]+);", re.MULTILINE
)


def _is_optional(annotation: str) -> bool:
    """True when a backend annotation can hold NULL.

    Accepts both spellings — ``Optional[str]`` and PEP 604 ``str | None`` —
    so reformatting one annotation cannot silently disable the check
    (issue #789 review F-005).
    """
    return "Optional[" in annotation or "| None" in annotation


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

    # Structural break -> abort, never a silent pass (issue #789 review F-005).
    for field in REQUIRED_FIELDS:
        if field not in annotations or field not in ts_types:
            missing = []
            if field not in annotations:
                missing.append("backend model")
            if field not in ts_types:
                missing.append("TS interface")
            print(
                f"ABORT: DraftClaim.{field} is missing from the {' and '.join(missing)}",
                file=sys.stderr,
            )
            return 3

    violations = []
    # A field present on only one side is drift the nullability comparison
    # cannot express, and today the two models carry an identical field set —
    # so a mismatch is a real finding, not a tolerated difference
    # (issue #789 review F-005). Each violation is (field, kind, detail) with
    # kind one of MISSING / NON_NULL / OVER_NULL.
    for field in sorted(set(annotations) ^ set(ts_types)):
        absent_from = "the TS interface" if field in annotations else "the backend model"
        violations.append((field, "MISSING", absent_from))
    for field in sorted(set(annotations) & set(ts_types)):
        backend_nullable = _is_optional(annotations[field])
        ts_nullable = "null" in ts_types[field]
        if backend_nullable and not ts_nullable:
            violations.append((field, "NON_NULL", None))
        elif ts_nullable and not backend_nullable:
            violations.append((field, "OVER_NULL", None))

    for field, kind, detail in violations:
        if kind == "MISSING":
            print(f"UNMIRRORED DraftClaim.{field} (absent from {detail})")
        elif kind == "NON_NULL":
            # Backend emits NULL for this field; the client type must allow it.
            print(f"NON-NULL DraftClaim.{field}")
        else:
            # Backend dropped Optional; a `| null` client type is now stricter
            # than the wire contract and will drift the other way.
            print(f"OVER-NULL DraftClaim.{field}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
