"""Issue #655 regression tests — the runtime-contract gate's docs surfaces.

Pins the three contract-hygiene repairs from the 2026-09-22 frontier audit:

* D1 — ``docs/engineering/conventions.md`` carries no stale runtime pins
  (Node/Python against ALLOWED_RUNTIME; Vitest/Vite majors against the live
  ``frontend/package.json``), and re-introducing one is flagged.
* D4 — ``docs/engineering/testing.md`` and ``conventions.md`` name every ci.yml
  job and (testing.md) all six ``scripts/check_*.py`` quality contracts; removing
  one is flagged.
* D6 — ``scripts/check_secretscan.py`` states on the success path that the
  ``secretscan`` scanner itself is not run by any CI job, and its docstring
  scopes what a PASS means.

The checker logic is exercised through the same module-level pure function the
frozen acceptance drivers use (``docs_surface_failures(conventions_text,
testing_text, ci_text)``), loaded via importlib because ``scripts/`` is not an
importable package. Script-level behavior (exit codes, output text) runs via
subprocess with ``sys.executable``, following the
``backend/tests/test_issue258_build_contracts.py`` pattern. Dependencies:
stdlib + pytest + PyYAML (already pinned in backend/requirements-lock-ci.txt);
nothing shells out to node.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_runtime_contract.py"
SECRETSCAN = REPO / "scripts" / "check_secretscan.py"
STALE_NODE_BULLET = (
    "- CI pins Node 20.19.0; regenerate lockfiles with the same Node release "
    "so the bundled npm version matches CI.\n"
)
MUTATED_JOB_NAME = "Docker build smoke"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_runtime_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _run_script(path: Path):
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _conventions_failures(module, conventions_text: str) -> list[str]:
    return module.docs_surface_failures(
        conventions_text, _doc("docs/engineering/testing.md"), _ci_text()
    )


def _testing_failures(module, testing_text: str) -> list[str]:
    return module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"), testing_text, _ci_text()
    )


def _ci_text() -> str:
    return _doc(".github/workflows/ci.yml")


def _ci_job_names() -> list[str]:
    workflow = yaml.safe_load(_ci_text())
    jobs = workflow.get("jobs") or {}
    return [job.get("name") or key for key, job in jobs.items()]


# ── D1: conventions.md runtime pins ─────────────────────────────────────────


def test_conventions_md_has_no_stale_runtime_pins():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    clean = [
        f
        for f in module.docs_surface_failures(
            real, _doc("docs/engineering/testing.md"), _ci_text()
        )
        if "conventions.md" in f
    ]
    assert not clean, f"repaired conventions.md must carry no pin violations: {clean}"

    failures = _conventions_failures(module, real + "\n" + STALE_NODE_BULLET)
    assert any(
        "conventions.md" in f and "Node" in f for f in failures
    ), f"stale Node pin re-introduction must be flagged with the doc and the pin in one message: {failures}"


def test_conventions_md_natural_language_pin_forms_flagged():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    vitest_forms = [
        "- Lockfiles need Vitest 4.x and Vite >= 6.\n",
        "- Lockfiles need vitest ^4.0.0 (package.json style).\n",
        "- Lockfiles need Vitest ~4.0.0 (tilde range).\n",
    ]
    for stale in vitest_forms:
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any(
            "conventions.md" in f and "vitest major" in f for f in failures
        ), f"stale vitest pin form must be flagged with the vitest major named: {stale!r} -> {failures}"
    vite_forms = [
        "- Lockfiles need Vite >= 6.\n",
        "- Lockfiles need vite ~6.0.0 (package.json style).\n",
        "- Lockfiles need vite v6.0.0 (v-prefixed).\n",
    ]
    for stale in vite_forms:
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any(
            "conventions.md" in f and "vite major" in f for f in failures
        ), f"stale vite pin form must be flagged with the vite major named: {stale!r} -> {failures}"
    node_forms = [
        "- CI pins Node.js 20.19.0 for lockfile regeneration.\n",
        "- Any Node 20.x release works for lockfiles.\n",
        "- CI pins Node v20.19.0 for lockfile regeneration.\n",
    ]
    for stale in node_forms:
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any(
            "conventions.md" in f and "Node" in f for f in failures
        ), f"stale Node pin form (incl. v-prefix) must be flagged: {stale!r} -> {failures}"


def test_conventions_md_node_operator_forms_flagged():
    """DOC_NODE_OP_RE branch: operator-form claims are pinned independently."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    stale = "- Any Node >= 20 release works for lockfiles.\n"
    failures = _conventions_failures(module, real + "\n" + stale)
    assert any(
        "conventions.md" in f and "Node" in f for f in failures
    ), f"stale operator-form Node pin must be flagged: {failures}"
    ok = "- CI uses Node >= 22.x for lockfiles.\n"
    failures = _conventions_failures(module, real + "\n" + ok)
    assert not [
        f for f in failures if "conventions.md" in f and "Node" in f
    ], f"a correct operator-form pin (Node >= 22.x) must not be flagged: {failures}"


