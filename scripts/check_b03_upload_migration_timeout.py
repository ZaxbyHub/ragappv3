#!/usr/bin/env python3
"""Issue #692 acceptance check AC6 (B03): upload-migration startup timeout.

`backend/app/lifespan.py` claims the upload migration runs "before accepting
requests". If that claim coexists with the migration being driven through
`asyncio.wait_for(asyncio.to_thread(migrate_uploads, ...), timeout=...)`, the
contradiction is live: `asyncio.to_thread` is NOT cancellable-synchronous, so
a timeout fires, the request loop proceeds, and the thread keeps
copying/renaming files concurrently with serving traffic.

The wait_for detection is AST-based (issue #692 review follow-up F-008): any
`asyncio.wait_for(...)` call wrapping an `asyncio.to_thread(...)` call whose
callee resolves to the name `migrate_uploads` fails the check regardless of
formatting, argument order, or keyword usage — a literal-substring probe was
shown to be evaded by a reformatted call.

This check is fix-agnostic: removing EITHER the uncancellable timeout wrap or
the "before accepting requests" claim makes it pass. Run from the repo root:

    python scripts/check_b03_upload_migration_timeout.py

Exit codes: 0 = OK, 1 = the contradiction is still present, 2 = cannot check.
"""

import ast
from pathlib import Path

CLAIM_SUBSTRING = "before accepting requests"

FAIL_LINE = (
    'FAIL: lifespan.py claims "before accepting requests" but uses '
    "wait_for(asyncio.to_thread(migrate_uploads ...))"
)


def _dotted_name(node: ast.AST) -> str:
    """Best-effort dotted name of an expression, '' when unresolved."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _wraps_to_thread_migrate_uploads(node: ast.AST) -> bool:
    """True when the subtree calls to_thread whose callee is migrate_uploads."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if _dotted_name(sub.func) not in ("asyncio.to_thread", "to_thread"):
            continue
        for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
            if isinstance(arg, (ast.Name, ast.Attribute)):
                if _dotted_name(arg).endswith("migrate_uploads"):
                    return True
    return False


def _find_uncancellable_wrap(tree: ast.AST) -> bool:
    """True when any wait_for(...) wraps a to_thread(migrate_uploads, ...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _dotted_name(node.func) not in ("asyncio.wait_for", "wait_for"):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _wraps_to_thread_migrate_uploads(arg):
                return True
    return False


def main() -> int:
    src_path = Path(__file__).resolve().parents[1] / "backend" / "app" / "lifespan.py"
    try:
        source = src_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"FAIL: could not read {src_path}: {exc}")
        return 2

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"FAIL: could not parse {src_path}: {exc}")
        return 2

    if CLAIM_SUBSTRING in source and _find_uncancellable_wrap(tree):
        print(FAIL_LINE)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
