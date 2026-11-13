"""Implementation mutation legs for the widened a04 gate (issue #1119 AC1).

The frozen spec (``test_a04_variable_status_echo.py``) pins the two acceptance
legs: a planted variable-status 5xx echo must fail the gate, and the REAL
audited modules must stay unflagged. This file pins the eight analyzer
mutation shapes the plan-critic ladder executed as counterexamples (rounds 2-5,
probe v5 legs L3-L10) so the widened predicate cannot silently regress:

  R2-F3  unguarded module-level mixed mapping + 500 default  -> FLAG
  R3-F2a the same violation with a FUNCTION-LOCAL mapping    -> FLAG
  R3-F2b status rebound to 500 after a dominating guard      -> FLAG
  R4-F1a call BEFORE rebinding (500+str(exc), later 404)     -> FLAG
  R4-F1b ASYNC function, literal 500 + str(exc)             -> FLAG
  R4-F1c MODULE-LEVEL literal 500 + str(exc)                -> FLAG
  R5-F1  STRICT > 500 guard leaves 500 reachable            -> FLAG
  ctrl   guarded function-local mapping                      -> NOT flagged

Harness mirrors the frozen spec's planted-tree idiom (gate copied into a
synthetic tree, run as a subprocess, draft_room.py replaced).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SCRIPT_REL = Path("scripts") / "check_a04_http500_detail_hygiene.py"
SCRIPT = REPO / SCRIPT_REL
ROUTE_REL = {
    name: Path("backend") / "app" / "api" / "routes" / name
    for name in ("memories.py", "vaults.py", "admin.py", "draft_room.py")
}
DIAGNOSTIC_LIMIT = 12_000

HEADER = """class DraftRoomHTTPError(Exception):
    pass
"""

MIXED_MODULE_LEVEL = (
    HEADER
    + '''

_CODES = {"conflict": 409}


def leak(exc):
    status_code = _CODES.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

MIXED_FUNCTION_LOCAL = (
    HEADER
    + '''

def leak(exc):
    codes = {"conflict": 409}
    status_code = codes.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

REBIND_AFTER_GUARD = (
    HEADER
    + '''

def leak(exc):
    status_code = 404
    if status_code >= 500:
        raise DraftRoomHTTPError(status_code, "fixed", "x")
    status_code = 500
    raise DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

CALL_BEFORE_REBIND = (
    HEADER
    + '''

def call_before_rebind(exc):
    status_code = 500
    err = DraftRoomHTTPError(status_code, str(exc), "mapper")
    status_code = 404
    return err
'''
)

ASYNC_LITERAL_500 = (
    HEADER
    + '''

async def async_handler(exc):
    raise DraftRoomHTTPError(500, str(exc), "internal_error")
'''
)

MODULE_LEVEL_LITERAL_500 = (
    HEADER
    + '''

raise DraftRoomHTTPError(500, str(exc_var), "internal_error")
'''
)

STRICT_GT_GUARD = (
    HEADER
    + '''

def strict_gt_guard(exc):
    status_code = 500
    if status_code > 500:
        return DraftRoomHTTPError(status_code, "fixed", "x")
    return DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)

GUARDED_FUNCTION_LOCAL_CONTROL = (
    HEADER
    + '''

def guarded(exc):
    codes = {"conflict": 409}
    status_code = codes.get(exc.code, 500)
    if status_code >= 500:
        return DraftRoomHTTPError(status_code, "fixed detail", "x")
    return DraftRoomHTTPError(status_code, str(exc), "mapper")
'''
)


def _run_gate(tmp_path: Path, draft_room_content: str) -> subprocess.CompletedProcess:
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    for name, rel in ROUTE_REL.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            draft_room_content
            if name == "draft_room.py"
            else (REPO / rel).read_text(encoding="utf-8")
        )
        target.write_text(content, encoding="utf-8", newline="\n")
    return subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )


def _flagged(result: subprocess.CompletedProcess) -> list[int]:
    return sorted(
        int(line.rsplit(":", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("RAW-EXCEPTION-DETAIL: backend/app/api/routes/draft_room.py:")
    )


def _echo_lines(source: str) -> list[int]:
    return [
        i + 1
        for i, line in enumerate(source.splitlines())
        if "DraftRoomHTTPError(status_code, str(exc)" in line
        or "DraftRoomHTTPError(status_code, f\"" in line
        or "DraftRoomHTTPError(500, str(" in line
    ]


def _assert_flagged(tmp_path, source: str, label: str) -> None:
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == _echo_lines(source), (
        f"{label}: the widened gate must flag exactly the 5xx-capable echo "
        f"call(s); exit={result.returncode} flagged={_flagged(result)} "
        f"expected={_echo_lines(source)}; stdout: {result.stdout}; "
        f"stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )


def _assert_clean(tmp_path, source: str, label: str) -> None:
    result = _run_gate(tmp_path, source)
    assert result.returncode == 0 and _flagged(result) == [], (
        f"{label}: the guarded control must stay unflagged; "
        f"exit={result.returncode} flagged={_flagged(result)}; "
        f"stdout: {result.stdout}"[:DIAGNOSTIC_LIMIT]
    )


def test_r2f3_unguarded_module_level_mixed_mapping_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MIXED_MODULE_LEVEL, "R2-F3 mixed module-level")


def test_r3f2a_function_local_mapping_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MIXED_FUNCTION_LOCAL, "R3-F2a function-local mapping")


def test_r3f2b_rebind_after_guard_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, REBIND_AFTER_GUARD, "R3-F2b rebind-after-guard")


def test_r4f1a_call_before_rebinding_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, CALL_BEFORE_REBIND, "R4-F1a call-before-rebind")


def test_r4f1b_async_literal_500_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, ASYNC_LITERAL_500, "R4-F1b async literal-500")


def test_r4f1c_module_level_literal_500_flags(tmp_path) -> None:
    _assert_flagged(tmp_path, MODULE_LEVEL_LITERAL_500, "R4-F1c module-level literal")


def test_r5f1_strict_gt_guard_leaves_500_reachable(tmp_path) -> None:
    _assert_flagged(tmp_path, STRICT_GT_GUARD, "R5-F1 strict-gt guard")


def test_r45f1_try_else_literal_500_flags(tmp_path) -> None:
    """Implementation-review round-1 F2: the try/except/ELSE arm must stay
    covered (the base gate's ast.walk visited every node; the ordered
    visitor must too)."""
    source = (
        "from fastapi import HTTPException\n"
        "\n"
        "\n"
        "def handler(exc):\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception as e:  # noqa: BLE001\n"
        "        pass\n"
        "    else:\n"
        '        raise HTTPException(status_code=500, detail=f"boom {exc}")\n'
    )
    result = _run_gate(tmp_path, source)
    assert result.returncode == 1 and _flagged(result) == [10], (
        "the try/except/ELSE arm must stay covered by the ordered visitor "
        f"(base gate's ast.walk visited it); exit={result.returncode} "
        f"flagged={_flagged(result)}; stdout: {result.stdout}; "
        f"stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )


def test_control_guarded_function_local_stays_clean(tmp_path) -> None:
    _assert_clean(tmp_path, GUARDED_FUNCTION_LOCAL_CONTROL, "guarded control")
