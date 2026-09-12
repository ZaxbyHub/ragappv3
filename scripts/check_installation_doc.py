#!/usr/bin/env python3
"""Validate INSTALLATION.md against the shipped application and pin table.

Issue #258 (DOC-001/002/003 + docs-validation AC): the installation guide
drifted from reality with no mechanical check. This script re-parses the doc
and validates every executable claim it makes:

1. Version mentions (Node majors, Python X.Y) must match the runtime
   contract's allowed set (ALLOWED_RUNTIME in scripts/check_runtime_contract.py
   — the single decision point; this script imports it, never re-declares it).
2. Every ``python -c "..."`` snippet's calls into ``app.*`` modules are
   arity-checked against the real function signatures by AST-parsing the
   backend source (no imports, no environment needed).
3. The compose recipe block must reference only services it defines (via
   depends_on or http:// URLs in environment entries) and every
   build context/dockerfile pair must exist in the repo.
4. Every CHAT_MODEL / INSTANT_CHAT_MODEL value the doc configures must appear
   in an ``ollama pull`` instruction in the same doc.

Stdlib-only on purpose: the CI Quality contracts job runs scripts/check_*.py
with a bare setup-python and no dependency install (same convention as
scripts/check_config_contract.py). The compose recipe is therefore parsed with
a structure-scoped line parser (documented shape: services/depends_on/
environment/build keys at fixed indentation). Full YAML *validity* of the
recipe is additionally asserted with PyYAML in
backend/tests/test_issue258_installation_doc.py, where PyYAML is a locked test
dependency — the two layers complement, not duplicate.

Functions are importable (backend/tests adds scripts/ to sys.path) so tests can
drive individual halves; ``main()`` runs everything.

Exit 0 = doc agrees with the app; exit 1 with one ``installation-doc:`` line
per finding on stderr. Run from anywhere (paths resolve from this file).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Sibling import: when run as a script, Python auto-prepends this file's
# directory to sys.path; when imported from the tests, scripts/ is already on
# sys.path. Either way this resolves without mutation.
from check_runtime_contract import ALLOWED_RUNTIME

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "INSTALLATION.md"

NODE_MAJOR = ALLOWED_RUNTIME["node"]["major"]
PYTHON_VERSION = ALLOWED_RUNTIME["python"]["version"]

# ── doc parsing helpers ─────────────────────────────────────────────────────

NODE_MAJOR_PATTERNS = (
    (re.compile(r"(?m)^\|\s*Node\.js\s*\|\s*(\d+)\+"), "prerequisite table"),
    (re.compile(r"\bnode@(\d+)\b"), "brew formula"),
    (re.compile(r"\bnode:(\d+)"), "image tag"),
    (re.compile(r"\bsetup_(\d+)\.x\b"), "nodesource setup script"),
    (re.compile(r"\((\d+)\.x or (\d+)\.x\)"), "LTS guidance"),
)

PYTHON_MENTION_RE = re.compile(r"(?i)\bpython[^\d\n]{0,4}(\d+\.\d+)")
PYTHON_TABLE_RE = re.compile(r"(?m)^\|\s*Python\s*\|\s*(\d+\.\d+)\+")

CHAT_MODEL_RE = re.compile(r"(?m)^\s*(?:-\s*)?(?:CHAT_MODEL|INSTANT_CHAT_MODEL)=(\S+)")
OLLAMA_PULL_RE = re.compile(r"\bollama pull (\S+)")

PYTHON_C_RE = re.compile(r'python(?:\d(?:\.\d+)?)?\s+-c\s+"([^"\n]+)"')


def read_doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def node_major_findings(doc: str) -> list[str]:
    """Node major mentions in the doc that disagree with the pin table."""
    findings: list[str] = []
    seen: set[tuple[int, str]] = set()
    for pattern, label in NODE_MAJOR_PATTERNS:
        for match in pattern.finditer(doc):
            for group in match.groups():
                if group is None:
                    continue
                key = (line_of(doc, match.start()), group)
                if key in seen:
                    continue
                seen.add(key)
                if group != NODE_MAJOR:
                    findings.append(
                        f"INSTALLATION.md:{key[0]}: Node mention {match.group(0).strip()!r} "
                        f"({label}) is major {group}, runtime contract requires {NODE_MAJOR}"
                    )
    return findings


def python_version_findings(doc: str) -> list[str]:
    findings: list[str] = []
    seen: set[tuple[int, str]] = set()
    for pattern in (PYTHON_TABLE_RE, PYTHON_MENTION_RE):
        for match in pattern.finditer(doc):
            key = (line_of(doc, match.start()), match.group(1))
            if key in seen:
                continue
            seen.add(key)
            if match.group(1) != PYTHON_VERSION:
                findings.append(
                    f"INSTALLATION.md:{key[0]}: Python mention "
                    f"{match.group(0).strip()!r} is {match.group(1)}, runtime "
                    f"contract requires {PYTHON_VERSION}"
                )
    return findings


# ── python -c snippet arity (DOC-002) ───────────────────────────────────────


def extract_python_c_snippets(doc: str) -> list[tuple[int, str]]:
    """Return (line, code) for every ``python -c "..."`` snippet."""
    return [(line_of(doc, m.start()), m.group(1)) for m in PYTHON_C_RE.finditer(doc)]


def _backend_module_path(module: str) -> Path | None:
    relative = module.replace(".", "/")
    for candidate in (ROOT / "backend" / f"{relative}.py", ROOT / "backend" / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _signature(module_path: Path, function: str) -> ast.FunctionDef | None:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return node
    return None


def _required_positional(func_def: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count parameters a caller must supply (positional-or-keyword, no default)."""
    params = func_def.args.posonlyargs + func_def.args.args
    # ast aligns defaults to the tail of params.
    first_optional = len(params) - len(func_def.args.defaults)
    required = 0
    for index, arg in enumerate(params):
        if index == 0 and arg.arg in ("self", "cls") and not func_def.args.posonlyargs:
            continue
        if index < first_optional:
            required += 1
    return required


