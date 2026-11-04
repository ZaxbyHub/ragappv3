"""Acceptance checks for issue #746 (Workstream H PR 9): guardrails and operator-facing checks must be able to detect their own target defect. RED at base where the blind spot is live, GREEN after the fix.

Arm's-length checks, one per #746 acceptance criterion (AC1-AC4, AC6-AC8;
AC5 is HOST_ONLY per the issue and AC9 is the preserving run of the suites
these checks inspect — both live in the issue's own argv):

* AC1 (T1-28-K-03) — the CI restore drill must be able to fail on a data
  tree with NO draft-room directory. DRIFT PIN: the product side is already
  fixed at master (commit e037a97c / PR #1026: a missing draft-room is
  skipped, an empty tree is recorded ``empty: true``), so this drill runs
  GREEN at base and pins the tolerance.
* AC2 (T1-28-K-03) — same harness, with the only vault directory existing
  but EMPTY. DRIFT PIN: GREEN at base for the same reason.
* AC3 (T1-29-KR-09) — DOC-003b's ``model_pull_findings`` must evaluate
  commented example lines (``# CHAT_MODEL=...``), not just the two compose
  placeholder forms it currently matches then skips. RED at base: 0 == 1.
* AC4 (TQ-differential-e-09) — the only seq-uniqueness concurrency test
  must not carry an unconditional win32 ``skipif``. RED at base: 1 == 0.
* AC6 (TQ-sibling-batch-01-04) — the #652 hardcoded-timeout guardrail's
  census must flag positional and attribute-qualified ``LLMClient`` calls.
  RED at base: 0 == 2.
* AC7 (TQ-sibling-batch-04-02) — the coverage-design pin must be comment
  aware: an ``include`` entry present only inside a JS comment must fail a
  design test. RED at base: 0 AssertionErrors against the mutated config.
* AC8 (RT-S4-01) — CONTRIBUTING.md's Windows section must name the
  python-magic / msys-magic DLL seam. RED at base: 0 mentions.
"""

import ast
import importlib.util
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent

# The scripts/ import seam, copied from test_e01_backup_set_default_layout.py
# (scripts.restore / scripts.backup_set / check_installation_doc resolve via
# the repo root being importable).
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.restore import RestoreError, restore_backup_set  # noqa: E402


def _load_test_module(path: Path, name: str):
    """Import a sibling test module by path under a private name so its
    helpers can be reused without polluting sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


# ---------------------------------------------------------------------------
# AC1/AC2 — the restore drill must see empty/missing tree inputs (pins)
# ---------------------------------------------------------------------------


def _run_restore_drill(tmp_path: Path, mutate_tree) -> tuple:
    """Build test_518_restore.py's source tree, apply ``mutate_tree(src)``
    BEFORE the backup, then back up and restore through the frozen helpers.

    Returns ``(err, message)``: ``err`` is ``type(e).__name__`` when the
    restore raised ``RestoreError`` (else ``None``) and ``message`` the
    exception text (else ``None``)."""
    drill = _load_test_module(
        TESTS / "test_518_restore.py", "h09_loaded_test_518_restore"
    )
    src = drill._make_source_tree(tmp_path)
    mutate_tree(src)
    out_dir = tmp_path / "backup-h09"
    drill._run_backup(src, out_dir, drill.FakeLanceTable("chunks"))

    err = None
    message = None
    try:
        restore_backup_set(
            out_dir,
            tmp_path / "restored-h09",
            lancedb_factory=lambda name: drill.FakeLanceTable(name),
            key_provider=drill._key_provider,
        )
    except RestoreError as exc:
        err = type(exc).__name__
        message = str(exc)
    return err, message


def test_restore_drill_without_draft_room(tmp_path):
    """A backup set created from a data tree with no draft-room directory
    must restore without raising RestoreError — the documented CI drill's
    fixture always seeds one, so it could never observe this case (the two
    HIGH restore defects tracked by Workstream E).

    DRIFT NOTE (pin): the producer/consumer pair is tolerant at master
    (e037a97c / PR #1026 — a missing draft-room is not recorded, an empty
    tree carries ``empty: true``), so this drill passes at base and pins the
    fixed behavior for the #746 fix to keep."""
    err, message = _run_restore_drill(
        tmp_path, lambda src: shutil.rmtree(src / "draft-room")
    )
    assert err is None, (
        "H09-AC1 RESTORE DRILL BLIND TO MISSING DRAFT-ROOM: backing up a "
        f"data tree with no draft-room and restoring it raised "
        f"{err}: {message} — the drill's fixture must be able to see this "
        "shape fail"
    )


def test_restore_drill_with_empty_vault(tmp_path):
    """A backup set created from a data tree whose only vault directory is
    EMPTY must restore without raising RestoreError (the audit's fixture
    always seeds a non-empty vault). DRIFT NOTE (pin): passes at base for
    the same e037a97c reason as AC1."""
    def _empty_the_vault(src: Path) -> None:
        shutil.rmtree(src / "vault-7")
        (src / "vault-7").mkdir()

    err, message = _run_restore_drill(tmp_path, _empty_the_vault)
    assert err is None, (
        "H09-AC2 RESTORE DRILL BLIND TO EMPTY VAULT: backing up a data "
        "tree whose only vault directory exists but is empty and restoring "
        f"it raised {err}: {message} — the drill's fixture must be able to "
        "see this shape fail"
    )


