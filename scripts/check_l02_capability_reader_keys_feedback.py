#!/usr/bin/env python3
"""Check Draft Room capability-limit reads against backend emitted keys."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from check_l02_typescript_ast import run_typescript_facts


def emitted_limit_keys(root: Path) -> set[str]:
    backend = root / "backend" / "app" / "api" / "routes" / "draft_room.py"
    tree = ast.parse(backend.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_capabilities":
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "DraftRoomCapabilities":
                    continue
                limits = next((kw.value for kw in call.keywords if kw.arg == "limits"), None)
                if isinstance(limits, ast.Dict):
                    return {key.value for key in limits.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise RuntimeError("could not locate get_capabilities limits literal")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        emitted = emitted_limit_keys(root)
        frontend = root / "frontend" / "src"
        files = sorted(path for suffix in ("*.ts", "*.tsx") for path in frontend.rglob(suffix) if ".test." not in path.name and ".spec." not in path.name)
        facts = run_typescript_facts(root, files)
        errors: list[str] = []
        for item in facts["files"]:
            relative = Path(item["file"]).relative_to(root).as_posix()
            if item["readerUnsupported"]:
                errors.append(f"UNSUPPORTED reader syntax: {relative}")
            for fact in item["readerKeys"]:
                if fact["key"] not in emitted:
                    errors.append(f"UNEMITTED {fact['key']}: {relative}:{fact['line']}")
    except (OSError, SyntaxError, RuntimeError, ValueError) as exc:
        print(f"capability-readers: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
