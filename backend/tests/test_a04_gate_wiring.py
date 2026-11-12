"""Wiring pin for the HTTP 500 detail-hygiene gate (issue #686, AC12; #917).

The gate (scripts/check_a04_http500_detail_hygiene.py) only protects the
routes if it actually runs: these tests pin its presence in the CI
quality-contracts job and the justfile recipe (modeled on
test_settings_consumers_gate.py::test_t7_census_wired_into_ci_and_justfile)
so the step cannot be silently dropped.

Issue #917 extended the gate to the Draft Room: the planted-violation tests
below prove a ``DraftRoomHTTPError`` built with a literal status >= 500 and a
raw-exception ``detail`` (f-string or ``str(...)``) fails the gate with the
file and line, that a 4xx ``str(exc)`` detail stays unflagged, that the gate
still passes on the real (unmodified) route modules, and that the audited
module set is exactly the four route files with the original HTTPException
coverage intact in each of memories/vaults/admin.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

COMMAND = "python scripts/check_a04_http500_detail_hygiene.py"
SCRIPT_REL = Path("scripts") / "check_a04_http500_detail_hygiene.py"
SCRIPT = REPO / SCRIPT_REL
ROUTE_REL = {
    name: Path("backend") / "app" / "api" / "routes" / name
    for name in ("memories.py", "vaults.py", "admin.py", "draft_room.py")
}
DIAGNOSTIC_LIMIT = 12_000

DRAFT_ROOM_STANDIN = '''from fastapi import HTTPException


class DraftRoomHTTPError(HTTPException):
    def __init__(self, status_code, detail, code, context=None):
        super().__init__(status_code=status_code, detail=detail)


def boom_fstring(exc):
    raise DraftRoomHTTPError(500, f"promotion failed: {exc}", "internal_error")


def boom_str(exc):
    raise DraftRoomHTTPError(500, str(exc), "internal_error")


def boom_503(exc):
    raise DraftRoomHTTPError(503, f"worker unavailable: {exc}", "internal_error")


def ok_4xx(exc):
    raise DraftRoomHTTPError(413, str(exc), "promotion_too_large")
'''


def _httpexception_standin() -> str:
    return (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "def bad(exc):\n"
        '    raise HTTPException(status_code=500, detail=f"failure: {exc}")\n'
    )


def _run_gate(tmp_path: Path, replacements: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the gate against a synthetic tree rooted at ``tmp_path``."""
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    for name, content in replacements.items():
        target = tmp_path / ROUTE_REL[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )


def _real_module(name: str) -> str:
    return (REPO / ROUTE_REL[name]).read_text(encoding="utf-8")


def _patch(text: str, old: str, new: str) -> str:
    assert old in text, f"anchor not found in source: {old!r}"
    return text.replace(old, new, 1)


def test_gate_fails_on_planted_draftroom_500_fstring_and_str_details(tmp_path) -> None:
    """The #917 extension: a literal >= 500 DraftRoomHTTPError whose detail
    echoes the exception (either shape) must fail with file and line."""
    result = _run_gate(
        tmp_path,
        {
            "memories.py": _real_module("memories.py"),
            "vaults.py": _real_module("vaults.py"),
            "admin.py": _real_module("admin.py"),
            "draft_room.py": DRAFT_ROOM_STANDIN,
        },
    )
    assert result.returncode == 1, (result.stdout + result.stderr)[:DIAGNOSTIC_LIMIT]
    flagged = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("RAW-EXCEPTION-DETAIL: backend/app/api/routes/draft_room.py:")
    ]
    flagged_lines = {int(line.rsplit(":", 1)[1]) for line in flagged}
    source_lines = DRAFT_ROOM_STANDIN.splitlines()
    expected = {
        index + 1
        for index, line in enumerate(source_lines)
        if "DraftRoomHTTPError(500," in line or "DraftRoomHTTPError(503," in line
    }
    assert expected, "stand-in has no planted raises"
    assert flagged_lines == expected, (
        f"expected the 500/503 echo sites {sorted(expected)}, "
        f"got {sorted(flagged_lines)}; stdout: {result.stdout}"
    )


