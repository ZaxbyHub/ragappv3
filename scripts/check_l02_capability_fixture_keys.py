#!/usr/bin/env python3
"""Check Draft Room test capability fixtures against the emitted limits keys.

Issue #773 AC7.  This is deliberately stdlib-only so it can run in the reduced
CI environment before frontend tests are collected.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "app" / "api" / "routes" / "draft_room.py"
FRONTEND_TESTS = ROOT / "frontend" / "src" / "components" / "draft-room"


def emitted_limit_keys() -> set[str]:
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "get_capabilities":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Name) and call.func.id == "DraftRoomCapabilities"):
                continue
            limits = next((kw.value for kw in call.keywords if kw.arg == "limits"), None)
            if isinstance(limits, ast.Dict):
                return {key.value for key in limits.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise RuntimeError("could not locate get_capabilities limits literal")


def fixture_keys(path: Path) -> list[tuple[str, int]]:
    text = path.read_text(encoding="utf-8")
    found: list[tuple[str, int]] = []
    for match in re.finditer(r"\blimits\s*:\s*\{(?P<body>[^{}]*)\}", text, re.DOTALL):
        line = text.count("\n", 0, match.start()) + 1
        for key_match in re.finditer(r"(?:['\"](?P<quoted>[A-Za-z_]\w*)['\"]|(?P<bare>[A-Za-z_]\w*))\s*:", match.group("body")):
            key = key_match.group("quoted") or key_match.group("bare")
            found.append((key, line + match.group("body")[: key_match.start()].count("\n")))
    return found


def main() -> int:
    try:
        emitted = emitted_limit_keys()
        errors: list[str] = []
        for path in sorted(FRONTEND_TESTS.glob("*.test.tsx")):
            for key, line in fixture_keys(path):
                if key not in emitted:
                    errors.append(f"FIXTURE-DRIFT {key}: {path.relative_to(ROOT).as_posix()}:{line}")
    except (OSError, SyntaxError, RuntimeError) as exc:
        print(f"capability-fixtures: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
