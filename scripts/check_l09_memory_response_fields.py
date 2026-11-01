#!/usr/bin/env python3
"""Issue #780 L09 / AC5 — AddMemoryResponse must not declare fields the backend MemoryResponse never sends.

addMemory posts to /memories, whose response body is the backend
MemoryResponse pydantic model. A TS field the backend never sends is a
phantom contract: it type-checks today and reads as undefined at runtime
(issue #780 defect 5: AddMemoryResponse.status). This check resolves the
AddMemoryResponse type declared in frontend/src/lib/api/memories.ts — either
an `export interface AddMemoryResponse {` declaration, or an
`export type AddMemoryResponse = <Other>;` alias resolved to <Other>'s
`export interface <Other> {` in memories.ts first, then the other modules
under frontend/src/lib/api/ — and compares its field names with the annotated
fields of `class MemoryResponse(BaseModel):` in
backend/app/api/routes/memories.py. Any TS field absent from the pydantic
field set prints "PHANTOM AddMemoryResponse.<field>" (one line per phantom)
and the script exits 1; none prints "PHANTOM 0" and exits 0. A missing or
unparseable declaration on either side fails closed with exit 2.

Parser hardening (review feedback, PR #880): the TS side tracks brace depth
AFTER skipping // and /* */ comment text, so a `}` inside a comment can no
longer truncate the compared field list, and only fields at the interface's
top level (depth 1, before the field's own brace) are collected, so nested
object keys no longer count as fields. The pydantic side counts only lines
at exactly class-body indent (4 spaces) with a `name:` annotation, with
triple-quoted docstrings stripped first, so docstring prose and
method-local annotations no longer mask phantoms.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TS_FILE = ROOT / "frontend" / "src" / "lib" / "api" / "memories.ts"
API_DIR = ROOT / "frontend" / "src" / "lib" / "api"
PY_FILE = ROOT / "backend" / "app" / "api" / "routes" / "memories.py"

TS_FIELD_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\??\s*:")
PY_FIELD_RE = re.compile(r"^    ([A-Za-z_]\w*)\s*:")
ALIAS_RE = re.compile(r"export\s+type\s+AddMemoryResponse\s*=\s*([A-Za-z_$][\w$]*)\s*;")


def parse_interface_fields(text: str, type_name: str) -> list[str] | None:
    """Top-level field names of `export interface <type_name> { ... }`, or None if absent."""
    marker = f"export interface {type_name} {{"
    start = text.find(marker)
    if start == -1:
        return None
    lines = text[start:].splitlines()
    fields: list[str] = []
    depth = lines[0].count("{") - lines[0].count("}")
    in_block_comment = False
    for line in lines[1:]:
        if depth <= 0:
            break
        stripped = line.strip()
        if not stripped:
            continue
        # Comment handling precedes depth accounting so a `}` inside a
        # comment cannot end the interface early.
        if in_block_comment:
            end = stripped.find("*/")
            if end == -1:
                continue
            line = line[line.index("*/") + 2 :]
            in_block_comment = False
            stripped = line.strip()
            if not stripped:
                continue
        if stripped.startswith("//"):
            continue
        block_start = stripped.find("/*")
        if block_start != -1:
            end = stripped.find("*/", block_start + 2)
            if end == -1:
                in_block_comment = True
                line = line[: line.index("/*")]
            else:
                line = line[:block_start] + line[end + 2 :]
        # Inline trailing comments must not feed the depth accounting (a `}`
        # inside `field: string; // }` would otherwise close the interface).
        code_part = line.split("//", 1)[0] if "//" in line else line
        depth_before = depth
        depth += code_part.count("{") - code_part.count("}")
        if depth_before <= 0:
            break
        if depth_before != 1:
            continue  # nested object keys are not interface fields
        match = TS_FIELD_RE.match(line)
        if match:
            fields.append(match.group(1))
    return fields


def resolve_ts_fields(ts_text: str) -> list[str] | None:
    """Resolve AddMemoryResponse to a concrete interface's field names."""
    fields = parse_interface_fields(ts_text, "AddMemoryResponse")
    if fields is not None:
        return fields
    alias = ALIAS_RE.search(ts_text)
    if alias is None:
        return None
    target = alias.group(1)
    # Alias target's interface: memories.ts first, then the other api modules.
    for candidate in [TS_FILE, *sorted(p for p in API_DIR.glob("*.ts") if p != TS_FILE)]:
        try:
            candidate_text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        fields = parse_interface_fields(candidate_text, target)
        if fields is not None:
            return fields
    return None


def parse_pydantic_fields(py_text: str) -> list[str] | None:
    """Annotated field names of `class MemoryResponse(BaseModel):`, or None.

    Only lines at exactly class-body indent (4 spaces) with a `name:`
    annotation count; triple-quoted docstrings are stripped first so their
    prose and any method-local `name:` annotations cannot masquerade as
    fields.
    """
    class_match = re.search(
        r"^class\s+MemoryResponse\s*\([^)]*\)\s*:", py_text, re.MULTILINE
    )
    if class_match is None:
        return None
    fields: list[str] = []
    in_docstring = False
    docstring_delim = ""
    for line in py_text[class_match.end() :].splitlines():
        if line.strip() and not line[0].isspace():
            # Next top-level statement (class / @ / async def / ...) ends the
            # class body.
            break
        stripped = line.strip()
        if not stripped:
            continue
        if in_docstring:
            if docstring_delim in stripped:
                in_docstring = False
            continue
        triple = re.match(r'"""|\'\'\'', stripped)
        if triple:
            docstring_delim = triple.group(0)
            rest = stripped[3:]
            if docstring_delim not in rest:
                in_docstring = True
                continue
            continue
        if stripped.startswith("#"):
            continue
        field_match = PY_FIELD_RE.match(line)
        if field_match:
            fields.append(field_match.group(1))
    return fields


def main() -> int:
    missing_paths = [p for p in (TS_FILE, API_DIR, PY_FILE) if not p.exists()]
    if missing_paths:
        print(f"memory-response-fields: required paths not found: {missing_paths}", file=sys.stderr)
        return 2
    try:
        ts_text = TS_FILE.read_text(encoding="utf-8")
        py_text = PY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"memory-response-fields: {exc}", file=sys.stderr)
        return 2

    ts_fields = resolve_ts_fields(ts_text)
    if not ts_fields:
        print(
            "memory-response-fields: AddMemoryResponse declaration not found or unparseable",
            file=sys.stderr,
        )
        return 2
    py_fields = parse_pydantic_fields(py_text)
    if py_fields is None:
        print(
            "memory-response-fields: class MemoryResponse(BaseModel) not found or unparseable",
            file=sys.stderr,
        )
        return 2

    py_field_set = set(py_fields)
    phantoms = [field for field in ts_fields if field not in py_field_set]
    if phantoms:
        for field in phantoms:
            print(f"PHANTOM AddMemoryResponse.{field}")
        return 1
    print("PHANTOM 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
