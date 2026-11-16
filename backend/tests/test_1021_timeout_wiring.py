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


def _step_body(ci: str, step_name: str) -> str:
    """The full text of one named ci.yml step (name line to the next name)."""
    lines = ci.splitlines()
    for index, line in enumerate(lines):
        if f"- name: {step_name}" in line:
            body: list[str] = [line]
            for follow in lines[index + 1 :]:
                if follow.strip().startswith("- name:"):
                    break
                body.append(follow)
            return "\n".join(body)
    return ""


def _backend_test_step_run_lines(ci: str) -> list[str]:
    """The Backend job's ``Test`` step run line(s) — job- AND step-scoped.

    tests-falsifiability-001: the previous whole-file scan matched ANY run
    line mentioning ``tests/`` and the watchdog anywhere in ci.yml, so a
    second watchdog-shaped step (or a canary copy on a ``run:`` line) could
    satisfy the pin without the Backend Test step carrying it. Scope to the
    ``backend:`` job block (indent-2 key), then to its ``- name: Test`` step
    (indent-6 list item), and return that step's run lines only. The canary
    legs remain excluded (their pytest calls live inside a bash block, not on
    ``run:`` lines).
    """
    lines = ci.splitlines()
    job_start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == "  backend:"), None
    )
    if job_start is None:
        return []
    job_end = next(
        (
            i
            for i in range(job_start + 1, len(lines))
            if lines[i].strip() and (len(lines[i]) - len(lines[i].lstrip())) <= 2
        ),
        len(lines),
    )
    step_start = next(
        (
            i
            for i in range(job_start, job_end)
            if lines[i].rstrip() == "      - name: Test"
        ),
        None,
    )
    if step_start is None:
        return []
    # Round 4 Important-3: the step ends at the NEXT step list item of ANY
    # shape (`- name:`, `- run:`, `- uses:`, ...) — an unnamed following step
    # must terminate the scan, or its run line could satisfy the pin after
    # the real Test step loses the wrapper. The start match is EXACT
    # (`Test`), so a `Test setup` step can never satisfy the pin either.
    step_end = next(
        (
            i
            for i in range(step_start + 1, job_end)
            if lines[i].startswith("      - ")
        ),
        job_end,
    )
    return [
        ln
        for ln in lines[step_start:step_end]
        if ln.strip().startswith("run:") and "tests/" in ln
    ]


def _missing_flags(text: str) -> list[str]:
    return [flag for flag in _CANONICAL_FLAGS if flag not in text]