def test_conventions_md_lists_every_ci_job():
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    missing = [name for name in _ci_job_names() if name not in real]
    assert not missing, f"conventions.md job inventory is missing ci.yml jobs: {missing}"
    trimmed = real.replace(MUTATED_JOB_NAME, "[removed-job]")
    assert trimmed != real, "sanity: the mutated job name must exist in the real doc"
    failures = _conventions_failures(module, trimmed)
    assert any(
        "conventions.md" in f and MUTATED_JOB_NAME in f for f in failures
    ), f"removing a job name from conventions.md must be flagged: {failures}"


# ── D4: testing.md CI gate inventory ────────────────────────────────────────


def test_testing_md_lists_every_ci_job():
    module = _load_checker()
    real = _doc("docs/engineering/testing.md")
    clean = [
        f
        for f in module.docs_surface_failures(
            _doc("docs/engineering/conventions.md"), real, _ci_text()
        )
        if "testing.md" in f
    ]
    assert not clean, f"repaired testing.md must carry no inventory violations: {clean}"

    assert trimmed_failures_contains_job(module, real), (
        "removing a job name from testing.md must be flagged with the doc and "
        "the missing job in one message"
    )


def test_testing_md_lists_every_quality_contract_script():
    """The script-inventory branch must flag a removed quality-contract script.

    Mirror of the job-inventory mutation test: a testing.md copy with one of
    the six ``scripts/check_*.py`` mentions removed must produce a failure
    naming both testing.md and the missing script (PR #663 review R-B3).
    """
    module = _load_checker()
    real = _doc("docs/engineering/testing.md")
    missing_script = "scripts/check_test_collection_scope.py"
    assert missing_script in real, "sanity: the real doc lists the script"
    # testing.md lists the six scripts inline within a single bullet, so the
    # mutation renames the mention rather than deleting a line.
    trimmed = real.replace(missing_script, "scripts/removed_scope_check.py")
    assert missing_script not in trimmed, "sanity: the trimmed copy must drop it"
    failures = _testing_failures(module, trimmed)
    assert any(
        "testing.md" in f and missing_script in f for f in failures
    ), f"removing a quality-contract script from testing.md must be flagged: {failures}"


def trimmed_failures_contains_job(module, real: str) -> bool:
    trimmed = real.replace(MUTATED_JOB_NAME, "[removed-job]")
    failures = _testing_failures(module, trimmed)
    return any(
        "testing.md" in f and MUTATED_JOB_NAME in f for f in failures
    )


# ── D6: the secretscan gate's honest labeling ───────────────────────────────


def test_secretscan_summary_states_scanner_not_wired():
    result = _run_script(SECRETSCAN)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"check_secretscan must still pass: {combined}"
    assert (
        "not run by any CI job" in combined
    ), f"success output must disclose the scanner is not wired: {combined}"


def test_secretscan_docstring_states_gate_scope():
    source = " ".join(SECRETSCAN.read_text(encoding="utf-8").split())
    assert "not that a scan ran" in source, (
        "the docstring must scope what a PASS means until the scanner is wired"
    )


