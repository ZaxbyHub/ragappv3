"""Issue #566 guardrail — Vite native config-loader compatibility of config files.

Vite 8 loads ``frontend/vite.config.ts`` (and transitively ``vite.paths.ts``)
through its native ESM config loader BY DEFAULT. That loader warns — and a
future major is planned to reject — two classes of construct in config-loaded
files:

* CommonJS module-scope globals (``__dirname`` / ``__filename`` / ``require``),
  which do not exist under native ESM execution;
* extensionless relative imports (``./vite.paths``), which native ESM
  resolution cannot load.

Issue #566 fixed both (``import.meta.dirname`` at the alias and the explicit
``./vite.paths.ts`` extension at the import). This module pins that shape the
same way ``test_issue258_coverage_design.py`` pins the coverage-gate design:
a contract over the config-loaded files that fails loudly if either trigger
is reintroduced.

Detection is AST-based via the TypeScript compiler (shelling out to ``node``
with the frontend's own installed ``typescript`` package). Ground-truth
parsing is deliberate: regex/token hand-lexing went through four adversarial
review rounds on #624 (comment token-separation, token joining, ``//`` inside
quoted specifiers, ``//`` inside regex literals) and each fix sprouted a new
hole; the compiler has none of them. Out of class by design: ``__dirname`` in
Vitest TEST files (they run through the transform pipeline's bundler
resolution, not the native config loader).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# The files Vite's native config loader executes. vite.paths.ts is loaded
# transitively via the config's relative import.
CONFIG_LOADED_FILES = ("vite.config.ts", "vite.paths.ts")

# A trailing segment that is a known script/data extension. './vite.paths'
# fails this (its dot is part of the name); './vite.paths.ts' passes.
_EXTENSION_RE = re.compile(r"\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs|json)$")

_NODE = shutil.which("node")

# Walks a TypeScript source file and reports every module specifier reachable
# by the native config loader (static import/export declarations and literal
# dynamic import()), plus any CommonJS-only global reference, as identifiers.
_AST_WALKER_JS = r"""
const ts = require('typescript');
const fs = require('fs');
// `node -e <script> <file>` places <file> at the END of process.argv
// regardless of how the runtime seeds argv[1].
const file = process.argv[process.argv.length - 1];
const sf = ts.createSourceFile(
  file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS
);
const out = { specifiers: [], cjsGlobals: [], nonLiteralDynamic: 0 };
function walk(node) {
  if (
    (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
    node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)
  ) {
    out.specifiers.push(node.moduleSpecifier.text);
  }
  if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
    const arg = node.arguments[0];
    if (arg && ts.isStringLiteral(arg)) out.specifiers.push(arg.text);
    else out.nonLiteralDynamic += 1;
  }
  if (ts.isIdentifier(node) && (node.text === '__dirname' || node.text === '__filename' || node.text === 'require')) {
    // any reference to the name — including aliasing like `const r = require` —
    // is in class, not just direct require() call expressions (#624 critic r6)
    out.cjsGlobals.push(node.text);
  }
  ts.forEachChild(node, walk);
}
walk(sf);
console.log(JSON.stringify(out));
"""


def _ast_facts(name: str) -> dict:
    """(specifiers, cjsGlobals, nonLiteralDynamic) for one config file,
    parsed by the real TypeScript compiler."""
    proc = subprocess.run(
        [_NODE, "-e", _AST_WALKER_JS, str(FRONTEND / name)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"566 GUARDRAIL CHECK: FAIL — TypeScript walker failed on {name}: "
        f"{proc.stderr.strip()}"
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(
    _NODE is None,
    reason="node not available (needed for the TypeScript parser); the "
    "Backend CI runner has node preinstalled, as does any frontend dev box",
)
@pytest.mark.parametrize("name", CONFIG_LOADED_FILES)
def test_config_files_have_no_cjs_only_globals(name: str) -> None:
    """CJS globals are shimmed+warned today and rejected by the planned
    native default — none may appear in a config-loaded file (as real
    identifiers, comments excluded by the parser)."""
    facts = _ast_facts(name)
    assert not facts["cjsGlobals"], (
        f"566 GUARDRAIL CHECK: FAIL — {name} references the CommonJS-only "
        f"{facts['cjsGlobals']}; Vite's native config loader does not "
        "support them (use import.meta.dirname / ESM imports)"
    )
    assert facts["nonLiteralDynamic"] == 0, (
        f"566 GUARDRAIL CHECK: FAIL — {name} contains a non-literal dynamic "
        "import() whose specifier the guardrail cannot statically verify; "
        "use a literal specifier or a static import"
    )
    print(f"566 GUARDRAIL CHECK: PASS — {name} free of CJS-only globals")


@pytest.mark.skipif(
    _NODE is None,
    reason="node not available (needed for the TypeScript parser); the "
    "Backend CI runner has node preinstalled, as does any frontend dev box",
)
@pytest.mark.parametrize("name", CONFIG_LOADED_FILES)
def test_config_files_relative_imports_have_extensions(name: str) -> None:
    """Native ESM resolution requires explicit file extensions on relative
    imports; an extensionless './vite.paths' style import warns today.
    Comments, regex literals, and template holes cannot hide a specifier:
    the compiler sees through all of them."""
    facts = _ast_facts(name)
    assert facts["nonLiteralDynamic"] == 0, (
        f"566 GUARDRAIL CHECK: FAIL — {name} contains a non-literal dynamic "
        "import() whose specifier the guardrail cannot statically verify; "
        "use a literal specifier or a static import"
    )
    for spec in facts["specifiers"]:
        if not spec.startswith("."):
            continue  # bare package specifier — unaffected by ESM resolution
        assert _EXTENSION_RE.search(spec), (
            f"566 GUARDRAIL CHECK: FAIL — {name} imports {spec!r} without a "
            "file extension; Vite's native config loader cannot resolve it"
        )
    print(f"566 GUARDRAIL CHECK: PASS — {name} relative imports carry extensions")


@pytest.mark.skipif(
    _NODE is None,
    reason="node not available (needed for the TypeScript parser)",
)
def test_ast_walker_bites_on_synthetic_evasion(tmp_path: Path) -> None:
    """The walker itself must not be fooled by the shapes that defeated the
    earlier regex detectors (#624 review rounds 2-4): commented imports,
    token-joining comments, ``//`` inside quoted specifiers, and ``//``
    inside regex literals followed by a real dynamic import."""
    nasty = tmp_path / "synthetic.config.ts"
    synthetic = nasty
    synthetic.write_text(
        "// import './commented.out'\n"
        "/* import './block-commented.out' */\n"
        "const re = /[//]/.test(value); const p = import('./vite//paths');\n"
        "import/*c*/'./vite.paths';\n"
        "import x/*c*/from/*c*/'./vite.paths';\n"
        "import /* mid */ ('./vite.paths');\n"
        "const ok = import('./vite.paths.ts');\n"
        "const dynamic = import(unevaluated);\n"
        "const aliasedRequire = require;\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_NODE, "-e", _AST_WALKER_JS, str(synthetic)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    facts = json.loads(proc.stdout)
    assert facts["cjsGlobals"] == ["require"], (
        "walker must flag an aliased require reference, not just direct calls"
    )
    # every extensionless specifier in the nasty file is still collected —
    # commented ones are (correctly) absent, all real ones are present
    assert sorted(facts["specifiers"]) == [
        "./vite.paths",
        "./vite.paths",
        "./vite.paths",
        "./vite.paths.ts",
        "./vite//paths",
    ], facts["specifiers"]
    extensionless = [s for s in facts["specifiers"] if not _EXTENSION_RE.search(s)]
    assert extensionless, "walker found no extensionless specifiers to flag"
    assert facts["nonLiteralDynamic"] == 1, (
        "walker must count non-literal dynamic imports so the parametrized "
        "checks can fail on statically unverifiable specifiers"
    )
    print(
        "566 GUARDRAIL CHECK: PASS — walker caught all "
        f"{len(extensionless)} extensionless imports through comments, "
        "regex literals, and quoted-// specifiers"
    )