def _watchdog_budget_args(line: str) -> dict[str, list[str]]:
    """The watchdog budget arguments parsed from a watchdog-wrapped run line:
    exactly the tokens between the watchdog script and the ` -- ` separator,
    with `--flag=value` tokens split into flag+value (argparse accepts both
    spellings, so an equals-form override like `--poll-interval=50` must be
    visible to the exact-value pin), collected as flag -> list of values
    (duplicates preserved) so drift and duplicate-override shapes are
    detectable."""
    head = line.split(_WATCHDOG_SCRIPT, 1)[1]
    raw_argv = head.split(" -- ", 1)[0].split()
    watchdog_argv: list[str] = []
    for tok in raw_argv:
        if tok.startswith("--") and "=" in tok:
            flag, value = tok.split("=", 1)
            watchdog_argv.extend([flag, value])
        else:
            watchdog_argv.append(tok)
    budgets: dict[str, list[str]] = {
        "--quiet-window": [],
        "--poll-interval": [],
        "--hard-deadline": [],
    }
    argv = iter(range(len(watchdog_argv)))
    for i in argv:
        if watchdog_argv[i] in budgets:
            try:
                budgets[watchdog_argv[i]].append(watchdog_argv[i + 1])
            except IndexError:
                pass
    return budgets


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
        or _WATCHDOG_SCRIPT in line
    ]
    assert run_lines, "nightly has no pytest run lines (plain or wrapped)"
    # EVERY full-suite pytest invocation in nightly (plain AND
    # watchdog-wrapped) must carry the flags — an any() over the line set
    # would let a revert of one line hide behind the other (probe-proven).
    unflagged = [line for line in run_lines if _THREAD_FLAGS not in line]
    assert not unflagged, (
        f"nightly pytest run lines lost the hang-class timeout flags: "
        f"{unflagged!r}"
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


def _site_lines(text: str, site: str) -> list[str]:
    """Extract the DECLARED command-site line(s) for a mirror (implementation-
    review round 2: associate the canonical string with the actual site, not
    just any occurrence in the file)."""
    lines = text.splitlines()
    if site == "ci.yml Backend Test step":
        return _backend_test_step_run_lines(text)
    if site == "justfile backend-test recipe":
        return [ln for ln in lines if ln.strip().startswith("cd backend") and "--timeout=300" in ln]
    if site == "docs/engineering/testing.md":
        return [ln for ln in lines if "- **Backend job:**" in ln]
    if site == ".agents/skills/ci-compatibility-audit/SKILL.md":
        return [
            ln
            for ln in lines
            if (ln.lstrip().startswith("- `pytest ") and "--timeout=300" in ln)
            or (ln.startswith("cd backend && ruff check . && pytest") and "tests/" in ln)
        ]
    if site == ".agents/skills/ci-fix-monitor/SKILL.md":
        return [
            ln
            for ln in lines
            if (ln.startswith("| **Backend** |") and "pytest" in ln)
            or (ln.startswith("cd backend && pytest") and "tests/" in ln)
        ]
    raise AssertionError(f"unknown site {site}")


def test_canonical_flags_at_every_declared_mirror():
    """#1038 Item A: every declared mirror SITE carries the canonical invocation.

    Site-aware (implementation-review rounds 1+2): the canonical string is
    asserted on the DECLARED command line itself (the ci.yml run line, the
    justfile recipe line, the testing.md Backend bullet, the ci-compat-audit
    CI-step + local-fence lines, the ci-fix-monitor job-row + reproduction
    lines) — an extra canonical copy in a comment can never satisfy a drifted
    site. The whole-file count stays as a backstop. A miss fails naming the
    site, the expected/observed site counts, and each missing flag.
    """
    sites = {
        "ci.yml Backend Test step": (_read(".github", "workflows", "ci.yml"), 1),
        "justfile backend-test recipe": (_read("justfile"), 1),
        "docs/engineering/testing.md": (_read("docs", "engineering", "testing.md"), 1),
        ".agents/skills/ci-compatibility-audit/SKILL.md": (
            _read(".agents", "skills", "ci-compatibility-audit", "SKILL.md"),
            2,  # the CI step description + the Local Mirror Commands fence
        ),
        ".agents/skills/ci-fix-monitor/SKILL.md": (
            _read(".agents", "skills", "ci-fix-monitor", "SKILL.md"),
            2,  # the job-row table + the full-suite reproduction block
        ),
    }
    for site, (text, expected_sites) in sites.items():
        site_lines = _site_lines(text, site)
        canonical_sites = [
            ln for ln in site_lines if _CANONICAL_INVOCATION in ln
        ]
        missing = _missing_flags("\n".join(site_lines))
        assert len(canonical_sites) >= expected_sites and not missing, (
            f"{site} drifted from the canonical Backend invocation: "
            f"{len(canonical_sites)} of >= {expected_sites} declared command "
            f"site(s) carry it (whole-file occurrences: "
            f"{text.count(_CANONICAL_INVOCATION)}); missing flag(s) at the "
            f"declared site(s): {', '.join(missing) or 'flag order/shape'}"
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
    # tests-falsifiability-006 (round 5): pin the PRODUCTION watchdog budgets
    # as EXACT single values — substring checks accept `--quiet-window 3600`
    # (contains "360") and duplicate-override shapes. The parser preserves
    # duplicates, so an override duplicate fails the exact-equality pin too.
    budgets = _watchdog_budget_args(wrapped[0])
    assert budgets == {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }, (
        "ci.yml Backend Test step watchdog budget drifted from production "
        f"pin (quiet-window 360 / poll-interval 5 / hard-deadline 2700, "
        f"each exactly once): parsed {budgets}"
    )
    assert _WATCHDOG_HOOK_SCRIPT in ci, (
        "ci.yml lost the watchdog faulthandler hook step "
        f"(scripts/{_WATCHDOG_HOOK_SCRIPT} is no longer copied)"
    )
    assert _WATCHDOG_HOOK_STEP_NAME in ci, (
        "ci.yml lost the 'Write Backend watchdog faulthandler hook (#1021)' step"
    )
    # Final-critic round 1: the Backend job's default working directory is
    # backend/, so the hook-copy source must be reachable FROM THERE — a bare
    # `scripts/...` cp fails on every CI run (the hook lives at the repo
    # root). Pin the ../ path shape (or an explicit working-directory override).
    hook_step_body = _step_body(ci, _WATCHDOG_HOOK_STEP_NAME)
    assert (
        f"cp ../scripts/{_WATCHDOG_HOOK_SCRIPT}" in hook_step_body
        or "working-directory: ." in hook_step_body
    ), (
        "ci.yml hook step copies the hook with a path that does not resolve "
        "from the Backend job's working directory (backend/) — use "
        f"'cp ../scripts/{_WATCHDOG_HOOK_SCRIPT}' or set working-directory: ."
    )
    for key in _WATCHDOG_ENV_KEYS:
        assert f"{key}:" in ci, (
            f"ci.yml Backend Test step lost the {key} env (watchdog stack "
            "capture wiring)"
        )


def test_backend_test_step_locator_rejects_lookalike_steps():
    """tests-falsifiability-001 (round 4 Important-3 negative fixtures): the
    locator must not be satisfied by a differently-named step carrying the
    watchdog line, nor leak past the real Test step into an unnamed
    following step. Both fixtures: the real Test step LOST its watchdog run
    line; a lookalike step carries it — the pin must FAIL (no lines found
    carrying the watchdog), never pass on the impostor."""
    watchdog_run = (
        "        run: python ../scripts/ci_backend_watchdog.py "
        "--quiet-window 360 ... tests/"
    )
    plain_run = "        run: pytest --tb=short ... tests/"
    # Fixture A: `Test setup` (prefix lookalike) carries the watchdog line;
    # the real Test step is plain.
    fixture_a = "\n".join(
        [
            "jobs:",
            "  backend:",
            "    steps:",
            "      - name: Test setup",
            watchdog_run,
            "      - name: Test",
            plain_run,
            "      - name: After",
            "        run: echo done",
        ]
    )
    assert _backend_test_step_run_lines(fixture_a) == [plain_run], (
        "a `Test setup` lookalike must not satisfy the Test-step pin"
    )
    # Fixture B: the real Test step is plain; an UNNAMED following step
    # (`- run:`) carries the watchdog line — the scan must stop there.
    fixture_b = "\n".join(
        [
            "jobs:",
            "  backend:",
            "    steps:",
            "      - name: Test",
            plain_run,
            "      - run: python ../scripts/ci_backend_watchdog.py ... tests/",
            "      - name: After",
            "        run: echo done",
        ]
    )
    assert _backend_test_step_run_lines(fixture_b) == [plain_run], (
        "an unnamed following step must not satisfy the Test-step pin"
    )


def test_watchdog_budget_pin_rejects_drift():
    """tests-falsifiability-006 (round 5 negative fixtures): the exact-value
    budget pin must REJECT drifted values (`--quiet-window 3600` contains
    the substring "360") and duplicate-override shapes (`--quiet-window 360
    --quiet-window 60`) — the substring pin accepted both."""
    base = (
        "        run: python ../scripts/ci_backend_watchdog.py "
        "--quiet-window 360 --poll-interval 5 --hard-deadline 2700 -- pytest tests/"
    )
    drifted = base.replace(
        "--quiet-window 360", "--quiet-window 3600"
    )
    assert _watchdog_budget_args(drifted) != {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }, "a drifted --quiet-window 3600 must fail the exact-value pin"
    duplicated = base.replace(
        "--poll-interval 5",
        "--poll-interval 5 --poll-interval 50",
    )
    assert _watchdog_budget_args(duplicated) != {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }, "a duplicate-override --poll-interval must fail the exact-value pin"
    equals_form = base.replace("--poll-interval 5", "--poll-interval=50")
    assert _watchdog_budget_args(equals_form) != {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }, (
        "an equals-form override (--poll-interval=50) must fail the "
        "exact-value pin"
    )
    # Control: the exact production line parses to the pin.
    assert _watchdog_budget_args(base) == {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }


_NIGHTLY_HOOK_STEP_NAME = "Write watchdog faulthandler hook (full-deps tier, #1021)"
_NIGHTLY_FULL_SUITE_STEP_NAME = (
    "Full backend suite (plain full run = cross-file isolation check)"
)
_NIGHTLY_CANONICAL = (
    "pytest --tb=short -v --timeout=300 --timeout-method=thread -rs -n auto tests/"
)


def _nightly_step_body(nightly: str, step_name: str) -> str:
    """The full text of one NAMED nightly step (exact name line to the next
    step list item of any shape) — step-scoped, so a watchdog-shaped line in
    an impostor step can never satisfy a pin meant for the full-suite step
    (review round 7)."""
    lines = nightly.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == f"      - name: {step_name}":
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("      - "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_nightly_full_suite_wrapped_in_watchdog():
    """compat-delivery-007 closed IN-PR: the nightly full-deps full-suite
    step runs under the SAME out-of-process watchdog as the PR-gate Backend
    Test step — same budgets, same SIGUSR2 hook wiring, and -v -rs so the
    watchdog's pending-test banner can name ids from verbose dispatch
    records. Every check is scoped to the NAMED step (round 7): an impostor
    step carrying a watchdog-shaped line cannot satisfy the pin."""
    nightly = _read(".github", "workflows", "nightly.yml")
    suite_step = _nightly_step_body(nightly, _NIGHTLY_FULL_SUITE_STEP_NAME)
    assert suite_step, "nightly full-suite step not found by exact name"
    wrapped = [ln for ln in suite_step.splitlines() if _WATCHDOG_SCRIPT in ln]
    assert wrapped, (
        "nightly full-suite step lost the #1021 watchdog wrapper"
    )
    budgets = _watchdog_budget_args(wrapped[0])
    assert budgets == {
        "--quiet-window": ["360"],
        "--poll-interval": ["5"],
        "--hard-deadline": ["2700"],
    }, (
        "nightly watchdog budgets drifted from the production pin "
        f"(360/5/2700, each exactly once): parsed {budgets}"
    )
    assert _NIGHTLY_CANONICAL in wrapped[0], (
        "nightly watchdog wrapping must keep the nightly canonical pytest "
        f"flags verbatim: {wrapped[0].strip()}"
    )
    hook_step = _nightly_step_body(nightly, _NIGHTLY_HOOK_STEP_NAME)
    assert hook_step, "nightly hook step not found by exact name"
    assert f"cp ../scripts/{_WATCHDOG_HOOK_SCRIPT}" in hook_step, (
        "nightly hook step copies the hook with a path that does not "
        "resolve from the job's backend/ working directory"
    )
    for key in _WATCHDOG_ENV_KEYS:
        assert f"{key}:" in suite_step, (
            f"nightly full-suite step lost the {key} env (watchdog stack "
            "capture wiring)"
        )


def test_nightly_watchdog_pin_rejects_impostor_step():
    """review round 7 negative fixture: when the REAL full-suite step loses
    the wrapper, a watchdog-shaped line in a differently-named step can
    NEVER satisfy the pin — the scoped helper finds nothing in the named
    step even though the string appears elsewhere in the file."""
    impostor = "\n".join(
        [
            "jobs:",
            "  full-deps:",
            "    steps:",
            "      - name: Lint",
            "        run: ruff check .",
            "      - name: Watchdog impostor",
            "        run: python ../scripts/ci_backend_watchdog.py "
            "--quiet-window 360 --poll-interval 5 --hard-deadline 2700 -- pytest tests/",
            "      - name: Full backend suite (plain full run = cross-file isolation check)",
            "        run: pytest --tb=short -q tests/",
        ]
    )
    suite_step = _nightly_step_body(
        impostor, "Full backend suite (plain full run = cross-file isolation check)"
    )
    assert suite_step, "the real step must be located by exact name"
    assert not [ln for ln in suite_step.splitlines() if "ci_backend_watchdog" in ln], (
        "the impostor's watchdog line must not leak into the real step's body"
    )
