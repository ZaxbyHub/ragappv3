"""CI reachability wrappers for the issue #773 capability guards.

The guards perform the source-level semantic checks; this test keeps them in
the always-collected backend pytest suite so they cannot remain local-only
scripts or be skipped by a frontend-only change.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARDS = (
    "scripts/check_l02_capability_fixture_keys.py",
    "scripts/check_l02_capability_reader_keys.py",
)
DIAGNOSTIC_LIMIT = 12_000


def _run_guard(path: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, str(REPO / path)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"{path} could not complete: {exc}")


@pytest.mark.parametrize("guard", GUARDS)
def test_l02_guard_passes_in_backend_ci(guard: str) -> None:
    result = _run_guard(guard)
    diagnostics = (result.stdout + result.stderr)[-DIAGNOSTIC_LIMIT:]
    assert result.returncode == 0, (
        f"{guard} exited {result.returncode}; diagnostics:\n{diagnostics}"
    )
