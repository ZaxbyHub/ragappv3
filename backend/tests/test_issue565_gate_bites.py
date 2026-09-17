"""Issue #565: the quality gates can fail — committed bite test.

Proves the async-defect-class gates bite, at three levels:

1. AST collection-guard helper detects ``async def test_*`` methods inside
   plain ``unittest.TestCase`` subclasses (silently dropped by asyncio auto
   mode) and does NOT flag IsolatedAsyncioTestCase subclasses (which
   legitimately run async tests).
2. The never-awaited warning matcher recognises both capture channels
   (direct RuntimeWarning and pytest's PytestUnraisableExceptionWarning
   wrapper emitted when ``-W error::RuntimeWarning`` routes the ``__del__``
   raise through ``sys.unraisablehook``).
3. The session-finish gate raises when a leak was recorded, and only then.
4. Subprocess contrast: under the repo pytest config an in-band
   RuntimeWarning fails the run; under an empty config the identical file
   passes — the exact blindness the pre-#565 suite had for this class.

The mirror image for the gc-timed un-awaited shape (passes without the
guards, fails with them) is enforced end to end by the frozen acceptance
checks C6/C7, which plant real files under backend/tests/ and run the real
repo config.
"""

import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

import conftest as _gates

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"


def _write(tmp_path, name, body):
    target = tmp_path / name
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return target


# --- 1. AST collection guard ------------------------------------------------


def test_ast_guard_flags_async_test_in_plain_testcase(tmp_path):
    probe = _write(
        tmp_path,
        "test_probe_plain.py",
        """
        import unittest


        class TestPlain(unittest.TestCase):
            async def test_silently_dropped(self):
                assert False  # would fail if it ever ran
        """,
    )
    hits = list(_gates.iter_async_testcase_methods([str(probe)]))
    assert len(hits) == 1
    fname, lineno, cls, meth = hits[0]
    assert fname == str(probe)
    assert cls == "TestPlain"
    assert meth == "test_silently_dropped"
    assert lineno > 0


def test_ast_guard_exempts_isolated_asyncio_testcase(tmp_path):
    probe = _write(
        tmp_path,
        "test_probe_isolated.py",
        """
        import unittest


        class TestIsolated(unittest.IsolatedAsyncioTestCase):
            async def test_legit_async(self):
                assert True
        """,
    )
    assert list(_gates.iter_async_testcase_methods([str(probe)])) == []


def test_ast_guard_exempts_sync_testcase(tmp_path):
    probe = _write(
        tmp_path,
        "test_probe_sync.py",
        """
        import unittest


        class TestSync(unittest.TestCase):
            def test_sync(self):
                assert True
        """,
    )
    assert list(_gates.iter_async_testcase_methods([str(probe)])) == []


# --- 2. warning matcher ------------------------------------------------------


def test_warning_matcher_matches_direct_runtime_warning():
    assert _gates.never_awaited_warning_match(
        RuntimeWarning, "coroutine 'guarded_op' was never awaited"
    )


def test_warning_matcher_matches_unraisable_wrapper_channel():
    # Under `-W error::RuntimeWarning` the warn inside coroutine.__del__
    # cannot propagate; pytest's unraisable plugin re-emits it wrapped.
    wrapped = (
        "Exception ignored in: <coroutine object 'guarded_op'>\\n"
        "RuntimeWarning: coroutine 'guarded_op' was never awaited"
    )
    assert _gates.never_awaited_warning_match(
        pytest.PytestUnraisableExceptionWarning, wrapped
    )


def test_warning_matcher_rejects_other_warnings():
    assert not _gates.never_awaited_warning_match(RuntimeWarning, "divide by zero in numpy op")
    assert not _gates.never_awaited_warning_match(UserWarning, "coroutine 'x' was never awaited")


