"""PR #647 review (L5-1): gate the calibration validator inside pytest.

Runs all seven validate.py scopes as subprocesses and asserts exit 0, so
the frozen evidence artifacts (dataset, distributions, cuts incl.
cutoff_sweep provenance, E05/perf/research/release evidence) cannot drift
silently - mirroring the scripts/check_* quality-contract pattern.
"""

import subprocess
import sys
from pathlib import Path

VALIDATOR = (
    Path(__file__).resolve().parent
    / "eval"
    / "calibration_2026_09"
    / "validate.py"
)

SCOPES = [
    "dataset",
    "distributions",
    "cuts",
    "e05",
    "perf",
    "research",
    "release",
]


def test_calibration_validator_scopes():
    for scope in SCOPES:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "--scope", scope],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"validate.py --scope {scope} failed:"
            f" {result.stdout} {result.stderr}"
        )
        assert f"PASS: {scope}" in result.stdout
