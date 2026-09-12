"""Issue #258 acceptance check — frontend coverage gate (AC11 / ENH-006).

The backend already has a scoped, rationale-documented coverage gate
(pyproject ``[tool.coverage]`` SSRF modules, ``fail_under = 90``); the frontend
has none. These nodes pin the ENH-006 contract:

* a Vitest coverage configuration with thresholds must exist
  (``vitest.config.*`` or ``test.coverage`` in ``vite.config.ts``), and
* the coverage command must pass against those thresholds.

Authoring the baseline itself (choosing the meaningful-contract module set and
the threshold values) is Phase 4 scope per the issue ("coverage gates based on
meaningful exercised contracts, not a blanket percentage that can be gamed") —
these checks only require the gate to exist and to pass.

Toolchain dependency (documented): the command node shells out through
``frontend/node_modules`` — in a linked worktree that directory is a junction
to the primary checkout's install. Without npm on PATH the node SKIPS with an
explicit reason; on any machine that can run the frontend suite it runs for
real. Timeout is the tight 120s issue-tracer standard; if Phase 4's scoped
coverage suite legitimately exceeds it, raise it with timing evidence.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
_TIMEOUT_SECONDS = 120

_NPM = shutil.which("npm")
_VITEST_CONFIG_CANDIDATES = (
    "vitest.config.ts",
    "vitest.config.mts",
    "vitest.config.js",
)


def _coverage_config_evidence() -> tuple[str, str] | None:
    """(file, text) of the first config that declares coverage thresholds."""
    for name in _VITEST_CONFIG_CANDIDATES:
        path = FRONTEND / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if "coverage" in text and "thresholds" in text:
                return name, text
    vite_config = FRONTEND / "vite.config.ts"
    if vite_config.is_file():
        text = vite_config.read_text(encoding="utf-8")
        if "coverage" in text and "thresholds" in text:
            return "vite.config.ts", text
    return None


def test_ac11_coverage_thresholds_configured():
    try:
        evidence = _coverage_config_evidence()
        print(f"coverage config: {evidence[0] if evidence else None}")
        assert evidence, (
            "frontend must configure Vitest coverage thresholds "
            "(vitest.config.* or test.coverage in vite.config.ts) — ENH-006: "
            "the frontend has zero coverage configuration today"
        )
        name, text = evidence
        assert "thresholds" in text, f"{name} mentions coverage but no thresholds"
    except Exception:
        print("AC11 CHECK: FAIL")
        raise
    print("AC11 CHECK: PASS")


@pytest.mark.skipif(_NPM is None, reason="no npm on PATH (frontend toolchain unavailable)")
def test_ac11_coverage_command_passes():
    try:
        package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        if "test:coverage" in scripts:
            argv = [_NPM, "run", "test:coverage"]
            command = "npm run test:coverage"
        else:
            argv = [_NPM, "exec", "--", "vitest", "run", "--coverage"]
            command = "npm exec -- vitest run --coverage"
        print(f"argv: {argv}")
        result = subprocess.run(
            argv,
            cwd=str(FRONTEND),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        print(f"exit: {result.returncode}")
        tail = (result.stdout + result.stderr).strip().splitlines()[-12:]
        for line in tail:
            print(f"  {line}")
        assert result.returncode == 0, (
            f"the frontend coverage command ({command}) must pass against the "
            "configured thresholds"
        )
    except Exception:
        print("AC11 CHECK: FAIL")
        raise
    print("AC11 CHECK: PASS")