# ---------------------------------------------------------------------------
# AC3 — model-pull check must evaluate commented example lines
# ---------------------------------------------------------------------------


def test_model_pull_check_evaluates_commented_examples():
    """DOC-003b's ``model_pull_findings`` must report a model configured only
    on a commented example line that no ``ollama pull`` provisions: every
    concrete model assignment in INSTALLATION.md is such a line, so the
    check currently evaluates zero real models and its own test asserts
    vacuously."""
    scripts_dir = str(REPO / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    doc_check = importlib.import_module("check_installation_doc")
    findings = doc_check.model_pull_findings("# CHAT_MODEL=never-pulled:1b\n")
    assert len(findings) == 1, (
        "H09-AC3 MODEL-PULL CHECK IGNORES COMMENTED EXAMPLE LINES: "
        f"model_pull_findings('# CHAT_MODEL=never-pulled:1b') returned "
        f"{findings!r} — CHAT_MODEL_RE only matches the two compose "
        "placeholder forms it immediately skips, so every real (commented) "
        "assignment in INSTALLATION.md is never evaluated"
    )


# ---------------------------------------------------------------------------
# AC4 — the seq-uniqueness concurrency test must not be win32-skipped
# ---------------------------------------------------------------------------


def test_seq_concurrency_test_not_skipped_on_win32():
    """``test_concurrent_batches_assign_unique_monotonic_seq`` — the only
    concurrency test of chat-turn ``seq`` uniqueness — must not carry a
    ``pytest.mark.skipif`` whose condition mentions ``win32``: the cited
    sqlite3 access violation does not reproduce on a live Windows host, and
    the unconditional skip removes real coverage from every Windows run.
    Unskipping or ``xfail(strict=False)`` both satisfy this check."""
    tree = ast.parse(
        (TESTS / "test_chat_turns.py").read_text(encoding="utf-8")
    )
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "test_concurrent_batches_assign_unique_monotonic_seq":
            continue
        for decorator in node.decorator_list:
            try:
                source = ast.unparse(decorator)
            except Exception:  # noqa: BLE001 — unparse failure is evidence
                source = ""
            if "win32" in source and "skipif" in source:
                offenders.append(source)
    assert len(offenders) == 0, (
        "H09-AC4 SEQ CONCURRENCY TEST UNCONDITIONALLY SKIPPED ON WIN32: "
        f"{offenders} — re-verify on a Windows host and unskip, or convert "
        "to xfail(strict=False) with a linked issue"
    )


# ---------------------------------------------------------------------------
# AC6 — the #652 timeout guardrail census must see positional/qualified calls
# ---------------------------------------------------------------------------


def test_timeout_guardrail_catches_positional_and_qualified_calls():
    """AMENDED (CHECK_WRONG, plan-critic round 1): strengthened from "both
    shapes flagged" to a shared numeric-literal evaluator with signed
    literals plus safe negative controls. ``_literal_timeout_call_sites``
    must report BOTH a positional numeric timeout (``LLMClient(300.0, ...)``
    — ``timeout`` is ``__init__``'s first positional parameter, including
    signed forms like ``LLMClient(-1.0)``) and an attribute-qualified callee
    (``services.llm_client.LLMClient(timeout=300.0)``), while NOT flagging a
    dynamic timeout (``timeout=settings.timeout``), a call with no timeout
    argument, or a non-LLMClient call with a numeric arg. At base the census
    requires a bare ``Name`` callee and inspects only keywords, so the two
    hardcoded shapes reintroduce the #652 defect without failing the
    guardrail."""
    guardrail = _load_test_module(
        TESTS / "test_issue652_guardrail_no_hardcoded_timeouts.py",
        "h09_loaded_652_guardrail",
    )
    census = getattr(guardrail, "_literal_timeout_call_sites", None)
    assert census is not None, (
        "H09-AC6 HARNESS DRIFT: test_issue652_guardrail_no_hardcoded_"
        "timeouts.py no longer defines _literal_timeout_call_sites; actual "
        "module functions: "
        f"{[n for n in dir(guardrail) if n.startswith('_literal')]}"
    )
    snippet = (
        "LLMClient(300.0, base_url='x')\n"
        "services.llm_client.LLMClient(timeout=300.0)\n"
        "LLMClient(timeout=-5.0)\n"
        "LLMClient(timeout=settings.timeout)\n"
        "LLMClient(base_url='x')\n"
        "OtherClient(300.0)\n"
        "OtherClient(timeout=300.0)\n"
    )
    sites = census(ast.parse(snippet))
    assert len(sites) == 3, (
        "H09-AC6 TIMEOUT GUARDRAIL BLIND TO POSITIONAL/QUALIFIED/SIGNED "
        f"CALLS: _literal_timeout_call_sites reported {sites!r} for the "
        "snippet (expected exactly 3 sites: positional 300.0, qualified "
        "keyword 300.0, and signed keyword -5.0 — and NO sites for the "
        "dynamic timeout, the timeout-less call, or the non-LLMClient "
        "calls) — extend the census to a shared numeric-literal evaluator "
        "covering node.args[0] and signed ast.UnaryOp constants, and "
        "ast.Attribute funcs whose attr == 'LLMClient'"
    )


# ---------------------------------------------------------------------------
# AC7 — the coverage-design pin must be comment aware
# ---------------------------------------------------------------------------


def _junction(target: Path, link: Path) -> None:
    """Best-effort Windows directory junction (no privilege needed); used
    only when the loaded design-pin module needs the frontend's real
    node_modules (e.g. a typescript-compiler-based rewrite)."""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    except Exception:  # noqa: BLE001 — fall through to mklink
        pass
    import subprocess

    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
    )


