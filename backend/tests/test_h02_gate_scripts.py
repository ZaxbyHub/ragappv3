"""Issue-tracer H02 acceptance checks — CI gate scripts (#740).

Frozen acceptance spec for the gate-script defects traced by issue #740.
Each test maps to exactly one AC (cited in its docstring). The scripts under
test are loaded from the real ``scripts/`` tree via importlib with unique
module names and bytecode writing disabled, so the repository working tree
is never modified. All temp state lives under pytest ``tmp_path``; no
network, no gh CLI.

AC map (test name -> AC):
  test_sast_c005_requires_run_bandit_inside_sast_job      AC13
  test_run_bandit_fails_on_bandit_parse_errors            AC14
  test_every_ci_gate_script_has_a_test_module             AC15
  test_secretscan_disk_walk_prunes_dependency_trees      AC16
  test_scope_drift_counts_api_deps_as_auth_runtime       AC17
  test_collection_scope_flags_uncollected_underscore_test_file  AC18
  test_secretscan_malformed_glob_is_c1_finding           AC19
  test_missing_git_binary_degrades_gracefully            AC20
  test_gate_git_calls_are_timeout_bounded                AC21
  test_gate_git_path_listing_is_nul_delimited            AC22
  test_runtime_contract_has_no_dead_contributing_regex   AC23
"""

import ast
import contextlib
import importlib.util
import itertools
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()

_SPEC_NAMES = itertools.count()


