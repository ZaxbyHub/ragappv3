#!/usr/bin/env python3
"""Settings consumer census: every declared Settings field needs a production read site.

Issue #662 — kills the #614 "declared-but-unwired settings" class: a field that is
declared, typed, defaulted, and documented but never read by production code fails
CI at authoring time (allowlisted fields with a written reason excepted).

The census is AST-based (stdlib only, CI's requirements-ci world):
  * Fields are enumerated pydantic-faithfully from backend/app/config.py — direct
    AnnAssign declarations in the Settings class body, excluding ClassVar
    annotations, underscore-prefixed names, and plain assignments such as
    model_config = SettingsConfigDict(...). Parity with Settings.model_fields is
    pinned by backend/tests/test_settings_consumers_gate.py.
  * A consumer is a Load-context read through any of the four binding forms used
    by this codebase (verified exhaustive at the time of the census):
      1. attribute read on an alias of the settings singleton at any lexical
         scope (`from app.config import settings [as x]`, or a rebinding
         `x = get_settings()` / `x = Settings()`),
      2. getattr(<alias>, "field", ...) with a constant string (multi-line
         calls included — the contextual_chunking.py shape that regex censuses
         miss),
      3. <expr>.settings.<field> attribute chains (self.settings.x,
         email_service.settings.x) — only in files that themselves import the
         settings singleton / Settings class / call get_settings, so an
         unrelated object named `.settings` in a settings-agnostic module
         cannot mint a consumer,
      4. params annotated `: Settings` (FastAPI DI: `dep: Settings = Depends(
         get_settings)`).
  * Not consumption: whole-model serialization (model_dump etc. never names
    individual fields), field validators inside config.py (the file is excluded
    from the consumer scope), tests, and this check script itself.
  * Only reads count: a field name in a comment, a string literal, or a
    Store-context assignment is not a consumer.

Residual risks (documented in docs/releases/pending/662-*.md):
  (A) a settings-importing file holding a non-Settings `.settings` object is
      counted as a consumer (masks dormancy) — review-time detection only;
  (B) a settings-agnostic file whose object holds a duck-typed Settings via an
      unannotated parameter loses its chain reads (false dormancy) — the
      allowlist is the remedy and T1 fails loud on any live field caught;
  (C) alias/typed-param bindings are file-scoped: a same-file name collision
      with a settings alias or Settings-typed param can mint a false consumer;
  (D) annotation shapes beyond a bare `Settings` name (`Optional[Settings]`,
      `Annotated[Settings, ...]`, string forward refs) are not detected as
      Settings-typed — fail-closed (the gate flags, the allowlist remedies);
  (E) the scan scope is `backend/app` + repo-root `scripts/` only; dev
      tooling under `backend/scripts/` deliberately does not count as a
      production read site.

Exit codes: 0 = green; 1 = dormant fields or allowlist contract violations;
2 = usage/IO errors (unreadable or unparseable config/consumer source,
missing Settings class, unreadable allowlist).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

SCRIPT_NAME = "check_settings_consumers.py"
CONFIG_REL = Path("backend/app/config.py")
SCAN_DIRS = (Path("backend/app"), Path("scripts"))
SINGLETON_NAMES = {"get_settings", "Settings"}


def enumerate_settings_fields(config_path: Path) -> list[str]:
    """Return Settings field names, mirroring pydantic's model_fields semantics."""
    try:
        tree = ast.parse(config_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError, RecursionError) as exc:
        # A config file we cannot read/parse is an IO/usage failure (exit 2),
        # never a dormant-field finding (exit 1) — keep the two contracts apart.
        print(f"settings-consumers: cannot read or parse {config_path.as_posix()}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            fields: list[str] = []
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                    continue
                name = stmt.target.id
                if name.startswith("_"):
                    continue
                annotation = stmt.annotation
                if isinstance(annotation, ast.Subscript) and getattr(annotation.value, "id", "") == "ClassVar":
                    continue
                fields.append(name)
            # Duplicate names collapse to one entry (matching pydantic's
            # last-wins field semantics; duplicate names are identical strings
            # so first-position retention is unobservable).
            return list(dict.fromkeys(fields))
    print(f"settings-consumers: class Settings not found in {config_path.as_posix()}", file=sys.stderr)
    raise SystemExit(2)


class FileCensus:
    """Alias/binding facts collected for one scanned file."""

    def __init__(self) -> None:  # noqa: D107 - simple container
        self.aliases: set[str] = set()  # names bound to the settings singleton
        self.typed: set[str] = set()  # names annotated : Settings
        self.singleton_callables: set[str] = set()  # imported get_settings/Settings names
        self.settings_touched = False  # file imports/calls anything settings-related


def _annotation_names(node: ast.expr | None) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    return set()


def analyze_file(tree: ast.Module, settings_class_names: set[str]) -> FileCensus:
    """Collect one file's binding facts. settings_class_names seeds with 'Settings' plus
    this file's own import aliases of the class (per-file; no cross-file contamination)."""
    facts = FileCensus()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # app.config absolute, plus relative forms (`from ..config import settings`)
            is_config_module = (
                module == "app.config"
                or module.endswith(".config")
                or ((node.level or 0) > 0 and module == "config")
            )
            if is_config_module:
                for alias in node.names:
                    if alias.name == "settings":
                        facts.aliases.add(alias.asname or "settings")
                        facts.settings_touched = True
                    elif alias.name == "Settings":
                        settings_class_names.add(alias.asname or "Settings")
                        facts.settings_touched = True
            for alias in node.names:
                if alias.name in SINGLETON_NAMES:
                    # Track the imported (possibly aliased) name so a rebind
                    # `x = <singleton>()` only counts when the callable traces
                    # to a real import — a local function merely named
                    # get_settings/Settings must not mint consumers.
                    facts.singleton_callables.add(alias.asname or alias.name)
                    facts.settings_touched = True
        elif isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = node.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in facts.singleton_callables
                ):
                    facts.aliases.add(node.targets[0].id)
                    facts.settings_touched = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if _annotation_names(arg.annotation) & settings_class_names:
                    facts.typed.add(arg.arg)
                    facts.settings_touched = True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and _annotation_names(node.annotation) & settings_class_names:
                facts.typed.add(node.target.id)
                facts.settings_touched = True
    return facts


