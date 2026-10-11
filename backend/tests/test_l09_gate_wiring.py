"""Issue #780 L09 CI guard tests.

``scripts/check_l09_skill_trigger_paths.py``, ``check_l09_index_assets.py``
and ``check_l09_memory_response_fields.py`` gate CI (Quality contracts job +
justfile), so their wiring and behavior are pinned here following the repo's
gate-test precedents (``test_l05_ci_guards.py``, ``test_l07_gate_wiring.py``).
Unlike check_l07 (which resolves its root from cwd), the three l09 guards
anchor ``ROOT = Path(__file__).resolve().parents[1]`` — so the synthetic-tree
and fail-closed tests run a COPY of each guard planted in a fixture tree (the
same relocated-copy mechanism the frozen vitest drivers use), while the
real-tree exit-0 runs keep the plain cwd form.

Review feedback (PR #880, PRR-001/PRR-014): each guard also gets a
planted-violation test — a fixture tree carrying a real AC1/AC4/AC5 violation
must exit 1 with its diagnostic — because a parser regression (zero globs
collected, zero attributes matched) previously produced a silent "STALE 0"/
"MISSING 0"/"PHANTOM 0" pass while every wiring pin stayed green. The wiring
pins match only non-comment run lines and fail on `if:`/`continue-on-error`
neutering (the l07 mutant pattern).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
DIAGNOSTIC_LIMIT = 12_000

L09_GUARDS = (
    "check_l09_skill_trigger_paths.py",
    "check_l09_index_assets.py",
    "check_l09_memory_response_fields.py",
)
CI_REL = Path(".github") / "workflows" / "ci.yml"
JUSTFILE = REPO / "justfile"


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        timeout=120,
        capture_output=True,
        text=True,
    )


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout[-DIAGNOSTIC_LIMIT:] + result.stderr[-DIAGNOSTIC_LIMIT:]


def test_guards_pass_on_real_tree() -> None:
    # Post-#780 tree: no stale glob, no missing %BASE_URL% asset, no phantom
    # response field. Any hit here is a regression of the shipped fixes.
    for name in L09_GUARDS:
        result = _run(SCRIPTS / name, REPO)
        assert result.returncode == 0, f"{name}: {_tail(result)}"


def _quality_contract_run_steps(ci_text: str) -> list[str]:
    """Non-comment `run:` values of the quality-contracts job.

    A commented-out step (or a step inside another job) is not wiring; an
    `if:`-false or `continue-on-error` job body is reported via the sentinel
    in the returned list so the pin can reject it. Steps use the mapping-key
    form (`- name:` then `        run: <cmd>`), not `- run:`.
    """
    steps: list[str] = []
    in_quality = False
    for raw in ci_text.splitlines():
        code = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        job_match = re.match(r"^  ([\w-]+):\s*$", code)
        if job_match:
            in_quality = job_match.group(1) == "quality-contracts"
            continue
        if not in_quality:
            continue
        if re.match(r"^\s+if:\s*false\b", code):
            steps.append("NEUTERED: if:false")
        if re.match(r"^\s+continue-on-error:\s*true\b", code):
            steps.append("NEUTERED: continue-on-error")
        step = re.match(r"^\s+run:\s*(.+)$", code)
        if step:
            steps.append(step.group(1).strip())
    return steps


def test_guards_wired_into_ci_and_justfile() -> None:
    ci = (REPO / CI_REL).read_text(encoding="utf-8")
    justfile = JUSTFILE.read_text(encoding="utf-8")

    ci_steps = _quality_contract_run_steps(ci_text=ci)
    for name in L09_GUARDS:
        run_line = f"python scripts/{name}"
        assert run_line in ci_steps, (
            f"{name} missing from the quality-contracts job's non-comment run steps "
            f"(got: {ci_steps})"
        )
        assert "NEUTERED" not in ci_steps, "quality-contracts job is if:false/continue-on-error"
        # justfile: only non-comment recipe lines count, and the recipe block
        # (not any other recipe) must carry the step.
        recipe = re.search(
            r"^quality-contracts:\n((?:    .*\n|^\n)*?)(?=^\S|\Z)",
            justfile,
            re.MULTILINE,
        )
        assert recipe, "quality-contracts recipe not found in justfile"
        recipe_lines = [
            line.strip()
            for line in recipe.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert run_line in recipe_lines, f"{name} missing from the justfile quality-contracts recipe"


def _plant_guard_copy(fixture_root: Path, name: str) -> Path:
    fixture_scripts = fixture_root / "scripts"
    fixture_scripts.mkdir(parents=True, exist_ok=True)
    copy = fixture_scripts / name
    shutil.copyfile(SCRIPTS / name, copy)
    return copy


def test_guards_fail_closed_when_scanned_surface_is_absent(tmp_path: Path) -> None:
    # The guards anchor ROOT to their own location, so plant a COPY in a
    # fixture tree whose scanned surfaces do not exist: each must exit 2
    # (fail closed), never silently pass.
    for name in L09_GUARDS:
        copy = _plant_guard_copy(tmp_path, name)
        result = _run(copy, tmp_path)
        assert result.returncode == 2, (
            f"{name}: expected exit 2 on a tree without the scanned surfaces, "
            f"got {result.returncode}; stdout tail: {_tail(result)}"
        )


# ---- planted-violation pins (PRR-001): each guard must DETECT a real
# violation in a synthetic tree — exit 1 with its diagnostic — so a parser
# regression cannot degrade the gate to a silent pass.


def _plant_skill_trigger_violation(tmp_path: Path) -> Path:
    generated = tmp_path / ".opencode" / "skills" / "generated" / "probe-skill"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text(
        "---\n"
        "name: probe-skill\n"
        "trigger:\n"
        "  - files:\n"
        "      include:\n"
        '        - "frontend/src/lib/deleted.ts"\n'
        "      exclude:\n"
        '        - "*.test.*"\n'
        "---\n"
        "# Probe\n",
        encoding="utf-8",
    )
    return _plant_guard_copy(tmp_path, "check_l09_skill_trigger_paths.py")


def test_skill_trigger_guard_detects_stale_glob(tmp_path: Path) -> None:
    copy = _plant_skill_trigger_violation(tmp_path)
    result = _run(copy, tmp_path)
    assert result.returncode == 1, f"expected exit 1 on a stale glob, got {result.returncode}: {_tail(result)}"
    assert "STALE frontend/src/lib/deleted.ts" in result.stdout


def test_skill_trigger_guard_detects_body_reference(tmp_path: Path) -> None:
    generated = tmp_path / ".opencode" / "skills" / "generated" / "probe-skill"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text(
        "---\nname: probe-skill\n---\n# Probe\n- [ ] Verify `frontend/src/lib/also-deleted.ts`.\n",
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_skill_trigger_paths.py")
    result = _run(copy, tmp_path)
    # A skill with no trigger block contributes zero globs; the body
    # reference alone must still fail the gate.
    assert result.returncode == 1, f"expected exit 1 on a stale body reference, got {result.returncode}: {_tail(result)}"
    assert "STALE frontend/src/lib/also-deleted.ts" in result.stdout


def test_skill_trigger_guard_fails_closed_on_unparseable_include(tmp_path: Path) -> None:
    generated = tmp_path / ".opencode" / "skills" / "generated" / "probe-skill"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text(
        "---\nname: probe-skill\ntrigger:\n  - files:\n      include:\n        frontend/src/lib/unparsed.ts\n---\n",
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_skill_trigger_paths.py")
    result = _run(copy, tmp_path)
    assert result.returncode == 2, f"expected exit 2 on an unparseable include block, got {result.returncode}: {_tail(result)}"


def test_index_assets_guard_detects_missing_asset(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "public").mkdir(parents=True)
    (frontend / "public" / "keep.txt").write_text("", encoding="utf-8")
    (frontend / "index.html").write_text(
        '<!doctype html><html><head><link rel="icon" href="%BASE_URL%missing.svg" /></head></html>\n',
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_index_assets.py")
    result = _run(copy, tmp_path)
    assert result.returncode == 1, f"expected exit 1 on a missing asset, got {result.returncode}: {_tail(result)}"
    assert "MISSING frontend/public/missing.svg" in result.stdout


def test_index_assets_guard_detects_quote_and_attr_escapes(tmp_path: Path) -> None:
    # Single-quoted href and src= forms the original regex missed must be
    # detected, not silently skipped.
    frontend = tmp_path / "frontend"
    (frontend / "public").mkdir(parents=True)
    (frontend / "public" / "keep.txt").write_text("", encoding="utf-8")
    (frontend / "index.html").write_text(
        "<!doctype html><html><head>"
        "<link rel='icon' href='%BASE_URL%missing-single.svg' />"
        "<img src='%BASE_URL%missing-src.png' />"
        "</head></html>\n",
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_index_assets.py")
    result = _run(copy, tmp_path)
    assert result.returncode == 1, f"expected exit 1 on single-quote/src escapes, got {result.returncode}: {_tail(result)}"
    assert "MISSING frontend/public/missing-single.svg" in result.stdout
    assert "MISSING frontend/public/missing-src.png" in result.stdout


def test_index_assets_guard_fails_closed_on_parser_drift(tmp_path: Path) -> None:
    # %BASE_URL% present but the attribute regex matches none of it.
    frontend = tmp_path / "frontend"
    (frontend / "public").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><html><head><!-- %BASE_URL% --></head></html>\n",
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_index_assets.py")
    result = _run(copy, tmp_path)
    assert result.returncode == 2, f"expected exit 2 on parser drift, got {result.returncode}: {_tail(result)}"


def _plant_memory_fields_violation(tmp_path: Path) -> Path:
    api = tmp_path / "frontend" / "src" / "lib" / "api"
    backend_routes = tmp_path / "backend" / "app" / "api" / "routes"
    api.mkdir(parents=True)
    backend_routes.mkdir(parents=True)
    (api / "memories.ts").write_text(
        "export interface MemoryResult {\n  id: string;\n  content: string;\n  status: string;\n}\n\n"
        "export type AddMemoryResponse = MemoryResult;\n",
        encoding="utf-8",
    )
    (backend_routes / "memories.py").write_text(
        "class MemoryResponse(BaseModel):\n"
        '    """Response model."""\n\n'
        "    id: str\n"
        "    content: str\n",
        encoding="utf-8",
    )
    return _plant_guard_copy(tmp_path, "check_l09_memory_response_fields.py")


def test_memory_fields_guard_detects_phantom_field(tmp_path: Path) -> None:
    copy = _plant_memory_fields_violation(tmp_path)
    result = _run(copy, tmp_path)
    assert result.returncode == 1, f"expected exit 1 on a phantom field, got {result.returncode}: {_tail(result)}"
    assert "PHANTOM AddMemoryResponse.status" in result.stdout


def test_memory_fields_guard_ignores_docstring_and_nested_lines(tmp_path: Path) -> None:
    # Docstring prose and method-local annotations must not count as backend
    # fields (masking), and nested TS object keys must not count as fields
    # (false phantom) — both shapes from the PR #880 review.
    api = tmp_path / "frontend" / "src" / "lib" / "api"
    backend_routes = tmp_path / "backend" / "app" / "api" / "routes"
    api.mkdir(parents=True)
    backend_routes.mkdir(parents=True)
    (api / "memories.ts").write_text(
        "export interface MemoryResult {\n"
        "  id: string;\n"
        "  metadata?: {\n"
        "    nested_key?: string; // must NOT count as a field\n"
        "  } | null;\n"
        "}\n\n"
        "export type AddMemoryResponse = MemoryResult;\n",
        encoding="utf-8",
    )
    (backend_routes / "memories.py").write_text(
        "class MemoryResponse(BaseModel):\n"
        '    """id: not a field; importance: also not a field."""\n\n'
        "    id: str\n"
        "    metadata: dict | None = None\n\n"
        "    def helper(self) -> None:\n"
        "        local: str = \"annotation at method indent must not count\"\n"
        "        return None\n",
        encoding="utf-8",
    )
    copy = _plant_guard_copy(tmp_path, "check_l09_memory_response_fields.py")
    result = _run(copy, tmp_path)
    assert result.returncode == 0, f"expected exit 0 (no phantom), got {result.returncode}: {_tail(result)}"
    assert "PHANTOM 0" in result.stdout
