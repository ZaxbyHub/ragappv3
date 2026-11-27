"""Contract-wiring test for the graceful-shutdown envelope gate (#854/#799).

Mirrors test_b03_contract_wiring.py's shape: proves the ci.yml gate exists,
executes the script green at the current tree, and pins the script's FAIL
behaviors on malformed inputs (unit-driven over temp copies of the launch
surfaces).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_p03_graceful_shutdown.py"
COMMAND = "python scripts/check_p03_graceful_shutdown.py"


class TestGracefulShutdownGateWiring(unittest.TestCase):
    def test_ci_yml_runs_the_gate(self):
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "scripts/check_p03_graceful_shutdown.py",
            ci,
            "ci.yml must run the graceful-shutdown envelope gate",
        )

    def test_gate_green_on_current_tree(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_gate_fails_when_dockerfile_flag_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(REPO, Path(tmp) / "repo", ignore=shutil.ignore_patterns(
                ".git", "node_modules", "graphify-out", ".venv", "venv",
            ))
            repo = Path(tmp) / "repo"
            dockerfile = repo / "Dockerfile"
            src = dockerfile.read_text(encoding="utf-8")
            dockerfile.write_text(
                src.replace("--timeout-graceful-shutdown 20", ""), encoding="utf-8"
            )
            proc = subprocess.run(
                [sys.executable, str(repo / "scripts" / "check_p03_graceful_shutdown.py")],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(
                "FAIL: Dockerfile uvicorn CMD has no --timeout-graceful-shutdown",
                proc.stdout,
            )

    def test_gate_fails_on_unit_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(REPO, Path(tmp) / "repo", ignore=shutil.ignore_patterns(
                ".git", "node_modules", "graphify-out", ".venv", "venv",
            ))
            repo = Path(tmp) / "repo"
            dockerfile = repo / "Dockerfile"
            src = dockerfile.read_text(encoding="utf-8")
            dockerfile.write_text(
                src.replace("--timeout-graceful-shutdown 20", "--timeout-graceful-shutdown 20s"),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(repo / "scripts" / "check_p03_graceful_shutdown.py")],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(
                "must be an integer number of seconds",
                proc.stdout,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