def snippet_arity_findings(doc: str) -> list[str]:
    """AST-validate every python -c call into app.* against the real signature."""
    findings: list[str] = []
    for line_no, code in extract_python_c_snippets(doc):
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            findings.append(
                f"INSTALLATION.md:{line_no}: python -c snippet does not parse: {exc}"
            )
            continue
        imports: dict[str, tuple[str, str]] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                for alias in node.names:
                    imports[alias.asname or alias.name] = (node.module, alias.name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in imports:
                continue
            module, function = imports[node.func.id]
            module_path = _backend_module_path(module)
            if module_path is None:
                findings.append(
                    f"INSTALLATION.md:{line_no}: snippet imports {module} but no such "
                    "backend module exists"
                )
                continue
            func_def = _signature(module_path, function)
            if func_def is None:
                findings.append(
                    f"INSTALLATION.md:{line_no}: snippet imports {function} from {module} "
                    f"but {module_path.name} defines no such function"
                )
                continue
            required = _required_positional(func_def)
            positional = len(node.args)
            keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
            accepted = {a.arg for a in func_def.args.posonlyargs + func_def.args.args}
            accepted |= {a.arg for a in func_def.args.kwonlyargs}
            supplied = positional + len(keywords & accepted)
            if supplied < required:
                findings.append(
                    f"INSTALLATION.md:{line_no}: snippet calls {function}() with "
                    f"{positional} argument(s) but {module}.{function} requires "
                    f"{required} ({', '.join(sorted(accepted)) or 'no parameters'})"
                )
            unknown = keywords - accepted
            if unknown:
                findings.append(
                    f"INSTALLATION.md:{line_no}: snippet passes unknown keyword "
                    f"argument(s) {sorted(unknown)} to {module}.{function}"
                )
    return findings


# ── compose recipe (DOC-003) ────────────────────────────────────────────────

FENCED_YAML_RE = re.compile(r"(?ms)^```yaml\n(.*?)^```")
SERVICE_RE = re.compile(r"(?m)^  ([A-Za-z0-9_-]+):\s*$")
CONTEXT_RE = re.compile(r"(?m)^(\s+)context:\s*(\S+)\s*$")
DOCKERFILE_RE = re.compile(r"(?m)^(\s+)dockerfile:\s*(\S+)\s*$")
ENV_LIST_RE = re.compile(r"(?m)^\s+- ([A-Z][A-Z0-9_]+)=(.*)$")
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
NON_SERVICE_HOSTS = {"localhost", "127.0.0.1"}


def extract_compose_recipe(doc: str) -> str | None:
    """The fenced ```yaml block that defines services:, if any."""
    for match in FENCED_YAML_RE.finditer(doc):
        if re.search(r"(?m)^services:", match.group(1)):
            return match.group(1)
    return None


def compose_services(recipe: str) -> list[str]:
    """Service names under the top-level ``services:`` key (2-space indent).

    Scoped to the services section so top-level keys of *other* sections
    (volumes:, networks:) are not mistaken for services.
    """
    services: list[str] = []
    in_services = False
    for line in recipe.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if not in_services:
            continue
        if re.match(r"^[A-Za-z0-9_-]+:\s*$", line):
            break  # next top-level key (volumes:, networks:, ...) ends the section
        service = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if service:
            services.append(service.group(1))
    return services


def _indented_list_after(recipe: str, key: str) -> list[str]:
    """Items of the bullet list directly under a ``key:`` line."""
    items: list[str] = []
    lines = recipe.splitlines()
    for idx, line in enumerate(lines):
        key_match = re.match(rf"^(\s*){re.escape(key)}:\s*$", line)
        if not key_match:
            continue
        base_indent = len(key_match.group(1))
        for follow in lines[idx + 1 :]:
            item = re.match(r"^(\s+)- (\S+)\s*$", follow)
            if item and len(item.group(1)) > base_indent:
                items.append(item.group(2))
                continue
            if follow.strip() and not item:
                break
        break
    return items


def compose_env_entries(recipe: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in ENV_LIST_RE.finditer(recipe)]


def compose_build_path_findings(recipe: str) -> list[str]:
    """Every build context/dockerfile pair in the recipe must exist in the repo."""
    findings: list[str] = []
    current_context: str | None = None
    context_indent = 0
    for line in recipe.splitlines():
        ctx = CONTEXT_RE.match(line)
        if ctx:
            current_context = ctx.group(2)
            context_indent = len(ctx.group(1))
            continue
        df = DOCKERFILE_RE.match(line)
        if df and current_context is not None and len(df.group(1)) == context_indent:
            resolved = (ROOT / current_context) / df.group(2)
            if not resolved.is_file():
                findings.append(
                    f"INSTALLATION.md: compose recipe builds {current_context}/{df.group(2)} "
                    f"but {resolved.relative_to(ROOT).as_posix()} does not exist"
                )
            current_context = None
    return findings


def compose_findings(doc: str) -> list[str]:
    recipe = extract_compose_recipe(doc)
    if recipe is None:
        return ["INSTALLATION.md: no fenced ```yaml compose recipe defining services: found"]
    findings: list[str] = []
    services = set(compose_services(recipe))
    for target in _indented_list_after(recipe, "depends_on"):
        if target not in services:
            findings.append(
                f"INSTALLATION.md: compose recipe depends_on references undefined "
                f"service {target!r} (defined: {sorted(services)})"
            )
    for name, value in compose_env_entries(recipe):
        for host in URL_RE.findall(value):
            if host in NON_SERVICE_HOSTS or host.startswith("${"):
                continue
            if host not in services:
                findings.append(
                    f"INSTALLATION.md: compose recipe {name} targets http host "
                    f"{host!r} which is not a defined service (defined: {sorted(services)})"
                )
    findings.extend(compose_build_path_findings(recipe))
    return findings


# ── model pulls (DOC-003b) ──────────────────────────────────────────────────


def model_pull_findings(doc: str) -> list[str]:
    pulled = set(OLLAMA_PULL_RE.findall(doc))
    findings: list[str] = []
    for match in CHAT_MODEL_RE.finditer(doc):
        model = match.group(1)
        if model.startswith("${"):
            continue
        if model not in pulled:
            findings.append(
                f"INSTALLATION.md:{line_of(doc, match.start())}: configured model "
                f"{model!r} is never pulled by any `ollama pull` instruction "
                f"(pulled: {sorted(pulled)})"
            )
    return findings


def main() -> int:
    doc = read_doc()
    failures = (
        node_major_findings(doc)
        + python_version_findings(doc)
        + snippet_arity_findings(doc)
        + compose_findings(doc)
        + model_pull_findings(doc)
    )
    for message in failures:
        print(f"installation-doc: {message}", file=sys.stderr)
    if failures:
        return 1
    print("installation-doc: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
