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
a source-inspection contract that fails loudly if either trigger is
reintroduced into a config-loaded file, independent of whether a build
happened to print a warning anyone read.

Out of class by design: ``__dirname`` in Vitest TEST files (they run through
the transform pipeline's bundler resolution, not the native config loader).
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# The files Vite's native config loader executes. vite.paths.ts is loaded
# transitively via the config's relative import.
CONFIG_LOADED_FILES = ("vite.config.ts", "vite.paths.ts")

CJS_GLOBAL_RE = re.compile(r"\b(?:__dirname|__filename)\b|\brequire\s*\(")

# Relative imports must carry a real module extension: './x.ts' (or .js,
# .mjs, ...), never './x' and never a bare './vite.paths' whose dot belongs
# to the file name. Bare specifiers (package imports) are unaffected.
# Covers all three ESM import shapes the native config loader resolves:
# `from './x'`, side-effect `import './x'`, and dynamic `import('./x')`
# (PRR-003: a side-effect or dynamic extensionless form would otherwise
# evade the guardrail).
EXTENSIONLESS_RELATIVE_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+)['"](\.[^'"]*)['"]"""
)

# A trailing segment that is a known script/data extension. './vite.paths'
# fails this (its dot is part of the name); './vite.paths.ts' passes.
_EXTENSION_RE = re.compile(r"\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs|json)$")


def _config_text(name: str) -> str:
    path = FRONTEND / name
    assert path.is_file(), f"config-loaded file {name} is missing"
    return _strip_js_comments(path.read_text(encoding="utf-8"))


def _strip_js_comments(text: str) -> str:
    """Remove block and line comments so comment placement cannot hide an
    import from the detector (JS permits comments between any two tokens,
    e.g. `import /* x */ './y'`). Comments are replaced with a SPACE, not
    removed: a comment is itself a token separator, so substituting it with
    a space preserves token boundaries and no legal import form can be
    hidden (final-critic round 3 on #624: empty-string substitution joined
    `import/*c*/'./x'` into an undetectable blob). Worst case the scan
    over-flags, which fails safe for a guardrail."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


@pytest.mark.parametrize("name", CONFIG_LOADED_FILES)
def test_config_files_have_no_cjs_only_globals(name: str) -> None:
    """CJS globals are shimmed+warned today and rejected by the planned
    native default — none may appear in a config-loaded file."""
    text = _config_text(name)
    match = CJS_GLOBAL_RE.search(text)
    assert match is None, (
        f"566 GUARDRAIL CHECK: FAIL — {name} uses the CommonJS-only global "
        f"{match.group(0)!r} at offset {match.start()}; Vite's native config "
        "loader does not support it (use import.meta.dirname / ESM imports)"
    )
    print(f"566 GUARDRAIL CHECK: PASS — {name} free of CJS-only globals")


@pytest.mark.parametrize("name", CONFIG_LOADED_FILES)
def test_config_files_relative_imports_have_extensions(name: str) -> None:
    """Native ESM resolution requires explicit file extensions on relative
    imports; an extensionless './vite.paths' style import warns today."""
    text = _config_text(name)
    for match in EXTENSIONLESS_RELATIVE_IMPORT_RE.finditer(text):
        spec = match.group(1)
        assert _EXTENSION_RE.search(spec), (
            f"566 GUARDRAIL CHECK: FAIL — {name} imports {spec!r} without a "
            "file extension; Vite's native config loader cannot resolve it"
        )
    print(f"566 GUARDRAIL CHECK: PASS — {name} relative imports carry extensions")


def test_import_detector_catches_commented_forms() -> None:
    """The detector must not be evaded by comments between import tokens
    (final-critic Round 2 on #624): side-effect, dynamic, and from shapes
    with block comments interleaved must all still be flagged."""
    for snippet in (
        "import /* c */ './vite.paths'",
        "import /* c */ ('./vite.paths')",
        "import x /* c */ from /* c */ './vite.paths'",
        "const m = await import /* c */ ('./vite.paths')",
        "import // line comment\n  './vite.paths'",
        # compact forms: the comment is the ONLY token separator
        "import/*c*/'./vite.paths'",
        "import x/*c*/from/*c*/'./vite.paths'",
    ):
        match = EXTENSIONLESS_RELATIVE_IMPORT_RE.search(_strip_js_comments(snippet))
        assert match is not None, (
            f"566 GUARDRAIL CHECK: FAIL — detector missed a commented "
            f"extensionless import: {snippet!r}"
        )
    print("566 GUARDRAIL CHECK: PASS — commented import forms still detected")
