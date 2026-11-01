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
_CANARY_STEP_NAME = "Hang-class timeout canary (#1021)"
_CANARY_NODEIDS = (
    "test_wedge_c_block_aborted_by_configured_timeout",
    "test_wedge_starved_pool_join_aborted_by_configured_timeout",
)


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_backend_test_step_pins_thread_timeout():
    ci = _read(".github", "workflows", "ci.yml")
    test_step = [
        line
        for line in ci.splitlines()
        if line.strip().startswith("run: pytest") and "tests/" in line
    ]
    assert test_step, "Backend Test step run line not found in ci.yml"
    # The full-suite invocation (not the canary legs, which are in the bash
    # block, not `run:` lines).
    assert any(_THREAD_FLAGS in line for line in test_step), (
        "ci.yml Test step lost the hang-class timeout flags: "
        f"{test_step}"
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