@contextlib.contextmanager
def _no_bytecode():
    """Keep spec_from_file_location loads from writing scripts/__pycache__."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old


def _load_script(filename: str):
    """Import scripts/<filename> as a fresh module (no repo-tree writes)."""
    path = SCRIPTS / filename
    if not path.is_file():
        pytest.fail(f"scripts/{filename} not found: {path}")
    name = f"_h02_acceptance_{Path(filename).stem}_{next(_SPEC_NAMES)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with _no_bytecode():
        spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def test_sast_c005_requires_run_bandit_inside_sast_job(tmp_path, capsys):
    """#740-AC13: C-005 must require run_bandit.py inside the sast job.

    check_workflow_integrity() accepts any `run:` line anywhere in ci.yml
    mentioning run_bandit.py, so a PR can move the gated scan out of the
    sast job (leaving `echo pass` there) while an unrelated job's mention
    still satisfies the gate. Frozen contract: with the real sast job
    neutered to `echo pass` and run_bandit.py only in an unrelated `lint`
    job, check_workflow_integrity() returns False.
    """
    sast_baseline = _load_script("check_sast_baseline.py")
    ci = tmp_path / "ci.yml"
    ci.write_text(
        # Real ci.yml step shape: `run:` on its own line under `- name:`
        # (the form check_sast_baseline's `^\s*run:` regex matches).
        "jobs:\n"
        "  lint:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Scan elsewhere\n"
        "        run: python scripts/run_bandit.py\n"
        "  sast:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: SAST\n"
        "        run: echo pass\n",
        encoding="utf-8",
        newline="\n",
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sast_baseline, "CI_FILE", ci)
    try:
        ok = sast_baseline.check_workflow_integrity()
    finally:
        monkey.undo()
    captured = capsys.readouterr()
    assert ok is False, (
        "C-005 must fail when the sast job no longer runs run_bandit.py, "
        "even if another job's step mentions it "
        f"(stderr: {captured.err!r})"
    )


def test_run_bandit_fails_on_bandit_parse_errors(tmp_path):
    """#740-AC14: a bandit `errors` array must fail the gated scan.

    bandit reports files it could not parse in the JSON `errors` array (not
    `results`); gated_scan() never reads it, so a syntax-broken tree scans
    as clean. Frozen contract: when _run_bandit_json returns exit 1 with an
    empty results array and a non-empty errors array, gated_scan() returns 1
    (the empty stderr yields no dead-nosec warnings, verified against
    _parse_dead_nosec_warnings).
    """
    run_bandit = _load_script("run_bandit.py")
    assert run_bandit._parse_dead_nosec_warnings("") == []
    baseline = tmp_path / "bandit-baseline.json"
    baseline.write_text(
        json.dumps({"results": [], "metrics": {}, "errors": []}), encoding="utf-8"
    )
    scan_result = (1, {"results": [], "errors": [
        {"filename": "backend/app/x.py", "reason": "syntax error"}
    ]}, "")

    def fake_run_bandit_json(targets, extra):
        return scan_result

    monkey = pytest.MonkeyPatch()
    monkey.setattr(run_bandit, "_run_bandit_json", fake_run_bandit_json)
    monkey.setattr(run_bandit, "BASELINE", baseline)
    try:
        rc = run_bandit.gated_scan()
    finally:
        monkey.undo()
    assert rc == 1, (
        "gated_scan must return 1 when bandit reports parse errors in the "
        f"JSON errors array; got {rc}"
    )


def test_every_ci_gate_script_has_a_test_module():
    """#740-AC15: every ci.yml check_*.py gate must be named by a test.

    A gate script no backend test references can be deleted or broken with
    the suite staying green. Frozen contract: every scripts/check_*.py
    basename invoked by a run: line in .github/workflows/ci.yml appears as
    a substring in at least one file under backend/tests/ (this test file
    excluded — it names the currently-unreferenced scripts itself).
    """
    ci_text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gate_basenames = sorted(set(re.findall(r"scripts/(check_[A-Za-z0-9_]+\.py)", ci_text)))
    assert gate_basenames, "no check_*.py gates parsed from ci.yml"
    other_test_texts = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if path.resolve() != THIS_FILE
    ]
    unreferenced = [
        name
        for name in gate_basenames
        if not any(name in text for text in other_test_texts)
    ]
    assert unreferenced == [], (
        "ci.yml gate scripts with no referencing backend test module "
        f"(a test must name each gate so deletion/breakage is caught): "
        f"{unreferenced}"
    )


def test_secretscan_disk_walk_prunes_dependency_trees(tmp_path):
    """#740-AC16: on_disk_files() must prune node_modules and .venv trees.

    The disk-walk census feeds the stale/overly-broad glob advisories; a
    checkout with frontend/node_modules or backend/.venv present makes those
    advisories iterate thousands of vendored files. Frozen contract: with a
    temp ROOT containing frontend/node_modules/pkg/a.js and
    backend/.venv/lib/b.py, on_disk_files() returns no path under
    node_modules or .venv.
    """
    secretscan = _load_script("check_secretscan.py")
    for rel in ("frontend/node_modules/pkg/a.js", "backend/.venv/lib/b.py", "notes.txt"):
        path = tmp_path / Path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(secretscan, "ROOT", tmp_path)
    try:
        files = secretscan.on_disk_files()
    finally:
        monkey.undo()
    offenders = [p for p in files if "node_modules" in p or ".venv" in p]
    assert offenders == [], (
        f"on_disk_files() must prune dependency trees; leaked: {offenders}"
    )


def test_scope_drift_counts_api_deps_as_auth_runtime(capsys):
    """#740-AC17: backend/app/api/deps.py IS auth runtime for the drift gate.

    The auth-runtime prefix tuple lacks backend/app/api/deps.py, so an auth
    test change paired with an api/deps.py change is reported as drift.
    Frozen contract: changed_files() = [backend/tests/test_auth_x.py,
    backend/app/api/deps.py] => main() returns 0.
    """
    scope_drift = _load_script("check_pr_scope_drift.py")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        scope_drift,
        "changed_files",
        lambda: ["backend/tests/test_auth_x.py", "backend/app/api/deps.py"],
    )
    try:
        rc = scope_drift.main()
    finally:
        monkey.undo()
    captured = capsys.readouterr()
    assert rc == 0, (
        "api deps.py changes must count as auth runtime so paired auth-test "
        f"changes are not drift (rc={rc}, stderr: {captured.err!r})"
    )


def test_collection_scope_flags_uncollected_underscore_test_file(tmp_path, capsys):
    """#740-AC18: a foo_test.py under backend/tests must be flagged.

    backend/pyproject.toml pins python_files = ["test_*.py"], so a
    *_test.py file inside backend/tests/ matches the C-TESTSCOPE-1 filename
    heuristic but is never collected by `pytest tests/`. Frozen contract:
    in a temp repo whose backend/pyproject.toml sets
    python_files = [\"test_*.py\"] with a committed backend/tests/foo_test.py,
    find_violations(repo) reports exactly one violation naming that file —
    and the degraded-mode note never printed (git mode must have answered).
    """
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npython_files = ["test_*.py"]\n',
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "backend" / "tests" / "foo_test.py").write_text(
        "def test_placeholder():\n    pass\n", encoding="utf-8", newline="\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "h02@example.com")
    _git(tmp_path, "config", "user.name", "h02 acceptance")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed foo_test.py")
    collection_scope = _load_script("check_test_collection_scope.py")
    violations = collection_scope.find_violations(tmp_path)
    captured = capsys.readouterr()
    assert "git unavailable" not in captured.err, (
        "the git-mode path must have answered (a degraded-mode fallback "
        "would make this leg vacuous)"
    )
    assert len(violations) == 1 and "backend/tests/foo_test.py" in violations[0], (
        "an uncollected *_test.py file under backend/tests/ must be reported "
        f"as exactly one violation; got {violations}"
    )


def test_secretscan_malformed_glob_is_c1_finding(tmp_path, capsys):
    """#740-AC19: a malformed .secretscanignore glob is a C-SSECRETSCAN-1.

    glob_to_regex compiles `[z-a]` into a regex that raises re.error, and
    the exception escapes main() as a traceback instead of a finding. Frozen
    contract: with .secretscanignore containing the real repo content plus
    an appended `[z-a]` line, main() returns 1 with an output line
    containing "C-SSECRETSCAN-1" — never a traceback.
    """
    secretscan = _load_script("check_secretscan.py")
    ignore_file = tmp_path / ".secretscanignore"
    real_content = (REPO / ".secretscanignore").read_text(encoding="utf-8")
    ignore_file.write_text(real_content + "\n[z-a]\n", encoding="utf-8", newline="\n")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(secretscan, "SECRETSCANIGNORE", ignore_file)
    try:
        try:
            rc = secretscan.main()
        except Exception as exc:  # classified below, never a pass
            outcome = "traceback"
            detail = f"{exc.__class__.__name__}: {exc}"
        else:
            captured = capsys.readouterr()
            combined = captured.out + captured.err
            if rc == 1 and any(
                "C-SSECRETSCAN-1" in line for line in combined.splitlines()
            ):
                outcome = "c1"
                detail = combined
            else:
                outcome = "other"
                detail = f"rc={rc}, output={combined!r}"
    finally:
        monkey.undo()
    assert outcome == "c1", (
        "a malformed glob line must surface as a C-SSECRETSCAN-1 finding "
        f"(rc 1, finding line naming it); outcome was {outcome!r}: {detail}"
    )


def test_missing_git_binary_degrades_gracefully(capsys):
    """#740-AC20: gate scripts must survive a missing git binary.

    check_sast_baseline and check_secretscan call subprocess.run(["git",
    ...]) unguarded, so on a box without git the gate crashes with
    FileNotFoundError instead of degrading. Frozen contract: with
    subprocess.run raising FileNotFoundError for every git argv, neither
    module's main() raises.
    """
    sast_baseline = _load_script("check_sast_baseline.py")
    secretscan = _load_script("check_secretscan.py")
    real_run = subprocess.run

    def git_denying_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if argv and argv[0] == "git":
            raise FileNotFoundError("git binary unavailable (simulated)")
        return real_run(*args, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(subprocess, "run", git_denying_run)
    raised = 0
    try:
        for module in (sast_baseline, secretscan):
            try:
                module.main()
            except FileNotFoundError:
                raised += 1
    finally:
        monkey.undo()
    captured = capsys.readouterr()
    assert raised == 0, (
        "gate main() functions must degrade (not raise FileNotFoundError) "
        f"when the git binary is missing; {raised} raised "
        f"(output: {captured.out!r} {captured.err!r})"
    )


def test_gate_git_calls_are_timeout_bounded():
    """#740-AC21: every gate-script git invocation must pass a timeout.

    Unbounded git calls can hang a CI gate until the job timeout. Frozen
    contract: across the git paths of check_sast_baseline (run_git,
    changed_files, and check_baseline_expansion's git-status call),
    check_pr_scope_drift (changed_files/run_git) and check_secretscan
    (tracked_files), no module issues a git subprocess call without a
    finite timeout kwarg.
    """
    sast_baseline = _load_script("check_sast_baseline.py")
    scope_drift = _load_script("check_pr_scope_drift.py")
    secretscan = _load_script("check_secretscan.py")
    real_run = subprocess.run
    unbounded: list[str] = []
    current_module = [""]

    def recording_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        timeout = kwargs.get("timeout")
        bounded = (
            isinstance(timeout, (int, float))
            and not isinstance(timeout, bool)
            and math.isfinite(timeout)
        )
        proc = real_run(*args, **kwargs)
        if argv and argv[0] == "git" and not bounded:
            unbounded.append(current_module[0])
        return proc

    def _phase(module_name: str, fn) -> None:
        current_module[0] = module_name
        fn()

    monkey = pytest.MonkeyPatch()
    monkey.setattr(subprocess, "run", recording_run)
    # Force the base-ref candidates to be walked (first candidate fails to
    # resolve, so changed_files falls through to the remaining candidates).
    monkey.setenv("PR_SCOPE_DRIFT_BASE", "no-such-ref-h02-acceptance")
    monkey.delenv("GITHUB_BASE_REF", raising=False)
    try:
        _phase("check_sast_baseline", lambda: sast_baseline.run_git(["--version"]))
        _phase("check_sast_baseline", sast_baseline.changed_files)
        _phase("check_sast_baseline", lambda: sast_baseline.check_baseline_expansion([]))
        _phase("check_pr_scope_drift", scope_drift.changed_files)
        _phase("check_secretscan", secretscan.tracked_files)
    finally:
        monkey.undo()
    offenders = sorted(set(unbounded))
    assert offenders == [], (
        "gate scripts issued git subprocess calls without a finite timeout "
        f"kwarg: {offenders}"
    )


def test_gate_git_path_listing_is_nul_delimited(tmp_path):
    """#740-AC22: git path listings must be NUL-delimited (no C-quoting).

    With core.quotePath (git's default), `git ls-files` / `git diff
    --name-only` emit non-ASCII paths C-quoted ("docs/r\\303\\251sumé.env"
    literal with quotes and backslash escapes), which the gates then compare
    against real paths. Frozen contract: in a temp repo tracking
    docs/résumé.env, check_secretscan.tracked_files(),
    check_pr_scope_drift.changed_files() and check_sast_baseline.changed_files()
    (PR_SCOPE_DRIFT_BASE=<parent sha> so the diff path runs) each return the
    path with no C-quoted backslash escape and no surrounding double quote.
    """
    secretscan = _load_script("check_secretscan.py")
    scope_drift = _load_script("check_pr_scope_drift.py")
    sast_baseline = _load_script("check_sast_baseline.py")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "placeholder.txt").write_bytes(b"x")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "h02@example.com")
    _git(tmp_path, "config", "user.name", "h02 acceptance")
    _git(tmp_path, "config", "core.quotePath", "true")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "docs" / "résumé.env").write_bytes(b"k=v")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "add resume env")
    parent_sha = _git(tmp_path, "rev-parse", "HEAD~1").stdout.strip()

    def _is_c_quoted(paths: list[str]) -> bool:
        return any(
            "\\" in p or p.startswith('"') or p.endswith('"') for p in paths
        )

    results: dict[str, list[str]] = {}
    monkey = pytest.MonkeyPatch()
    monkey.setenv("PR_SCOPE_DRIFT_BASE", parent_sha)
    monkey.delenv("GITHUB_BASE_REF", raising=False)
    try:
        monkey.setattr(secretscan, "ROOT", tmp_path)
        results["check_secretscan"] = secretscan.tracked_files()
        monkey.setattr(scope_drift, "ROOT", tmp_path)
        results["check_pr_scope_drift"] = scope_drift.changed_files()
        monkey.setattr(sast_baseline, "ROOT", tmp_path)
        results["check_sast_baseline"] = sast_baseline.changed_files()
    finally:
        monkey.undo()
    offenders = sorted(name for name, paths in results.items() if _is_c_quoted(paths))
    assert offenders == [], (
        "gate git path listings must use NUL-delimited output so non-ASCII "
        "paths are not C-quoted; offenders returned quoted forms: "
        f"{ {name: results[name] for name in offenders} }"
    )


def test_runtime_contract_has_no_dead_contributing_regex():
    """#740-AC23: CONTRIBUTING_NODE_RE must not be a dead constant.

    check_contributing() compiles its own local node regex, so the
    module-level CONTRIBUTING_NODE_RE is declared but never loaded — a
    reader-changing constant with no effect. Frozen contract: the constant
    is not dead (declared at module level AND loaded inside some function).
    """
    source = (SCRIPTS / "check_runtime_contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = False
    loaded = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CONTRIBUTING_NODE_RE"
            for target in node.targets
        ):
            declared = True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Name)
                    and sub.id == "CONTRIBUTING_NODE_RE"
                    and isinstance(sub.ctx, ast.Load)
                ):
                    loaded = True
    dead_constant = declared and not loaded
    assert dead_constant is False, (
        "CONTRIBUTING_NODE_RE is declared at module level but never loaded "
        "inside any function — a dead constant that misleads readers"
    )
