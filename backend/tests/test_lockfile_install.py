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


def _lock_entry_line(lockfile_name: str, package: str) -> str:
    """Return the first line of `package`'s entry (version + markers)."""
    lock_path = os.path.join(
        os.path.dirname(__file__), "..", lockfile_name
    )
    lock_path = os.path.normpath(lock_path)
    prefix = package + "=="
    with open(lock_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith(prefix):
                return line.rstrip("\n")
    raise AssertionError(f"{package} entry not found in {lockfile_name}")


class TestUniversalLockfiles:
    """Issue #567 (C15/E10): locks must carry platform markers.

    Regenerating with a Linux-only resolver (the pre-#567 pip-compile
    procedure) strips these markers and drops the win32-conditional entries,
    which re-breaks Windows installs — these assertions turn that red.
    """

    def test_uvloop_excluded_on_win32_in_both_locks(self) -> None:
        for name in ("requirements-lock.txt", "requirements-lock-ci.txt"):
            entry = _lock_entry_line(name, "uvloop")
            assert "sys_platform" in entry and "win32" in entry, (
                f"uvloop entry in {name} lacks a win32-exclusion marker "
                f"(a Windows install would try to build the sdist): {entry}"
            )

    def test_prod_cuda_family_gated_to_linux(self) -> None:
        entry = _lock_entry_line("requirements-lock.txt", "nvidia-cufile")
        assert "sys_platform" in entry and "linux" in entry, (
            "nvidia-cufile entry lacks a sys_platform marker (no win32 "
            f"distribution exists at any version): {entry}"
        )

    def test_tzdata_present_and_win32_gated_in_both_locks(self) -> None:
        for name in ("requirements-lock.txt", "requirements-lock-ci.txt"):
            entry = _lock_entry_line(name, "tzdata")
            assert "sys_platform" in entry and "win32" in entry, (
                f"tzdata entry in {name} lacks a win32 marker (pandas needs "
                f"it on Windows): {entry}"
            )
