"""Tests that verify the pip lockfiles are well-formed and parseable.

These tests are intentionally lightweight: they check file presence, content
shape, and pip's ability to resolve the lockfiles without performing full
network downloads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile


def _pip_parse_report(lockfile_path: str) -> tuple[bool, str]:
    """Run pip install --dry-run --report on a lockfile and return (success, error)."""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".json", prefix="pip-lock-report-", delete=False
        ) as f:
            report_path = f.name
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--dry-run",
                    "--report",
                    report_path,
                    "-r",
                    lockfile_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return True, ""
            return False, result.stderr.strip()
        finally:
            if os.path.exists(report_path):
                os.unlink(report_path)
    except subprocess.TimeoutExpired:
        return False, "pip install --dry-run timed out after 120s"
    except Exception as e:
        return False, str(e)


class TestLockfileInstall:
    """Verify pip lockfiles exist, are non-empty, and parseable by pip."""

    def test_ci_lockfile_exists_and_nonempty(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock-ci.txt"
        )
        lock_path = os.path.normpath(lock_path)
        assert os.path.isfile(lock_path), (
            f"requirements-lock-ci.txt not found at {lock_path}"
        )
        size = os.path.getsize(lock_path)
        assert size > 0, "requirements-lock-ci.txt is empty"
        assert size > 1000, (
            f"requirements-lock-ci.txt is suspiciously small ({size} bytes); "
            "expected a full lockfile with many packages"
        )

    def test_prod_lockfile_exists_and_nonempty(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock.txt"
        )
        lock_path = os.path.normpath(lock_path)
        assert os.path.isfile(lock_path), (
            f"requirements-lock.txt not found at {lock_path}"
        )
        size = os.path.getsize(lock_path)
        assert size > 0, "requirements-lock.txt is empty"
        assert size > 1000, (
            f"requirements-lock.txt is suspiciously small ({size} bytes); "
            "expected a full lockfile with many packages"
        )

    def test_ci_lockfile_has_pinned_versions_and_hashes(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock-ci.txt"
        )
        lock_path = os.path.normpath(lock_path)
        with open(lock_path, encoding="utf-8") as f:
            content = f.read()
        # Check for pinned versions: pkg==1.2.3
        pinned = re.findall(r"^[a-zA-Z0-9_\-]+==[\d\.]+", content, re.MULTILINE)
        assert len(pinned) >= 10, (
            f"Expected at least 10 pinned packages (pkg==version) in "
            f"requirements-lock-ci.txt, found {len(pinned)}"
        )
        # Check for hash lines
        hash_lines = re.findall(r"--hash=sha256:[a-f0-9]{64}", content)
        assert len(hash_lines) >= 10, (
            f"Expected at least 10 --hash=sha256: entries in "
            f"requirements-lock-ci.txt, found {len(hash_lines)}"
        )

    def test_prod_lockfile_has_pinned_versions_and_hashes(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock.txt"
        )
        lock_path = os.path.normpath(lock_path)
        with open(lock_path, encoding="utf-8") as f:
            content = f.read()
        # Check for pinned versions: pkg==1.2.3
        pinned = re.findall(r"^[a-zA-Z0-9_\-]+==[\d\.]+", content, re.MULTILINE)
        assert len(pinned) >= 50, (
            f"Expected at least 50 pinned packages (pkg==version) in "
            f"requirements-lock.txt, found {len(pinned)}"
        )
        # Check for hash lines
        hash_lines = re.findall(r"--hash=sha256:[a-f0-9]{64}", content)
        assert len(hash_lines) >= 50, (
            f"Expected at least 50 --hash=sha256: entries in "
            f"requirements-lock.txt, found {len(hash_lines)}"
        )

    def test_ci_lockfile_is_parseable_by_pip(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock-ci.txt"
        )
        lock_path = os.path.normpath(lock_path)
        ok, err = _pip_parse_report(lock_path)
        assert ok, f"pip could not parse requirements-lock-ci.txt: {err}"

    def test_prod_lockfile_is_parseable_by_pip(self) -> None:
        lock_path = os.path.join(
            os.path.dirname(__file__), "..", "requirements-lock.txt"
        )
        lock_path = os.path.normpath(lock_path)
        ok, err = _pip_parse_report(lock_path)
        assert ok, f"pip could not parse requirements-lock.txt: {err}"