def consumer_reads(tree: ast.Module, facts: FileCensus, fields: set[str], consumers: dict[str, str], rel: str) -> None:
    bases = facts.aliases | facts.typed

    def is_base(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in bases
        if isinstance(node, ast.Attribute) and node.attr == "settings":
            return facts.settings_touched  # chain form, guarded per file
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Load) and node.attr in fields and is_base(node.value):
                consumers.setdefault(node.attr, f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                args = node.args
                if (
                    len(args) >= 2
                    and is_base(args[0])
                    and isinstance(args[1], ast.Constant)
                    and isinstance(args[1].value, str)
                    and args[1].value in fields
                ):
                    consumers.setdefault(args[1].value, f"{rel}:{node.lineno}")


def parse_allowlist(path: Path, fields: set[str]) -> tuple[set[str], list[str]]:
    """Return (allowed fields, violations). Entry: `field :: reason [:: owner-hint]`."""
    allowed: set[str] = set()
    violations: list[str] = []
    try:
        text = path.read_text(encoding="utf-8-sig")  # utf-8-sig: tolerate a BOM from Windows editors
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError (non-UTF-8 bytes) — same
        # usage/IO-error contract as the config/scan-source paths.
        print(f"settings-consumers: cannot read allowlist {path.as_posix()}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("::")]
        field = parts[0] if parts else ""
        reason = parts[1] if len(parts) > 1 else ""
        if not reason:
            violations.append(f"{path}:{lineno}: allowlist entry for '{field}' has no reason")
            continue
        if field not in fields:
            violations.append(f"{path}:{lineno}: allowlist entry '{field}' is not a Settings field")
            continue
        allowed.add(field)
    return allowed, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repo-style root to census (fixtures pass a miniature tree)")
    parser.add_argument("--allowlist", default=None, help="allowlist path (default <root>/scripts/settings_dormant_allowlist.txt; absent file = empty)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = root / CONFIG_REL
    if not config_path.is_file():
        print(f"settings-consumers: {config_path.as_posix()} not found under root {root}", file=sys.stderr)
        return 2

    fields = enumerate_settings_fields(config_path)
    field_set = set(fields)

    allowlist_path = Path(args.allowlist).resolve() if args.allowlist else root / "scripts" / "settings_dormant_allowlist.txt"
    if allowlist_path.is_file():
        allowed, violations = parse_allowlist(allowlist_path, field_set)
        if violations:
            for violation in violations:
                print(f"settings-consumers: {violation}", file=sys.stderr)
            return 1
    else:
        allowed = set()

    consumers: dict[str, str] = {}
    for scan_dir in SCAN_DIRS:
        directory = root / scan_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == SCRIPT_NAME:
                continue
            if path.resolve() == config_path.resolve():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError, RecursionError) as exc:
                print(f"settings-consumers: cannot read or parse {path.as_posix()}: {exc}", file=sys.stderr)
                return 2
            rel = path.relative_to(root).as_posix()
            facts = analyze_file(tree, {"Settings"})
            if facts.aliases or facts.typed or facts.settings_touched:
                consumer_reads(tree, facts, field_set, consumers, rel)

    dormant = sorted(field for field in fields if field not in consumers and field not in allowed)
    if dormant:
        for field in dormant:
            print(f"DORMANT: {field}")
        print(
            f"settings-consumers: {len(dormant)} field(s) without production consumers "
            f"(declare a consumer, or add a reasoned entry to {allowlist_path.relative_to(root).as_posix() if allowlist_path.is_relative_to(root) else allowlist_path.as_posix()})",
            file=sys.stderr,
        )
        return 1
    print(
        f"settings-consumers: OK ({len(fields)} fields, all consumed or allowlisted; "
        f"{len(allowed)} allowlisted)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
