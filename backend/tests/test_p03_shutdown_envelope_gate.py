"""Tests for the shutdown-envelope contract gate (#799 AC5 / #854).

The gate script is `scripts/check_p03_shutdown_envelope.py`: it owns the ONE
derivation of the composed shutdown envelope, reads every ENFORCED term
from the code that enforces it, requires both force-kill limits to sit
strictly above the total, and requires every surface to quote the
derivation verbatim.

This module is the gate's test counterpart (the repo's #740 contract:
every ci.yml `check_*.py` gate must be named by a backend test, so a
deleted or broken gate fails the suite). The gate's own mutation probes
live in the issue trace's `repro/` directory; the cases here pin the
behaviour the gate must have on the SHIPPED tree.
"""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "check_p03_shutdown_envelope.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_p03_shutdown_envelope", GATE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestShutdownEnvelopeGate(unittest.TestCase):
    def setUp(self):
        self.gate = _load_gate()

    def test_gate_script_exists_and_is_runnable(self):
        self.assertTrue(GATE.is_file(), f"missing gate script: {GATE}")
        proc = subprocess.run(
            [sys.executable, str(GATE)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"the gate must pass on the shipped tree:\n{proc.stdout}\n"
            f"{proc.stderr}",
        )
        self.assertIn("composed shutdown envelope", proc.stdout)

    def test_enforced_terms_are_read_from_their_runtime_sources(self):
        # The uvicorn grace comes from the Dockerfile's OPERATIVE CMD.
        self.assertEqual(self.gate._uvicorn_grace_seconds(), 20.0)

        # The lifespan aggregate is the module's budget plus its cap.
        canonical, total = self.gate._derivation()
        self.assertEqual(total, 135.0)
        self.assertIn("20 s (uvicorn request drain)", canonical)
        self.assertIn("65 s (lifespan aggregate)", canonical)
        self.assertIn("15 s (turn-finalizer drain)", canonical)
        self.assertIn("allowance:", canonical)
        self.assertTrue(canonical.endswith("total 135 s"))

    def test_the_operative_cmd_wins_and_the_last_flag_wins(self):
        # Docker runs the LAST CMD, and argparse lets the LAST occurrence
        # of a repeated option win: the gate must read the value the
        # runtime actually receives.
        original = self.gate._DOCKERFILE
        try:
            with tempfile_dockerfile() as path:
                path.write_text(
                    'CMD ["uvicorn --timeout-graceful-shutdown 20"]\n'
                    'CMD ["uvicorn --timeout-graceful-shutdown 40"]\n',
                    encoding="utf-8",
                )
                self.gate._DOCKERFILE = path
                self.assertEqual(self.gate._uvicorn_grace_seconds(), 40.0)

            with tempfile_dockerfile() as path:
                path.write_text(
                    'CMD ["uvicorn --timeout-graceful-shutdown 20 '
                    '--timeout-graceful-shutdown 40"]\n',
                    encoding="utf-8",
                )
                self.gate._DOCKERFILE = path
                self.assertEqual(self.gate._uvicorn_grace_seconds(), 40.0)
        finally:
            self.gate._DOCKERFILE = original

    def test_a_cmd_without_the_flag_fails_closed(self):
        original = self.gate._DOCKERFILE
        try:
            with tempfile_dockerfile() as path:
                path.write_text('CMD ["uvicorn", "app.main:app"]\n', encoding="utf-8")
                self.gate._DOCKERFILE = path
                with self.assertRaises(ValueError):
                    self.gate._uvicorn_grace_seconds()
        finally:
            self.gate._DOCKERFILE = original

    def test_every_surface_quotes_the_derivation_verbatim(self):
        canonical, _total = self.gate._derivation()
        normalized = self.gate._normalize(canonical)
        surfaces = [
            REPO / "docker-compose.yml",
            REPO / "start-services.ps1",
            REPO / "stop-services.ps1",
            REPO
            / "docs"
            / "releases"
            / "pending"
            / "854-799-lifecycle-drain-shutdown-budget.md",
            REPO / "backend" / "app" / "lifespan.py",
        ]
        for surface in surfaces:
            self.assertTrue(
                surface.is_file(),
                f"the envelope surface is missing: {surface}",
            )
            text = self.gate._normalize(surface.read_text(encoding="utf-8"))
            self.assertIn(
                normalized,
                text,
                f"{surface.name} does not quote the envelope derivation "
                "verbatim",
            )

    def test_both_force_limits_exceed_the_envelope(self):
        _canonical, total = self.gate._derivation()
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        grace = self.gate._COMPOSE_GRACE_RE.search(compose)
        self.assertIsNotNone(grace, "docker-compose.yml has no stop_grace_period")
        self.assertGreater(
            self.gate._parse_duration(grace.group(1)), total,
            "the compose grace must exceed the composed envelope",
        )
        for name in ("start-services.ps1", "stop-services.ps1"):
            text = (REPO / name).read_text(encoding="utf-8")
            # The envelope-bounding deadline is the NAMED graceful window:
            # the launchers also carry short fallbacks (a failed signal, a
            # process with no marker match), and reading "the first
            # WaitForExit literal" would bind the wrong one.
            graceful = self.gate._GRACEFUL_WINDOW_RE.search(text)
            self.assertIsNotNone(
                graceful, f"{name} declares no $GracefulWindowMs"
            )
            self.assertGreater(
                int(graceful.group(1)) / 1000.0,
                total,
                f"{name}'s grouped-backend graceful window must exceed the "
                "composed envelope",
            )
            failed_signal = self.gate._FAILED_SIGNAL_WAIT_RE.search(text)
            self.assertIsNotNone(
                failed_signal, f"{name} declares no $FailedSignalWaitMs"
            )
            self.assertLess(
                int(failed_signal.group(1)),
                int(graceful.group(1)),
                f"{name}'s failed-signal fallback must be shorter than the "
                "graceful window: a failed signal starts no graceful "
                "shutdown, so waiting out the full window buys nothing",
            )

    def test_the_named_windows_are_the_ones_the_runtime_waits_on(self):
        # Declaring the constants is not enough: the launcher must SELECT
        # between them on the sender's exit status and wait on the result,
        # or the named value would be a comment in disguise.
        for name in ("start-services.ps1", "stop-services.ps1"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIsNotNone(
                self.gate._WAIT_SELECTION_RE.search(text),
                f"{name} does not select between the failed-signal fallback "
                "and the graceful window on the sender's exit status",
            )
            self.assertIsNotNone(
                self.gate._WAITFOREXIT_NAMED_RE.search(text),
                f"{name} never waits on $WaitMs",
            )

    def test_an_ambiguous_window_declaration_fails_the_gate(self):
        # PowerShell lets the LAST assignment win, so a launcher that
        # declares $GracefulWindowMs twice has an envelope-bounding window
        # the gate cannot see. Reading the first declaration and passing
        # would be a false pass: the gate must fail closed on the ambiguity.
        import shutil
        import tempfile

        original_root = self.gate.ROOT
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            for name in ("start-services.ps1", "stop-services.ps1"):
                shutil.copy(REPO / name, tmp_root / name)
            launcher = tmp_root / "start-services.ps1"
            text = launcher.read_text(encoding="utf-8")
            text = text.replace(
                "$GracefulWindowMs = 140000",
                "$GracefulWindowMs = 140000\n        $GracefulWindowMs = 1000",
                1,
            )
            launcher.write_text(text, encoding="utf-8")
            try:
                self.gate.ROOT = tmp_root
                exit_code = self.gate.main()
            finally:
                self.gate.ROOT = original_root
        self.assertEqual(
            exit_code,
            1,
            "a launcher that declares the graceful window twice must fail "
            "the gate: the last assignment is the one PowerShell uses",
        )


class tempfile_dockerfile:
    """A temporary file usable as a stand-in for the Dockerfile."""

    def __enter__(self):
        import tempfile

        handle, self._path = tempfile.mkstemp(suffix=".Dockerfile")
        os.close(handle)
        return Path(self._path)

    def __exit__(self, *exc_info):
        try:
            os.unlink(self._path)
        except OSError:
            pass
        return False


if __name__ == "__main__":
    unittest.main()
