"""Issue #780 L09 CI guard tests.

``scripts/check_l09_skill_trigger_paths.py``, ``check_l09_index_assets.py``
and ``check_l09_memory_response_fields.py`` gate CI (Quality contracts job +
justfile), so their wiring and fail-closed behavior are pinned here following
the repo's gate-test precedents (``test_l05_ci_guards.py``,
``test_l07_gate_wiring.py``). Unlike check_l07 (which resolves its root from
cwd), the three l09 guards anchor ``ROOT = Path(__file__).resolve().parents[1]``
— so the fail-closed exit-2 pins run a COPY of each guard planted in a
fixture tree where the scanned surface is absent (the same relocated-copy
mechanism the frozen vitest drivers use), while the real-tree exit-0 runs
keep the plain cwd form.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
DIAGNOSTIC_LIMIT = 12_000

L09_GUARDS = (
    "check_l09_skill_trigger_paths.py",
    "check_l09_index_assets.py",
    "check_l09_memory_response_fields.py",
)
CI_REL = Path(".github") / "workflows" / "ci.yml"


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        timeout=120,
        capture_output=True,
        text=True,
    )


def test_guards_pass_on_real_tree() -> None:
    # Post-#780 tree: no stale glob, no missing %BASE_URL% asset, no phantom
    # response field. Any hit here is a regression of the shipped fixes.
    for name in L09_GUARDS:
        result = _run(SCRIPTS / name, REPO)
        assert result.returncode == 0, (
            f"{name}: "
            + result.stdout[-DIAGNOSTIC_LIMIT:]
            + result.stderr[-DIAGNOSTIC_LIMIT:]
        )


def test_guards_wired_into_ci_and_justfile() -> None:
    ci = (REPO / CI_REL).read_text(encoding="utf-8")
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    for name in L09_GUARDS:
        run_line = f"python scripts/{name}"
        assert run_line in ci, f"{name} missing from ci.yml Quality contracts steps"
        assert run_line in justfile, f"{name} missing from the justfile quality-contracts recipe"


def test_guards_fail_closed_when_scanned_surface_is_absent(tmp_path: Path) -> None:
    # The guards anchor ROOT to their own location, so plant a COPY in a
    # fixture tree whose scanned surfaces do not exist: each must exit 2
    # (fail closed), never silently pass.
    fixture_scripts = tmp_path / "scripts"
    fixture_scripts.mkdir(parents=True)
    for name in L09_GUARDS:
        shutil.copyfile(SCRIPTS / name, fixture_scripts / name)
        result = _run(fixture_scripts / name, tmp_path)
        assert result.returncode == 2, (
            f"{name}: expected exit 2 on a tree without the scanned surfaces, "
            f"got {result.returncode}; stdout tail: "
            + result.stdout[-DIAGNOSTIC_LIMIT:]
            + "; stderr tail: "
            + result.stderr[-DIAGNOSTIC_LIMIT:]
        )
