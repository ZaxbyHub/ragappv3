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
"""

import re
from pathlib import Path

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

_THRESHOLD_PATTERN = re.compile(
    r"(?P<key>lines|branches|functions|statements)\s*:\s*(?P<value>-?\d+(?:\.\d+)?)"
)


def _coverage_config_text() -> tuple[str, str] | None:
    """(file, text) of the first config declaring a coverage block."""
    for name in _VITEST_CONFIG_CANDIDATES:
        path = FRONTEND / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"coverage\s*:", text):
            return name, text
    return None


def _extract_block(text: str, keyword: str) -> str:
    """Extract the balanced ``{...}`` or ``[...]`` block following ``keyword:``.

    (Or '' when the key is absent — coverage.include is an array literal,
    coverage.thresholds is an object literal.)
    """
    match = re.search(rf"{keyword}\s*:\s*(\{{|\[)", text)
    if match is None:
        return ""
    opening, closing = ("{", "}") if match.group(1) == "{" else ("[", "]")
    depth = 0
    start = match.end() - 1  # position of the opening bracket
    for index in range(start, len(text)):
        char = text[index]
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def test_ac11_design_coverage_include_scopes_both_contracts():
    try:
        evidence = _coverage_config_text()
        print(f"coverage config: {evidence[0] if evidence else None}")
        assert evidence is not None, (
            "no vitest/vite config declares a coverage block — the ENH-006 "
            "gate would not exist (see test_issue258_coverage_gate.py)"
        )
        name, text = evidence
        coverage_block = _extract_block(text, "coverage")
        assert coverage_block, f"{name} mentions coverage but has no coverage block"

        include_block = _extract_block(coverage_block, "include")
        print(
            "DESIGN CHECK: FAIL — expecting coverage.include to contain both "
            "src/lib/api and src/stores scopes"
        )
        assert "src/lib/api" in include_block, (
            f"coverage.include in {name} lost the src/lib/api scope "
            "(per-domain HTTP client modules)"
        )
        assert "src/stores" in include_block, (
            f"coverage.include in {name} lost the src/stores scope "
            "(Zustand stores)"
        )
    except Exception:
        print("DESIGN CHECK: FAIL")
        raise
    print("DESIGN CHECK: PASS")


def test_ac11_design_every_threshold_at_or_above_floor():
    try:
        evidence = _coverage_config_text()
        assert evidence is not None, "no coverage config with a coverage block"
        name, text = evidence
        coverage_block = _extract_block(text, "coverage")
        assert coverage_block, f"{name} has no coverage block"

        thresholds_block = _extract_block(coverage_block, "thresholds")
        print(
            "DESIGN CHECK: FAIL — expecting a thresholds block with "
            "lines/branches/functions/statements entries"
        )
        assert thresholds_block, f"{name} has no thresholds block"
        thresholds = {
            match.group("key"): float(match.group("value"))
            for match in _THRESHOLD_PATTERN.finditer(thresholds_block)
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
