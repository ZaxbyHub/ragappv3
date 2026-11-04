"""Issue #258 acceptance check — frontend coverage gate DESIGN pin (AC11 / ENH-006).

The companion gate ``test_issue258_coverage_gate.py`` proves a coverage
configuration with thresholds exists and that the command passes. That alone
cannot prevent the gate from being quietly hollowed out later (include
re-scoped to one trivial module, thresholds zeroed). This check pins the
SUBSTANTIVE design decisions Phase 4 made (see
``.agents/issue-traces/258-ci-integration-tests-installation/07-approved-plan.md``,
G5, plan-critic item 5):

* the coverage ``include`` must contain BOTH meaningful-contract scopes —
  the per-domain HTTP client modules (``src/lib/api``) and the Zustand
  stores (``src/stores``); and
* every configured threshold (lines/branches/functions/statements) must be
  at or above the documented floor of 50, so the gate cannot be zeroed.

The measured baseline and the minus-~2pt threshold rationale are recorded as
a comment in the coverage block itself; this file only enforces the floor.

#746 AC7 (TQ-sibling-batch-04-02): extraction is now TypeScript-compiler
AST-based (the #624 fix pattern — see
test_issue566_config_loader_native.py). The previous regex/bracket
extraction was comment-blind: an include entry or threshold that existed
only inside a comment still satisfied the pin, which is exactly the class
the sibling guard's history documents (comment token-separation, ``//``
inside quoted specifiers, ``//`` inside regex literals). Comments never
reach the compiler's AST, so a commented-out scope or threshold now fails
the pin. Dependency resolution: the frontend's own ``typescript`` package
when ``frontend/node_modules`` exists, CI's NODE_PATH install otherwise, or
a LOUD skip naming the venue — never a silent green.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"

_VITEST_CONFIG_CANDIDATES = (
    "vitest.config.ts",
    "vitest.config.mts",
    "vitest.config.js",
    "vite.config.ts",
)

# Documented floor: prevents thresholds from being zeroed while still leaving
# Phase 4 free to tune the margin around the measured baseline.
_THRESHOLD_FLOOR = 50

_NODE = shutil.which("node")

# Walks a config file with the REAL TypeScript compiler and reports the
# coverage block's include array (string literals only — comments never
# appear) and thresholds object (numeric literal properties only).
_COVERAGE_WALKER_JS = r"""
const ts = require('typescript');
const fs = require('fs');
const file = process.argv[process.argv.length - 1];
const sf = ts.createSourceFile(
  file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS
);
const out = { include: [], thresholds: {}, hasCoverage: false, hasThresholds: false };
function isKey(node, name) {
  return node.name && ts.isIdentifier(node.name) && node.name.text === name;
}
function walk(node) {
  if (ts.isPropertyAssignment(node) && isKey(node, 'coverage')) {
    out.hasCoverage = true;
    ts.forEachChild(node.initializer, function (covChild) {
      if (ts.isPropertyAssignment(covChild) && isKey(covChild, 'include')) {
        if (ts.isArrayLiteralExpression(covChild.initializer)) {
          covChild.initializer.elements.forEach(function (el) {
            if (ts.isStringLiteral(el)) out.include.push(el.text);
          });
        }
      }
      if (ts.isPropertyAssignment(covChild) && isKey(covChild, 'thresholds')) {
        out.hasThresholds = true;
        if (ts.isObjectLiteralExpression(covChild.initializer)) {
          covChild.initializer.properties.forEach(function (prop) {
            if (ts.isPropertyAssignment(prop) && prop.initializer &&
                ts.isNumericLiteral(prop.initializer) && prop.name) {
              out.thresholds[prop.name.text] = Number(prop.initializer.text);
            }
          });
        }
      }
    });
  }
  ts.forEachChild(node, walk);
}
walk(sf);
console.log(JSON.stringify(out));
"""


def _coverage_ast() -> tuple[str, dict] | None:
    """(filename, walker facts) for the first config declaring a coverage
    block, parsed by the real TypeScript compiler. Skips LOUDLY when
    node or typescript is unresolvable (bare local env); Backend CI
    provisions typescript via NODE_PATH (ci.yml) and any frontend dev box
    has node_modules. The guard lives HERE (not only on the design tests'
    skipif markers) because test_issue258_coverage_gate.py imports this
    helper without its own marker (Copilot round-1 note)."""
    if _NODE is None:
        pytest.skip(
            "node is not available — the TypeScript parser cannot run; the "
            "coverage design pin does NOT silently pass without the parser"
        )
    for name in _VITEST_CONFIG_CANDIDATES:
        path = FRONTEND / name
        if not path.is_file():
            continue
        proc = subprocess.run(
            [_NODE, "-e", _COVERAGE_WALKER_JS, str(path)],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0 and "Cannot find module 'typescript'" in proc.stderr:
            pytest.skip(
                "typescript is not resolvable from frontend/ (no "
                "frontend/node_modules and no NODE_PATH install) — run "
                "`npm install` in frontend/ or set NODE_PATH (Backend CI "
                "provisions it outside the tree; see ci.yml). The coverage "
                "design pin does NOT silently pass without the parser."
            )
        if proc.returncode != 0:
            # pytest.fail (Failed: BaseException), NOT assert: a walker crash
            # is a HARNESS error, not design evidence. The h09 AC7 probe only
            # counts AssertionError from the mutated config as proof; a
            # crashing compiler must land in its proof_absent arm and FAIL
            # the probe (reviewer Round 4 Finding 1 falsifier: a fake node
            # exiting 2 previously left the probe green with the parser never
            # running).
            pytest.fail(
                f"DESIGN CHECK: FAIL — TypeScript walker failed on {name}: "
                f"{proc.stderr.strip()}"
            )
        facts = json.loads(proc.stdout)
        if facts["hasCoverage"]:
            return name, facts
    return None


@pytest.mark.skipif(
    _NODE is None,
    reason="node not available (needed for the TypeScript parser); the "
    "Backend CI runner has node preinstalled, as does any frontend dev box",
)
def test_ac11_design_coverage_include_scopes_both_contracts():
    try:
        evidence = _coverage_ast()
        print(f"coverage config: {evidence[0] if evidence else None}")
        assert evidence is not None, (
            "no vitest/vite config declares a coverage block — the ENH-006 "
            "gate would not exist (see test_issue258_coverage_gate.py)"
        )
        name, facts = evidence

        print(
            "DESIGN CHECK: FAIL — expecting coverage.include to contain both "
            "src/lib/api and src/stores scopes"
        )
        assert any(e.startswith("src/lib/api") for e in facts["include"]), (
            f"coverage.include in {name} lost the src/lib/api scope "
            "(per-domain HTTP client modules)"
        )
        assert any(e.startswith("src/stores") for e in facts["include"]), (
            f"coverage.include in {name} lost the src/stores scope "
            "(Zustand stores)"
        )
    except Exception:
        print("DESIGN CHECK: FAIL")
        raise
    print("DESIGN CHECK: PASS")


@pytest.mark.skipif(
    _NODE is None,
    reason="node not available (needed for the TypeScript parser)",
)
def test_ac11_design_every_threshold_at_or_above_floor():
    try:
        evidence = _coverage_ast()
        assert evidence is not None, "no coverage config with a coverage block"
        name, facts = evidence
        assert facts["hasCoverage"], f"{name} has no coverage block"

        thresholds_block = facts["thresholds"] if facts["hasThresholds"] else ""
        print(
            "DESIGN CHECK: FAIL — expecting a thresholds block with "
            "lines/branches/functions/statements entries"
        )
        assert thresholds_block, f"{name} has no thresholds block"
        thresholds = {
            key: float(value) for key, value in dict(thresholds_block).items()
        }
        print(f"thresholds: {thresholds}")
        for key in ("lines", "branches", "functions", "statements"):
            print(
                f"DESIGN CHECK: FAIL — expecting a {key} threshold, "
                f"measured {thresholds.get(key)}"
            )
            assert key in thresholds, f"{name} thresholds must pin {key}"
        below_floor = {k: v for k, v in thresholds.items() if v < _THRESHOLD_FLOOR}
        print(
            "DESIGN CHECK: FAIL — expecting every threshold >= "
            f"{_THRESHOLD_FLOOR} (documented floor), below-floor entries: "
            f"{below_floor}"
        )
        assert not below_floor, (
            f"thresholds below the documented floor of {_THRESHOLD_FLOOR} "
            f"in {name}: {below_floor} — the gate can no longer be zeroed"
        )
    except Exception:
        print("DESIGN CHECK: FAIL")
        raise
    print("DESIGN CHECK: PASS")
