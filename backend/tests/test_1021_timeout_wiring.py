"""CI-command wiring gate for the #1021 hang-class fix.

Issue #1021's production change is two pytest flags: ``--timeout-method=thread``
on the Backend job's Test step and the nightly full-suite step. The canary
step cannot protect those lines — its own legs pass the flag explicitly, so a
silent revert of the production commands leaves every job green (proven by
mutation during the trace review). This module is the pin the review required:

* both production invocation lines carry the exact timeout flags;
* the hang-class canary step exists, is step-scoped to the Backend job, and
  still selects its four nodeids;
* the local/doc mirrors of the invocation (justfile, testing.md, the two CI
  skills) have not drifted.

At the pre-fix base the production lines and the canary step do not exist, so
these assertions fail there and pass at head — this file is also the PR's
executing closure-evidence test for AC1 (#1021).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_THREAD_FLAGS = "--timeout=300 --timeout-method=thread"
# #1038 Item A: the canonical Backend full-suite invocation, verbatim from the
# ci.yml Test step. Every declared mirror must carry this exact string. Kept
# on ONE physical line (E501 is repo-ignored) so the frozen C1 probe's
# line-based grep can pin it in this gate's source.
_CANONICAL_INVOCATION = "pytest --tb=short -v --timeout=300 --timeout-method=thread -rs -n auto --cov --cov-report=term-missing tests/"
_CANONICAL_FLAGS = (
    "--tb=short",
    "-v",
    "--timeout=300",
    "--timeout-method=thread",
    "-rs",
    "-n auto",
    "--cov",
    "--cov-report=term-missing",
)
_CANARY_STEP_NAME = "Hang-class timeout canary (#1021)"
_CANARY_NODEIDS = (
    "test_wedge_c_block_aborted_by_configured_timeout",
    "test_wedge_starved_pool_join_aborted_by_configured_timeout",
)
_WATCHDOG_SCRIPT = "ci_backend_watchdog.py"
_WATCHDOG_HOOK_SCRIPT = "ci_watchdog_faulthandler_hook.py"
_WATCHDOG_HOOK_STEP_NAME = "Write Backend watchdog faulthandler hook (#1021)"
_WATCHDOG_ENV_KEYS = ("PYTHONFAULTHANDLER", "PYTHONPATH", "WATCHDOG_DUMP_DIR")


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def _backend_test_step_run_lines(ci: str) -> list[str]:
    """The Backend Test step's pytest invocation line(s), watchdog-wrapped or not.

    #1021's residual fix wraps the run in the out-of-process watchdog, so the
    line no longer starts with ``run: pytest`` — accept either shape while
    keeping the canary legs excluded (their pytest calls live inside a bash
    block, not on ``run:`` lines).
    """
    return [
        line
        for line in ci.splitlines()
        if line.strip().startswith("run:")
        and "tests/" in line
        and (
            line.strip().startswith("run: pytest")
            or _WATCHDOG_SCRIPT in line
        )
    ]


def _missing_flags(text: str) -> list[str]:
    return [flag for flag in _CANONICAL_FLAGS if flag not in text]


def test_backend_test_step_pins_thread_timeout():
    ci = _read(".github", "workflows", "ci.yml")
    test_step = _backend_test_step_run_lines(ci)
    assert test_step, (
        "ci.yml Backend Test step run line not found (neither a plain "
        "`run: pytest ... tests/` line nor a watchdog-wrapped one)"
    )
    # The full-suite invocation (not the canary legs, which are in the bash
    # block, not `run:` lines).
    for line in test_step:
        assert _THREAD_FLAGS in line, (
            f"ci.yml Test step lost the hang-class timeout flags: {line.strip()}"
        )


def test_nightly_full_suite_pins_thread_timeout():
    nightly = _read(".github", "workflows", "nightly.yml")
    run_lines = [
        line
        for line in nightly.splitlines()
        if line.strip().startswith("run: pytest")
    ]
    assert run_lines, "nightly has no pytest run lines"
    # EVERY pytest invocation in nightly (full suite AND the real-parser
    # fixtures step) must carry the flags — an any() over the line set
    # would let a revert of one line hide behind the other (probe-proven).
    unflagged = [line for line in run_lines if _THREAD_FLAGS not in line]
    assert not unflagged, (
        f"nightly pytest run lines lost the hang-class timeout flags: "
        f"{unflagged}"
    )


def test_hang_canary_step_is_wired():
    ci = _read(".github", "workflows", "ci.yml")
    assert _CANARY_STEP_NAME in ci, "hang-class canary step missing from ci.yml"
    assert "RUN_CI_HANG_CANARY" in ci, "canary env gate missing from ci.yml"
    for nodeid in _CANARY_NODEIDS:
        assert nodeid in ci, f"canary leg for {nodeid} missing from ci.yml"
    # All four legs present: dropping a leg must fail this gate too.
    assert ci.count("timeout 300 pytest") == 4, (
        "expected exactly 4 canary legs (cblock/thread, pooljoin/thread, "
        "pooljoin/xdist, pooljoin/signal)"
    )


def test_local_and_doc_mirrors_carry_the_flag():
    justfile = _read("justfile")
    assert _THREAD_FLAGS in justfile, "justfile backend-test mirror drifted"

    testing_md = _read("docs", "engineering", "testing.md")
    assert _THREAD_FLAGS in testing_md, "testing.md mirror drifted"

    audit_skill = _read(
        ".agents", "skills", "ci-compatibility-audit", "SKILL.md"
    )
    assert _THREAD_FLAGS in audit_skill, (
        "ci-compatibility-audit skill mirror drifted"
    )

    fix_skill = _read(".agents", "skills", "ci-fix-monitor", "SKILL.md")
    assert _THREAD_FLAGS in fix_skill, "ci-fix-monitor skill mirror drifted"


def test_canonical_flags_at_every_declared_mirror():
    """#1038 Item A: every declared mirror carries the canonical invocation.

    A mirror dropping or altering ANY flag of the canonical set fails here
    with a message naming the site and each missing flag (the frozen mutant
    probe drops ``-rs`` from the ci.yml Test step and must fail via this
    naming, not via line-not-found).
    """
    sites = {
        "ci.yml Backend Test step": _read(".github", "workflows", "ci.yml"),
        "justfile backend-test recipe": _read("justfile"),
        "docs/engineering/testing.md": _read("docs", "engineering", "testing.md"),
        ".agents/skills/ci-compatibility-audit/SKILL.md": _read(
            ".agents", "skills", "ci-compatibility-audit", "SKILL.md"
        ),
        ".agents/skills/ci-fix-monitor/SKILL.md": _read(
            ".agents", "skills", "ci-fix-monitor", "SKILL.md"
        ),
    }
    for site, text in sites.items():
        assert _CANONICAL_INVOCATION in text, (
            f"{site} drifted from the canonical Backend invocation "
            f"(missing flag(s): {', '.join(_missing_flags(text)) or 'flag order/shape'})"
        )


def test_ci_fix_monitor_backend_rows_match_canonical():
    """#1038 Item A AC4: BOTH ci-fix-monitor command sites match canonical.

    The job-row table (:32-era) and the reproduction block (:134-era) each
    carry the canonical single invocation, and the phantom ``cov=app``
    split-coverage run is gone entirely.
    """
    fix_skill = _read(".agents", "skills", "ci-fix-monitor", "SKILL.md")
    assert "cov=app" not in fix_skill, (
        "ci-fix-monitor still describes a phantom split coverage run "
        "(cov=app matches no ci.yml command)"
    )
    occurrences = fix_skill.count(_CANONICAL_INVOCATION)
    assert occurrences >= 2, (
        f"ci-fix-monitor must carry the canonical invocation at BOTH command "
        f"sites (job row + reproduction block); found {occurrences}"
    )


def test_backend_test_step_invokes_watchdog():
    """#1021 AC-W3: the Backend Test step runs under the out-of-process
    watchdog, with the full stack-capture wiring (hook copy step + env) that
    the watchdog's fail-closed preflight depends on."""
    ci = _read(".github", "workflows", "ci.yml")
    run_lines = _backend_test_step_run_lines(ci)
    assert run_lines, "ci.yml Backend Test step run line not found"
    wrapped = [line for line in run_lines if _WATCHDOG_SCRIPT in line]
    assert wrapped, (
        "ci.yml Backend Test step no longer invokes the #1021 watchdog "
        f"(no {_WATCHDOG_SCRIPT} on the run line): {run_lines}"
    )
    assert _CANONICAL_INVOCATION in wrapped[0], (
        "ci.yml Backend Test step: watchdog wrapping must keep the canonical "
        f"pytest flags verbatim (missing: {', '.join(_missing_flags(wrapped[0]))})"
    )
    assert _WATCHDOG_HOOK_SCRIPT in ci, (
        "ci.yml lost the watchdog faulthandler hook step "
        f"(scripts/{_WATCHDOG_HOOK_SCRIPT} is no longer copied)"
    )
    assert _WATCHDOG_HOOK_STEP_NAME in ci, (
        "ci.yml lost the 'Write Backend watchdog faulthandler hook (#1021)' step"
    )
    for key in _WATCHDOG_ENV_KEYS:
        assert f"{key}:" in ci, (
            f"ci.yml Backend Test step lost the {key} env (watchdog stack "
            "capture wiring)"
        )