def test_coverage_design_pin_is_comment_aware(tmp_path):
    """With ``coverage.include``'s ``'src/lib/api/**'`` entry present ONLY
    inside a JS comment, at least one ``test_ac11_design_*`` check in
    test_issue258_coverage_design.py must fail: the current regex + bracket
    extractor keeps comment text, so a commented-out scope still satisfies
    the design pin. The check runs the loaded module against a copy of the
    real vite.config.ts under tmp_path so the real tree is never mutated."""
    module_path = TESTS / "test_issue258_coverage_design.py"
    design = _load_test_module(module_path, "h09_loaded_258_coverage")
    assert hasattr(design, "REPO") and hasattr(design, "FRONTEND"), (
        "H09-AC7 HARNESS DRIFT: test_issue258_coverage_design.py no longer "
        "exposes REPO/FRONTEND path globals — it computes paths another way; "
        f"module globals: {sorted(vars(design))}"
    )

    vite_path = REPO / "frontend" / "vite.config.ts"
    source = vite_path.read_text(encoding="utf-8")
    anchor = "include: ['src/lib/api/**', 'src/stores/**'],"
    assert source.count(anchor) == 1, (
        "H09-AC7 HARNESS DRIFT: the coverage.include anchor "
        f"{anchor!r} occurs {source.count(anchor)} time(s) in the real "
        "frontend/vite.config.ts (expected exactly 1) — the config or the "
        "anchor drifted"
    )
    mutated = source.replace(
        anchor, "include: [/* 'src/lib/api/**', */ 'src/stores/**'],"
    )

    fake_frontend = tmp_path / "frontend"
    fake_frontend.mkdir()
    (fake_frontend / "vite.config.ts").write_text(mutated, encoding="utf-8")

    # Only a typescript-compiler-based rewrite needs the frontend's real
    # node_modules; junction it in when the loaded module mentions it.
    module_source = module_path.read_text(encoding="utf-8")
    real_node_modules = REPO / "frontend" / "node_modules"
    if "typescript" in module_source and real_node_modules.is_dir():
        _junction(real_node_modules, fake_frontend / "node_modules")

    design.REPO = tmp_path
    design.FRONTEND = fake_frontend

    failures = []
    called = 0
    for name in sorted(vars(design)):
        if name.startswith("test_ac11_design_") and callable(
            getattr(design, name, None)
        ):
            called += 1
            try:
                getattr(design, name)()
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
    assert called >= 1, (
        "H09-AC7 HARNESS DRIFT: test_issue258_coverage_design.py defines no "
        "test_ac11_design_* functions to call"
    )
    assert len(failures) >= 1, (
        "H09-AC7 COVERAGE DESIGN PIN IS COMMENT BLIND: with "
        "'src/lib/api/**' commented out of coverage.include, every "
        "test_ac11_design_* check still passed against the mutated "
        "vite.config.ts — the regex/bracket extractor keeps comment text; "
        "parse the config with comment awareness (e.g. the TypeScript AST "
        "walker test_issue566_config_loader_native.py already uses)"
    )


# ---------------------------------------------------------------------------
# AC8 — CONTRIBUTING.md must name the python-magic / msys-magic DLL seam
# ---------------------------------------------------------------------------


def test_contributing_names_python_magic_msys_seam():
    """The repo-root CONTRIBUTING.md must name the python-magic /
    ``msys-magic-1.dll`` collection hang and its PATH workaround in its
    Windows development section: on Windows Git Bash, ``usr/bin`` on PATH
    makes python-magic load the msys DLL, which hangs the real-docs parsing
    module's import probe with no timeout."""
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    count = len(re.findall(r"msys-magic|python-magic", text, re.IGNORECASE))
    assert count >= 1, (
        "H09-AC8 CONTRIBUTING.MD MISSING THE python-magic/msys SEAM: the "
        "Windows development section names other Git Bash seams but not the "
        "msys-magic-1.dll collection hang or its PATH workaround (strip "
        "Git's usr/bin from PATH)"
    )