# ── Guard robustness (fail-loud, never crash) ───────────────────────────────


_WIRING_CALL = "    check_docs_surfaces(failures)\n"


def _copied_checker_tree(destination: Path) -> None:
    """Mirror every repo file the checker reads into a temp root.

    ``check_runtime_contract.py`` resolves ``ROOT`` from its own location
    (``parents[1]``), so a mutated copy of the script only behaves like the
    repo gate when the surfaces it reads exist under the same relative paths.
    """
    for relative in (
        "docs/engineering/conventions.md",
        "docs/engineering/testing.md",
        ".github/workflows/ci.yml",
        "frontend/package.json",
        "CONTRIBUTING.md",
        ".devcontainer/devcontainer.json",
        "Dockerfile",
        "frontend/Dockerfile",
    ):
        source = REPO / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


def test_docs_surface_wired_into_main():
    """Integration pin (Phase 4.5 review): the docs surface must stay wired.

    Runs the REAL script from a mirrored temp root with a stale pin injected
    into conventions.md, twice: with the original source (the gate must exit
    non-zero and name conventions.md) and with the ``check_docs_surfaces``
    call removed from ``main()`` (the gate must go blind and exit 0). Proves
    the ``main()`` wiring is load-bearing — deleting the call re-opens D1.
    """
    source = CHECKER.read_text(encoding="utf-8")
    assert _WIRING_CALL in source, (
        "the check_docs_surfaces call disappeared from main() — this test "
        "pins the wiring itself; restore it or update this pin deliberately"
    )

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        _copied_checker_tree(root)
        conventions = root / "docs" / "engineering" / "conventions.md"
        conventions.write_text(
            conventions.read_text(encoding="utf-8") + "\n" + STALE_NODE_BULLET,
            encoding="utf-8",
        )
        script_dir = root / "scripts"
        script_dir.mkdir()

        wired = script_dir / "check_runtime_contract.py"
        wired.write_text(source, encoding="utf-8")
        wired_result = subprocess.run(
            [sys.executable, str(wired)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert wired_result.returncode != 0, (
            "with the wiring present, a stale pin must fail the real gate: "
            f"stdout={wired_result.stdout!r} stderr={wired_result.stderr!r}"
        )
        assert "conventions.md" in wired_result.stderr

        unwired = script_dir / "check_runtime_contract.py"
        unwired.write_text(source.replace(_WIRING_CALL, ""), encoding="utf-8")
        unwired_result = subprocess.run(
            [sys.executable, str(unwired)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert unwired_result.returncode == 0, (
            "with the wiring removed the gate must be blind again (this is "
            "the regression the pin exists to catch): "
            f"stderr={unwired_result.stderr!r}"
        )


def test_docs_surfaces_missing_file_reports_failure():
    module = _load_checker()
    with tempfile.TemporaryDirectory() as scratch:
        original_root = module.ROOT
        module.ROOT = Path(scratch)
        try:
            failures: list[str] = []
            module.check_docs_surfaces(failures)
        finally:
            module.ROOT = original_root
    assert failures, "missing docs surfaces must produce failures"
    assert all("missing" in f for f in failures), (
        f"missing-file failures must be clean missing-surface messages: {failures}"
    )


def test_dep_major_tracks_package_json():
    module = _load_checker()
    package_text = _doc("frontend/package.json")
    assert module._dep_major(package_text, "vitest", []) is not None
    assert module._dep_major(package_text, "vite", []) is not None
    # The helper derives from the live file: a synthetic bump moves the major.
    assert module._dep_major('{"vitest": "~9.0.0"}', "vitest", []) == "9"
    assert module._dep_major('{"vite": "^7.1.2"}', "vite", []) == "7"
    probe: list[str] = []
    assert module._dep_major("{}", "vitest", probe) is None
    assert probe and "frontend/package.json" in probe[0], (
        "a missing dependency must fail loud with a runtime-contract message"
    )


# ── ci.yml job-name parser (final-critic hardening) ─────────────────────────


def test_ci_job_display_names_parser():
    module = _load_checker()
    real = module._ci_job_display_names(_ci_text())
    assert real == [
        "Frontend",
        "Playwright e2e smoke",
        "Quality contracts",
        "Detect docker scope",
        "Docker build smoke",
        "SAST (bandit)",
        "Backend",
    ], f"real ci.yml ground truth changed or the parser regressed: {real}"

    # Quoted scalar values must parse without their quote characters.
    quoted = (
        "name: CI\n"
        "jobs:\n"
        '  frontend:\n    name: "Frontend"\n'
        "  backend:\n    name: 'Backend'\n"
    )
    assert module._ci_job_display_names(quoted) == ["Frontend", "Backend"]

    # Indentation inside jobs: may differ from 4 spaces; every job is found.
    deep = (
        "jobs:\n"
        "      frontend:\n"
        "        name: Frontend\n"
        "      backend:\n"
        "        name: Backend\n"
    )
    assert module._ci_job_display_names(deep) == ["Frontend", "Backend"]

    # A job without a name: falls back to its key.
    unnamed = "jobs:\n  frontend:\n    runs-on: ubuntu-latest\n  backend:\n    name: Backend\n"
    assert module._ci_job_display_names(unnamed) == ["frontend", "Backend"]

    # A quoted-empty name must fall back to the job key too: an empty-string
    # entry would make the substring inventory check vacuous ('' in anything).
    empty = "jobs:\n  frontend:\n    name: ''\n  backend:\n    name: Backend\n"
    names = module._ci_job_display_names(empty)
    assert names == ["frontend", "Backend"] and "" not in names, (
        f"a quoted-empty name: must fall back to the job key, never '' (the "
        f"substring check cannot fail on ''): {names}"
    )

    # A one-of-seven omission can never silently truncate the inventory: with
    # the name: gone the parser falls back to the job key, so the job stays in
    # the ground truth (as 'docker-smoke') and the gate flags the doc naming it.
    mutated = _ci_text().replace("    name: Docker build smoke\n", "    runs-on: ubuntu-latest\n")
    names = module._ci_job_display_names(mutated)
    assert "docker-smoke" in names and "Docker build smoke" not in names and len(names) == 7, (
        f"a name:-less job must fall back to its key so the inventory stays "
        f"complete: {names}"
    )

    # Absent or empty jobs: must fail loud ([]) rather than []-by-drift being
    # indistinguishable — the caller turns [] into a runtime-contract failure.
    assert module._ci_job_display_names("name: CI\n") == []


def test_docs_surface_flags_job_renamed_in_ci():
    """Inventory drift is caught in BOTH directions: if ci.yml renames a job,
    docs still naming the old display name must be flagged — the parser's
    ground truth tracks the workflow, not the docs.
    """
    module = _load_checker()
    ci_mutated = _ci_text().replace(
        "    name: Docker build smoke\n", "    name: Docker build check\n"
    )
    assert "Docker build check" in module._ci_job_display_names(ci_mutated), (
        "sanity: the parser must see the renamed job"
    )
    failures = module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"),
        _doc("docs/engineering/testing.md"),
        ci_mutated,
    )
    assert any(
        "testing.md" in f and "Docker build check" in f for f in failures
    ), f"renaming a ci.yml job must flag the missing new name in the docs: {failures}"
    assert any(
        "conventions.md" in f and "Docker build check" in f for f in failures
    ), f"renaming a ci.yml job must flag both doc inventories: {failures}"


def test_docs_surface_fails_loud_on_unparseable_jobs():
    module = _load_checker()
    failures = module.docs_surface_failures(
        _doc("docs/engineering/conventions.md"),
        _doc("docs/engineering/testing.md"),
        "name: CI\non: push\n",
    )
    assert any("no job names parsed" in f for f in failures), (
        f"an unparseable jobs: mapping must fail loud: {failures}"
    )


# ── PR #663 review follow-ups (swarm-pr-feedback) ────────────────────────────


def test_conventions_md_bare_major_node_pins_flagged():
    """Bare-major Node prose ("Node 20 LTS") is a pin claim: wrong majors fail."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    for stale in [
        "- CI requires Node 20 LTS for lockfile regeneration.\n",
        "- Tooling targets Node v20.\n",
        "- Minimum runtime: node (20).\n",
    ]:
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any(
            "conventions.md" in f and "node major" in f for f in failures
        ), f"a stale bare-major Node claim must be flagged: {stale!r} -> {failures}"
    for ok in [
        "- CI requires Node 22 LTS for lockfile regeneration.\n",
        "- Tooling targets Node v22.\n",
        "- Minimum runtime: node (22).\n",
    ]:
        failures = _conventions_failures(module, real + "\n" + ok)
        assert not [
            f for f in failures if "conventions.md" in f and "node major" in f
        ], f"a correct bare-major Node claim must not be flagged: {ok!r} -> {failures}"
    # A full version is the prose regex's job, never the bare-major regex's.
    failures = _conventions_failures(module, real + "\n- Pin: Node 22.22.0.\n")
    assert not [
        f for f in failures if "node major" in f
    ], f"full versions must not double-report as bare majors: {failures}"


def test_unsupported_yaml_job_shapes_fail_loud():
    """YAML shapes the indentation walk cannot inventory fail loud, never
    silently mis-inventory (block scalars and flow mappings previously
    produced zero failures)."""
    module = _load_checker()
    conventions = _doc("docs/engineering/conventions.md")
    testing = _doc("docs/engineering/testing.md")
    shapes = {
        "block scalar": "jobs:\n  frontend:\n    name: >\n      Frontend\n",
        "flow mapping": "jobs:\n  frontend: { name: Frontend, runs-on: x }\n",
        "list form": "jobs:\n  - name: Frontend\n    runs-on: x\n",
        "anchor name": "jobs:\n  frontend:\n    name: &f Frontend\n",
        "tab indent": "jobs:\n\tfrontend:\n    name: Frontend\n",
    }
    for label, ci_text in shapes.items():
        failures = module.docs_surface_failures(conventions, testing, ci_text)
        assert any("unparseable" in f for f in failures), (
            f"{label} jobs: shape must fail loud as unparseable: {failures}"
        )


def test_name_trailing_comment_and_apostrophe_are_decoded():
    """YAML details a maintainer will actually type: trailing comments are
    stripped and single-quote escaping is decoded."""
    module = _load_checker()
    assert module._ci_job_display_names(
        "jobs:\n  frontend:\n    name: Frontend  # the web build\n"
    ) == ["Frontend"]
    assert module._ci_job_display_names(
        "jobs:\n  frontend:\n    name: 'O''Reilly Frontend'\n"
    ) == ["O'Reilly Frontend"]
    # The real workflow still parses to its documented inventory.
    ci_text = _doc(".github/workflows/ci.yml")
    names = module._ci_job_display_names(ci_text)
    assert "Quality contracts" in names and "Docker build smoke" in names


def test_conventions_md_lists_every_contract_script():
    """The conventions.md contract-script inventory is gated too (it claimed
    two of six scripts while CI runs six)."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    dropped = real.replace("`scripts/check_secretscan.py`", "`scripts/check_ghost.py`")
    assert dropped != real, "test fixture must actually drop a script mention"
    failures = module.docs_surface_failures(
        dropped, _doc("docs/engineering/testing.md"), _ci_text()
    )
    assert any(
        "conventions.md" in f and "check_secretscan.py" in f for f in failures
    ), f"a script missing from conventions.md must be flagged: {failures}"
    failures = module.docs_surface_failures(real, _doc("docs/engineering/testing.md"), _ci_text())
    assert not [
        f for f in failures if "conventions.md" in f and "contract script" in f
    ], f"the real conventions.md lists every contract script: {failures}"


def test_node_op_form_duplicate_mention_reported_once():
    """A repeated operator-form Node mention yields one failure line, matching
    the prose loop's dedup (the module's one-line-per-mismatch contract)."""
    module = _load_checker()
    repeated = (
        "- Backoff: Use Node >= 20.19.0 in tooling.\n"
        "- Again: Use Node >= 20.19.0 in tooling.\n"
    )
    failures = _conventions_failures(module, repeated)
    node_lines = [f for f in failures if "Node >= 20.19" in f]
    assert len(node_lines) == 1, (
        f"a repeated mention must report once, got {len(node_lines)}: {node_lines}"
    )


def test_docs_surface_unreadable_file_fails_clean():
    """A non-UTF-8 docs surface fails with a runtime-contract: line, not a raw
    UnicodeDecodeError traceback."""
    module = _load_checker()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "docs" / "engineering").mkdir(parents=True)
        (root / "docs" / "engineering" / "conventions.md").write_bytes(b"\xff\xfe bad")
        (root / "docs" / "engineering" / "testing.md").write_text("x\n", encoding="utf-8")
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\njobs:\n  backend:\n    name: Backend\n", encoding="utf-8"
        )
        original_root = module.ROOT
        module.ROOT = root
        try:
            failures: list[str] = []
            module.check_docs_surfaces(failures)
        finally:
            module.ROOT = original_root
    assert any("unreadable" in f for f in failures), (
        f"an undecodable docs surface must fail clean: {failures}"
    )


def test_python_space_less_and_newline_forms_flagged():
    """`Python3.14` (space-less — the real binary name) and newline-separated
    mentions are pin claims and must not pass silently."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    allowed = module.ALLOWED_RUNTIME["python"]["version"]
    stale_major, stale_minor = ("3", "14") if allowed != "3.14" else ("3", "99")
    for stale in [
        f"- Tooling assumes Python{stale_major}.{stale_minor} only.\n",
        f"- We standardize on Python\n  {stale_major}.{stale_minor} for tooling.\n",
    ]:
        failures = _conventions_failures(module, real + "\n" + stale)
        assert any(
            "conventions.md" in f and "python" in f.lower() for f in failures
        ), f"a space-less/newline python pin must be flagged: {stale!r} -> {failures}"


def test_comment_mention_is_flagged_by_design():
    """Runtime pins inside HTML comments are flagged like live claims — a
    deliberate, documented scope decision, pinned here so it cannot drift."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    failures = _conventions_failures(
        module, real + "\n<!-- historical note: CI once pinned Node 20.19.0 -->\n"
    )
    assert any(
        "conventions.md" in f and "Node 20.19" in f for f in failures
    ), f"a commented stale pin is still a pin: {failures}"


