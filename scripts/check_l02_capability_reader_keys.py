#!/usr/bin/env python3
"""Check frontend Draft Room capability reads against backend emitted keys."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "app" / "api" / "routes" / "draft_room.py"
FRONTEND = ROOT / "frontend" / "src"


def emitted_limit_keys() -> set[str]:
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_capabilities":
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "DraftRoomCapabilities":
                    continue
                limits = next((kw.value for kw in call.keywords if kw.arg == "limits"), None)
                if isinstance(limits, ast.Dict):
                    return {key.value for key in limits.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise RuntimeError("could not locate get_capabilities limits literal")


def main() -> int:
    try:
        emitted = emitted_limit_keys()
        errors: list[str] = []
        pattern = re.compile(r"\blimits\?(?:\.)?\.([A-Za-z_]\w*)|\blimits\.([A-Za-z_]\w*)")
        for path in sorted((*FRONTEND.rglob("*.ts"), *FRONTEND.rglob("*.tsx"))):
            if ".test." in path.name or ".spec." in path.name:
                continue
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                key = match.group(1) or match.group(2)
                if key not in emitted:
                    line = text.count("\n", 0, match.start()) + 1
                    errors.append(f"UNEMITTED {key}: {path.relative_to(ROOT).as_posix()}:{line}")
    except (OSError, SyntaxError, RuntimeError) as exc:
        print(f"capability-readers: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
