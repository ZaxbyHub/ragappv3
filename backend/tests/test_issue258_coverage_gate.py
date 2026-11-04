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


def _coverage_config_evidence() -> tuple[str, dict] | None:
    """(file, TS-AST facts) of the first config that declares coverage
    thresholds. #746 AC7 family (implementation-review revision 3): the
    previous raw-text scan was comment-blind — a fully commented-out
    coverage block still satisfied it (reviewer probe). It now reuses the
    TypeScript-compiler walker from test_issue258_coverage_design, so only
    real AST nodes count."""
    from test_issue258_coverage_design import _coverage_ast

    return _coverage_ast()


def test_ac11_coverage_thresholds_configured():
    try:
        evidence = _coverage_config_evidence()
        print(f"coverage config: {evidence[0] if evidence else None}")
        assert evidence, (
            "frontend must configure Vitest coverage thresholds "
            "(vitest.config.* or test.coverage in vite.config.ts) — ENH-006: "
            "the frontend has zero coverage configuration today"
        )
        name, facts = evidence
        assert facts["hasThresholds"], (
            f"{name} mentions coverage but has no thresholds block in the AST "
            "(a commented-out block does not count — #746 AC7)"
        )
    except Exception:
        print("AC11 CHECK: FAIL")
        raise
    print("AC11 CHECK: PASS")


def test_ac11_coverage_evidence_is_comment_aware(tmp_path):
    """Self-check for the #746 AC7 class: a vite.config.ts whose entire
    coverage/thresholds block exists ONLY inside comments must NOT count as
    evidence (the reviewer probe shape). The walker resolves typescript via
    NODE_PATH pointed at the REAL frontend install (PRR-007, feedback round
    1): repointing FRONTEND at tmp_path without feeding the real
    node_modules made the walker skip with a false "typescript not
    resolvable" message on ordinary dev boxes."""
    import os

    import test_issue258_coverage_design as design

    real_node_modules = design.REPO / "frontend" / "node_modules"
    if not real_node_modules.is_dir():
        pytest.skip(
            "no real frontend/node_modules install to resolve typescript "
            "from — the self-check cannot exercise the walker here"
        )
    commented = tmp_path / "vite.config.ts"
    commented.write_text(
        "export default defineConfig({\n"
        "  // coverage: {\n"
        "  //   thresholds: { lines: 80, branches: 70 }\n"
        "  // }\n"
        "});\n",
        encoding="utf-8",
    )
    saved = design.FRONTEND
    saved_node_path = os.environ.get("NODE_PATH")
    design.FRONTEND = tmp_path
    os.environ["NODE_PATH"] = str(real_node_modules)
    try:
        evidence = _coverage_config_evidence()
    finally:
        design.FRONTEND = saved
        if saved_node_path is None:
            os.environ.pop("NODE_PATH", None)
        else:
            os.environ["NODE_PATH"] = saved_node_path
    assert evidence is None, (
        "a fully commented-out coverage block must not be accepted as "
        "coverage evidence (comment-blind scanner class, #746 AC7)"
    )


@pytest.mark.skipif(
    _NPM is None or not (FRONTEND / "node_modules").is_dir(),
    reason="npm or frontend/node_modules unavailable — since #742 the "
    "Backend CI job no longer installs a frontend tree; this node's single "
    "CI venue is the Frontend job's scoped pytest step (dev boxes with a "
    "local frontend install also run it)",
)
def test_ac11_coverage_command_passes(tmp_path):
    # Issue #943: the coverage command's artifacts (including vitest's
    # transient reports-directory lifecycle) must stay OUTSIDE the
    # repository root — the default ``coverage/`` directory under
    # ``frontend/`` appears and disappears mid-run, racing any concurrent
    # repo-wide walk under pytest-xdist (observed crashing the secretscan
    # contract test). The redirect confines the whole lifecycle to a
    # per-test pytest tmp dir.
    reports_dir = tmp_path / "coverage-reports"
    frontend_coverage = FRONTEND / "coverage"
    try:
        assert not frontend_coverage.exists(), (
            "frontend/coverage exists before the run — previous artifact "
            "left in place; the confinement assertion below needs a clean slate"
        )
        package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
        scripts = package.get("scripts", {})
        # The json reporter makes the redirect OBSERVABLE: vitest must write
        # coverage-report.json into reports_dir, proving the artifacts really
        # landed outside the repository root (issue #943).
        redirect_flags = [
            f"--coverage.reportsDirectory={reports_dir.as_posix()}",
            "--coverage.reporter=json",
        ]
        if "test:coverage" in scripts:
            argv = [_NPM, "run", "test:coverage", "--", *redirect_flags]
            command = "npm run test:coverage"
        else:
            argv = [
                _NPM,
                "exec",
                "--",
                "vitest",
                "run",
                "--coverage",
                *redirect_flags,
            ]
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
        # Confinement oracle (issue #943): the redirect took effect (the
        # json report landed OUTSIDE the repo) and no coverage directory
        # exists under the repository root.
        assert any(reports_dir.iterdir()), (
            f"{command} wrote no coverage report into {reports_dir} — the "
            "reportsDirectory redirect did not take effect, so vitest may "
            "have written artifacts under the repository root"
        )
        assert not frontend_coverage.exists(), (
            "frontend/coverage exists after the run — coverage artifacts "
            "were written under the repository root (issue #943 race)"
        )
    except Exception:
        print("AC11 CHECK: FAIL")
        raise
    print("AC11 CHECK: PASS")