def test_warning_matcher_exempts_mock_internal_coroutines():
    # Blanket AsyncMock test doubles leak their internal call coroutine across
    # test boundaries with unreliable GC attribution; the wrapper name hides
    # which child was dropped. Mock-internal leaks are exempt (documented in
    # never_awaited_warning_match); real production methods stay gated.
    assert not _gates.never_awaited_warning_match(
        pytest.PytestUnraisableExceptionWarning,
        "coroutine 'AsyncMockMixin._execute_mock_call' was never awaited",
    )


# --- 3. session-finish gate ---------------------------------------------------


def test_sessionfinish_raises_on_recorded_leak_and_is_clearable():
    _gates._NEVER_AWAITED_RECORDED.clear()
    try:
        _gates._NEVER_AWAITED_RECORDED.append(
            "RuntimeWarning: coroutine 'leaky_op' was never awaited"
        )
        with pytest.raises(pytest.UsageError, match="never-awaited coroutine"):
            _gates.pytest_sessionfinish(session=None, exitstatus=0)
    finally:
        # The real session finish must not see this synthetic record.
        _gates._NEVER_AWAITED_RECORDED.clear()


def test_sessionfinish_silent_without_records():
    _gates._NEVER_AWAITED_RECORDED.clear()
    try:
        _gates.pytest_sessionfinish(session=None, exitstatus=0)  # no raise
    finally:
        _gates._NEVER_AWAITED_RECORDED.clear()


# --- 4. subprocess contrast: repo config escalates, empty config doesn't ------


def _run_pytest(tmp_path, test_body, config_name):
    # The probe file must NOT live under the system temp tree: pytest walks the
    # argument's ancestor directories during collection, and on this box the
    # shared temp root races with concurrently-deleted sibling dirs (zmem /
    # tooling scratch), aborting collection with FileNotFoundError. backend/data
    # is gitignored (repo .gitignore), so scratch there never pollutes the tree.
    scratch = _BACKEND / "data" / "_565_gate_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    probe = scratch / "test_gate_probe.py"
    probe.write_text(textwrap.dedent(test_body), encoding="utf-8")
    try:
        cmd = [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"]
        if config_name == "empty":
            cfg = tmp_path / "empty-pytest.ini"
            cfg.write_text("[pytest]\n", encoding="utf-8")
            cmd += ["-c", str(cfg)]
        elif config_name == "repo":
            # Explicit repo config: the addopts/-W/filterwarnings gates under test.
            cmd += ["-c", str(_BACKEND / "pyproject.toml")]
        proc = subprocess.run(
            cmd,
            cwd=str(_BACKEND),
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ),
        )
        return proc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def test_repo_config_fails_inband_runtime_warning(tmp_path):
    body = """
        import warnings


        def test_emits_runtime_warning():
            warnings.warn("gate probe in-band RuntimeWarning", RuntimeWarning)
    """
    with_repo_config = _run_pytest(tmp_path, body, "repo")
    assert with_repo_config.returncode != 0, with_repo_config.stdout
    assert "gate probe in-band RuntimeWarning" in with_repo_config.stdout

    without_config = _run_pytest(tmp_path, body, "empty")
    assert without_config.returncode == 0, without_config.stdout + without_config.stderr


def test_unawaited_coroutine_passes_without_gates(tmp_path):
    # The pre-#565 blindness, documented: an un-awaited coroutine defined at a
    # production-like filename passes under an empty config (no addopts, no
    # guards). The with-gates counterpart is enforced end to end by the frozen
    # acceptance checks (C6 LEG A / C7 LEG 2), which run this shape under the
    # real repo config and require the run to FAIL.
    body = """
        _NS = {}
        exec(
            compile(
                "async def _prod_like_op():\\n    return 1\\n",
                "app/services/prod_like.py",
                "exec",
            ),
            _NS,
        )


        def test_never_awaits():
            _NS["_prod_like_op"]()
            assert True
    """
    proc = _run_pytest(tmp_path, body, "empty")
    assert proc.returncode == 0, proc.stdout + proc.stderr