def test_quoted_job_name_with_hash_is_not_comment_stripped():
    """A `#` inside a quoted job name is part of the name, not a comment."""
    module = _load_checker()
    assert module._ci_job_display_names(
        'jobs:\n  frontend:\n    name: "Build #123"\n'
    ) == ["Build #123"]
    assert module._ci_job_display_names(
        "jobs:\n  frontend:\n    name: 'Build #7'\n"
    ) == ["Build #7"]
    # Unquoted comments are still stripped.
    assert module._ci_job_display_names(
        "jobs:\n  frontend:\n    name: Frontend # the web build\n"
    ) == ["Frontend"]


def test_wildcard_node_claim_reports_once():
    """A `.x` wildcard claim is the prose regex's job; the bare-major regex
    must not double-report it."""
    module = _load_checker()
    real = _doc("docs/engineering/conventions.md")
    failures = _conventions_failures(module, real + "\n- Any Node 20.x works.\n")
    lines = [f for f in failures if "Node 20" in f]
    assert len(lines) == 1, (
        f"a wildcard claim must report exactly once, got {len(lines)}: {lines}"
    )


def test_escaped_quotes_inside_double_quoted_name():
    """Backslash escapes do not close a double-quoted scalar (PyYAML parity)."""
    module = _load_checker()
    assert module._ci_job_display_names(
        'jobs:\n  frontend:\n    name: "Build \\"x\\" #1"\n'
    ) == ['Build "x" #1']
    assert module._ci_job_display_names(
        'jobs:\n  frontend:\n    name: "Build #123"  # real comment\n'
    ) == ["Build #123"]
