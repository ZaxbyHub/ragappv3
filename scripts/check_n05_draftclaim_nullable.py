#!/usr/bin/env python3
"""Frozen AC8 check for issue #789: DraftClaim nullability parity.

The backend response models document ``DraftClaim`` (and, since issue #912,
``DraftClaimSource``) fields as ``Optional`` (NULL when the claim has no
verifiable span; issue #517 / DRAFT-010) while the hand-maintained TypeScript
interfaces in ``frontend/src/lib/api/draftRoom.ts`` must mirror that
nullability. This script compares each declaration pair textually, in BOTH
directions:

1. Parse the ``DraftClaim`` and ``DraftClaimSource`` pydantic classes in
   ``backend/app/api/routes/draft_room.py`` and collect every field's
   annotation, treating ``Optional[...]`` and PEP 604 ``... | None`` alike.
2. Parse the ``DraftClaim`` and ``DraftClaimSource`` interfaces in
   ``frontend/src/lib/api/draftRoom.ts`` and collect each field's declared
   type text.
3. Compare nullability per field and class, and report the mismatch:
   ``NON-NULL`` (backend nullable, TS not), ``OVER-NULL`` (TS nullable, backend
   not), or ``UNMIRRORED`` (the field exists on only one side). Any of them
   exits 1; parity exits 0 silently.

Hardened in the PR's review round (issue #789 F-005): the check no longer
inspects a frozen three-field list, so a newly added backend field cannot slip
past it, and a missing pinned field aborts rather than passing. Issue #912
extended the same contract to ``DraftClaimSource`` (the maintainer's named
residual): every class below gets the full bidirectional comparison and its
own structural fail-closed.

Run from the repo root::

    python scripts/check_n05_draftclaim_nullable.py

Exit codes: 0 = parity holds; 1 = at least one mismatch line printed;
3 = a pinned field, a class, or an interface could not be located (the
structure changed, so the check can no longer certify anything).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND_FILE = REPO / "backend" / "app" / "api" / "routes" / "draft_room.py"
FRONTEND_FILE = REPO / "frontend" / "src" / "lib" / "api" / "draftRoom.ts"

#: Each parity pair: (class name, fields whose presence is structural). The
#: nullable fields are the drift that actually happened (issue #517 /
#: DRAFT-010 for DraftClaim; issue #912 for DraftClaimSource); the comparison
#: itself is not limited to them.
CLASS_SPECS = (
    ("DraftClaim", ("claim_sha256", "span_start", "span_end")),
    (
        "DraftClaimSource",
        ("passage_start", "passage_end", "lexical_overlap_score"),
    ),
)


def _class_re(class_name: str) -> re.Pattern[str]:
    return re.compile(rf"^class\s+{class_name}\s*\([^)]*\)\s*:", re.MULTILINE)


def _interface_re(class_name: str) -> re.Pattern[str]:
    return re.compile(rf"^export\s+interface\s+{class_name}\s*\{{", re.MULTILINE)


_PY_MEMBER_RE = re.compile(r"^\s+(?P<name>\w+)\s*:\s*(?P<ann>[^\n=]+)", re.MULTILINE)
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


def _backend_block(source: str, class_name: str) -> str:
    match = _class_re(class_name).search(source)
    if match is None:
        return ""
    rest = source[match.end():]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _ts_block(source: str, class_name: str) -> str:
    match = _interface_re(class_name).search(source)
    if match is None:
        return ""
    rest = source[match.end():]
    end = rest.find("}")
    return rest[: end if end != -1 else len(rest)]


def _abort(message: str) -> int:
    print(f"ABORT: {message}", file=sys.stderr)
    return 3


def main() -> int:
    try:
        backend_source = BACKEND_FILE.read_text(encoding="utf-8")
        frontend_source = FRONTEND_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        return _abort(f"could not read a source file: {exc}")

    violations = []
    for class_name, required_fields in CLASS_SPECS:
        py_block = _backend_block(backend_source, class_name)
        ts_block = _ts_block(frontend_source, class_name)
        if not py_block or not ts_block:
            missing = []
            if not py_block:
                missing.append("backend model")
            if not ts_block:
                missing.append("TS interface")
            return _abort(
                f"{class_name} class/interface not found where expected "
                f"({BACKEND_FILE.relative_to(REPO).as_posix()}, "
                f"{FRONTEND_FILE.relative_to(REPO).as_posix()}; missing: "
                f"{' and '.join(missing)})"
            )

        annotations = {
            m.group("name"): m.group("ann").strip()
            for m in _PY_MEMBER_RE.finditer(py_block)
        }
        ts_types = {
            m.group("name"): m.group("type").strip()
            for m in _TS_MEMBER_RE.finditer(ts_block)
        }

        # Structural break -> abort, never a silent pass (issue #789 review
        # F-005). Losing a pinned field is a contract break, not a
        # nullability finding.
        for field in required_fields:
            if field not in annotations or field not in ts_types:
                missing = []
                if field not in annotations:
                    missing.append("backend model")
                if field not in ts_types:
                    missing.append("TS interface")
                return _abort(
                    f"{class_name}.{field} is missing from the "
                    f"{' and '.join(missing)}"
                )

        # A field present on only one side is drift the nullability
        # comparison cannot express — the two models carry identical field
        # sets, so a mismatch is a real finding, not a tolerated difference
        # (issue #789 review F-005). Each violation is (field, kind, detail)
        # with kind one of MISSING / NON_NULL / OVER_NULL.
        for field in sorted(set(annotations) ^ set(ts_types)):
            absent_from = (
                "the TS interface" if field in annotations else "the backend model"
            )
            violations.append((class_name, field, "MISSING", absent_from))
        for field in sorted(set(annotations) & set(ts_types)):
            backend_nullable = _is_optional(annotations[field])
            ts_nullable = "null" in ts_types[field]
            if backend_nullable and not ts_nullable:
                violations.append((class_name, field, "NON_NULL", None))
            elif ts_nullable and not backend_nullable:
                violations.append((class_name, field, "OVER_NULL", None))

    for class_name, field, kind, detail in violations:
        if kind == "MISSING":
            print(f"UNMIRRORED {class_name}.{field} (absent from {detail})")
        elif kind == "NON_NULL":
            # Backend emits NULL for this field; the client type must allow it.
            print(f"NON-NULL {class_name}.{field}")
        else:
            # Backend dropped Optional; a `| null` client type is now stricter
            # than the wire contract and will drift the other way.
            print(f"OVER-NULL {class_name}.{field}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