def test_gate_leaves_a_4xx_str_detail_unflagged(tmp_path) -> None:
    """The 413 str(exc) control is not in the gate's class (client-error
    messages are crafted store messages, not server-fault echoes)."""
    result = _run_gate(
        tmp_path,
        {
            "memories.py": _real_module("memories.py"),
            "vaults.py": _real_module("vaults.py"),
            "admin.py": _real_module("admin.py"),
            "draft_room.py": DRAFT_ROOM_STANDIN.replace(
                'raise DraftRoomHTTPError(500, f"promotion failed: {exc}", "internal_error")',
                'raise DraftRoomHTTPError(413, f"promotion failed: {exc}", "too_large")',
            ).replace(
                "raise DraftRoomHTTPError(500, str(exc), \"internal_error\")",
                "raise DraftRoomHTTPError(413, str(exc), \"too_large\")",
            ).replace(
                'raise DraftRoomHTTPError(503, f"worker unavailable: {exc}", "internal_error")',
                'raise DraftRoomHTTPError(409, f"worker unavailable: {exc}", "conflict")',
            ),
        },
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[:DIAGNOSTIC_LIMIT]


def test_gate_passes_on_the_real_route_modules() -> None:
    """The unmodified route modules (now including draft_room.py) must pass —
    the production fix keeps the two map helpers' >= 500 arms generic."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[:DIAGNOSTIC_LIMIT]
    assert "4 files checked" in result.stdout


def test_audited_module_set_is_exactly_the_four_route_files() -> None:
    """Removing any audited module (e.g. vaults.py or admin.py) would silently
    shrink the gate's scope; pin the exact set (plan-critic round 1, MAJOR-4).
    The regex is scoped to the AUDITED_MODULES assignment itself so a route
    path written anywhere else in the script cannot count as an entry
    (final-critic NIT)."""
    import re

    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"AUDITED_MODULES\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert match, "AUDITED_MODULES assignment not found in the gate"
    entries = re.findall(r'"(backend/app/api/routes/[a-z_]+\.py)"', match.group(1))
    assert sorted(entries) == sorted(
        ROUTE_REL[name].as_posix() for name in ROUTE_REL
    ), f"unexpected AUDITED_MODULES entries: {entries}"


def test_httpexception_coverage_holds_per_original_module(tmp_path) -> None:
    """One planted HTTPException 500 violation per original module: dropping
    any one of memories/vaults/admin from the audited set fails a named test,
    not just the memories leg."""
    for name in ("memories.py", "vaults.py", "admin.py"):
        replacements = {
            other: _real_module(other) for other in ROUTE_REL if other != name
        }
        replacements[name] = _httpexception_standin()
        replacements["draft_room.py"] = _real_module("draft_room.py")
        result = _run_gate(tmp_path, replacements)
        assert result.returncode == 1, (
            f"{name}: planted HTTPException violation not caught; "
            f"{(result.stdout + result.stderr)[:DIAGNOSTIC_LIMIT]}"
        )
        assert f"RAW-EXCEPTION-DETAIL: backend/app/api/routes/{name}:" in result.stdout



def test_gate_wired_into_ci_quality_contracts_job():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    in_quality_job = False
    for line in ci.splitlines():
        if line.startswith("  quality-contracts:"):
            in_quality_job = True
            continue
        if in_quality_job and line.startswith("  ") and not line.startswith("    "):
            break  # next top-level job key ends the block
        if in_quality_job and not line.lstrip().startswith("#") and COMMAND in line:
            return
    raise AssertionError("ci.yml quality-contracts job must run the hygiene gate")


def test_gate_wired_into_justfile_quality_contracts_recipe():
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    in_recipe = False
    for line in justfile.splitlines():
        if line.startswith("quality-contracts:"):
            in_recipe = True
            continue
        if in_recipe and line and not line.startswith(" "):
            break  # next recipe ends the block
        if in_recipe and not line.lstrip().startswith("#") and COMMAND in line:
            return
    raise AssertionError("justfile quality-contracts recipe must run the hygiene gate")
