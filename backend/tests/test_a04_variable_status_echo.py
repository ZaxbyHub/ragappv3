"""Regression specs for issue #1119 AC1: the a04 HTTP-500 detail-hygiene gate
(``scripts/check_a04_http500_detail_hygiene.py``) must flag a
``DraftRoomHTTPError`` call whose status is computed at runtime and resolves
to >= 500 while its detail echoes the raw exception (``str(...)`` or an
f-string) — the exact variable-status shape issue #917 was filed about,
which the literal-``ast.Constant`` predicate passes silently today.

Mirrors ``test_a04_gate_wiring.py``'s planted-tree harness (the gate is
driven as a subprocess against a synthetic tree so the check is black-box).
Two frozen legs:

  1. ``test_gate_fails_on_planted_variable_status_5xx_echo`` — a planted
     draft_room stand-in carries two variable-status 5xx echo raises (str
     and f-string details) plus controls that MUST stay unflagged: the real
     mapper idiom (a guarded 4xx ``str(exc)`` arm), a literal 413 ``str(exc)``
     arm, and a literal 500 arm with a generic (non-echo) detail. The gate
     must exit 1 naming exactly the two planted echo lines.
  2. ``test_real_route_modules_stay_unflagged_after_widening`` — the widened
     gate run against the REAL (unmodified) audited route modules must still
     exit 0: the real 4xx mapper arms in ``draft_room.py``
     (``_map_store_error`` / ``_map_storage_error``, whose final arms pass a
     runtime-computed status with a ``str(exc)`` detail that resolves to 4xx
     behind a ``>= 500`` guard) must not be swept up by the widening.
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

# Planted draft_room stand-in. The two ``boom_variable_status_*`` raises are
# the violation: their status argument is a local computed at runtime from an
# all-5xx mapping (every resolution >= 500) and their detail echoes the raw
# exception. Everything else is an unflag-me control: the guarded 4xx mapper
# arm is the REAL production idiom from draft_room.py's map helpers, the
# literal 413 ``str(exc)`` arm is a crafted client-error message, and the
# literal 500 arm carries the generic non-echo detail the >= 500 arms use.
VARIABLE_STATUS_STANDIN = '''from fastapi import HTTPException


class DraftRoomHTTPError(HTTPException):
    def __init__(self, status_code, detail, code, context=None):
        super().__init__(status_code=status_code, detail=detail)


_SERVER_CODES = {"internal_error": 500, "store_unavailable": 503}
_CLIENT_CODES = {"too_large": 413, "unsupported": 415}


def boom_variable_status_str(exc):
    status_code = _SERVER_CODES.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, str(exc), "internal_error")


def boom_variable_status_fstring(exc):
    status_code = _SERVER_CODES.get(exc.code, 500)
    raise DraftRoomHTTPError(status_code, f"promotion failed: {exc}", "internal_error")


def ok_guarded_mapper_4xx(exc):
    status_code = _CLIENT_CODES.get(exc.code, 500)
    if status_code >= 500:
        return DraftRoomHTTPError(status_code, "draft store operation failed", exc.code)
    return DraftRoomHTTPError(status_code, str(exc), exc.code)


def ok_literal_4xx(exc):
    raise DraftRoomHTTPError(413, str(exc), "too_large")


def ok_literal_5xx_generic(exc):
    raise DraftRoomHTTPError(500, "draft store operation failed", "internal_error")
'''


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


def test_gate_fails_on_planted_variable_status_5xx_echo(tmp_path) -> None:
    """The planted variable-status (runtime-computed, resolves to >= 500)
    echo raises must fail the gate with the file and line — and ONLY those
    lines: the guarded 4xx mapper arm and the literal controls must stay
    unflagged after the widening."""
    result = _run_gate(
        tmp_path,
        {
            "memories.py": _real_module("memories.py"),
            "vaults.py": _real_module("vaults.py"),
            "admin.py": _real_module("admin.py"),
            "draft_room.py": VARIABLE_STATUS_STANDIN,
        },
    )
    assert result.returncode == 1, (
        "planted variable-status 5xx echo not flagged by the a04 gate "
        f"(exit {result.returncode}); stdout: {result.stdout}; "
        f"stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )
    flagged = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("RAW-EXCEPTION-DETAIL: backend/app/api/routes/draft_room.py:")
    ]
    flagged_lines = {int(line.rsplit(":", 1)[1]) for line in flagged}
    source_lines = VARIABLE_STATUS_STANDIN.splitlines()
    expected = {
        index + 1
        for index, line in enumerate(source_lines)
        if "raise DraftRoomHTTPError(status_code," in line
    }
    assert expected, "stand-in has no planted variable-status raises"
    assert flagged_lines == expected, (
        f"expected the gate to flag exactly the variable-status 5xx echo "
        f"raises at {sorted(expected)} (and leave the guarded 4xx mapper arm "
        f"and literal controls unflagged), got {sorted(flagged_lines)}; "
        f"stdout: {result.stdout}"
    )


def test_real_route_modules_stay_unflagged_after_widening() -> None:
    """The widened gate run against the real, unmodified audited modules must
    still exit 0: the real 4xx mapper arms in draft_room.py pass a
    runtime-computed status with a ``str(exc)`` detail that resolves to 4xx
    (guarded behind the >= 500 generic-detail arm), so a widening that flags
    every variable-status ``str`` detail would break this leg."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        "real audited route modules (including draft_room.py's guarded 4xx "
        "mapper arms) must stay unflagged by the widened gate; stdout: "
        f"{result.stdout}; stderr: {result.stderr}"[:DIAGNOSTIC_LIMIT]
    )
    assert "OK: no raw-exception 500 details" in result.stdout, result.stdout
