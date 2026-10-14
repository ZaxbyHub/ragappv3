"""Issue #789 N05 CI gate guard tests.

``scripts/check_n05_draftclaim_nullable.py`` gates CI (Quality-contracts job,
the justfile recipe, and the four documented inventories) and exists to stop
the backend claim model's ``Optional`` fields and the TypeScript ``DraftClaim``
interface from drifting apart. Issue #789 review F-014 found it shipped with
**no** test of its own — so a dropped CI step, a removed justfile line, or
parser rot against reformatted source would have regressed silently.

Pinned here, following the repo's gate-test precedents
(``test_l07_gate_wiring.py``, ``test_a04_gate_wiring.py``): the gate passes on
the real tree, the wiring is present, it FAILS on synthetic violating trees in
every direction it now covers, and it fails closed on structural drift.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT_REL = Path("scripts") / "check_n05_draftclaim_nullable.py"
SCRIPT = REPO / SCRIPT_REL
CI_REL = Path(".github") / "workflows" / "ci.yml"
JUSTFILE = REPO / "justfile"
BACKEND_REL = Path("backend") / "app" / "api" / "routes" / "draft_room.py"
FRONTEND_REL = Path("frontend") / "src" / "lib" / "api" / "draftRoom.ts"
DIAGNOSTIC_LIMIT = 12_000


def _run_on(tmp_path: Path, backend: str | None = None, frontend: str | None = None):
    """Run the gate against a synthetic tree rooted at ``tmp_path``."""
    root = tmp_path
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "scripts" / SCRIPT.name)
    for rel, replacement in ((BACKEND_REL, backend), (FRONTEND_REL, frontend)):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if replacement is None:
            shutil.copy(REPO / rel, target)
        else:
            target.write_text(replacement, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(root / "scripts" / SCRIPT.name)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=root,
    )


def _patch(text: str, old: str, new: str) -> str:
    assert old in text, f"anchor not found in source: {old!r}"
    return text.replace(old, new, 1)


def test_gate_passes_on_the_real_tree() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[:DIAGNOSTIC_LIMIT]


def test_gate_is_wired_into_ci_justfile_and_the_documented_inventories() -> None:
    """An unregistered gate is unwired code; its absence must fail."""
    ci = (REPO / CI_REL).read_text(encoding="utf-8")
    assert f"python {SCRIPT_REL.as_posix()}" in ci, (
        "gate missing from the CI Quality-contracts job"
    )
    assert "continue-on-error" not in _ci_step_block(ci), "gate step must fail the job"

    justfile = JUSTFILE.read_text(encoding="utf-8")
    recipe = _justfile_recipe(justfile, "quality-contracts")
    assert f"python {SCRIPT_REL.as_posix()}" in recipe, (
        "gate missing from the justfile recipe"
    )

    for doc in ("docs/engineering/conventions.md", "docs/engineering/testing.md"):
        assert SCRIPT_REL.name in (REPO / doc).read_text(encoding="utf-8"), (
            f"gate missing from the documented inventory in {doc}"
        )


def _ci_step_block(ci: str) -> str:
    start = ci.index(f"python {SCRIPT_REL.as_posix()}")
    return ci[max(0, start - 200) : start + 200]


def _justfile_recipe(justfile: str, name: str) -> str:
    lines = justfile.splitlines()
    out, capturing = [], False
    for line in lines:
        if line.startswith(f"{name}:"):
            capturing = True
            continue
        if capturing:
            if line.strip() and not line.startswith((" ", "\t")):
                break
            out.append(line)
    return "\n".join(out)


def test_gate_fails_when_a_nullable_backend_field_is_non_null_in_ts(tmp_path) -> None:
    frontend = (REPO / FRONTEND_REL).read_text(encoding="utf-8")
    broken = _patch(frontend, "claim_sha256: string | null;", "claim_sha256: string;")
    result = _run_on(tmp_path, frontend=broken)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NON-NULL DraftClaim.claim_sha256" in result.stdout


def test_gate_fails_on_reverse_drift(tmp_path) -> None:
    """Backend drops Optional while TS keeps ``| null`` — the direction a
    one-directional gate could never see (review F-005)."""
    backend = (REPO / BACKEND_REL).read_text(encoding="utf-8")
    broken = _patch(
        backend,
        "    span_start: Optional[int]\n    span_end: Optional[int]\n    claim_type: str",
        "    span_start: int\n    span_end: int\n    claim_type: str",
    )
    result = _run_on(tmp_path, backend=broken)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "OVER-NULL DraftClaim.span_start" in result.stdout


def test_gate_is_agnostic_to_pep604_annotations(tmp_path) -> None:
    """``str | None`` must be read as nullable, not skipped (review F-005)."""
    backend = (REPO / BACKEND_REL).read_text(encoding="utf-8")
    frontend = (REPO / FRONTEND_REL).read_text(encoding="utf-8")
    backend = _patch(
        backend, "    claim_sha256: Optional[str]", "    claim_sha256: str | None"
    )
    frontend = _patch(frontend, "claim_sha256: string | null;", "claim_sha256: string;")
    result = _run_on(tmp_path, backend=backend, frontend=frontend)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NON-NULL DraftClaim.claim_sha256" in result.stdout


def test_gate_fails_on_a_field_missing_from_the_ts_interface(tmp_path) -> None:
    """A new backend field with no client counterpart is drift too."""
    backend = (REPO / BACKEND_REL).read_text(encoding="utf-8")
    broken = _patch(
        backend,
        "    claim_type: str\n    status: str\n    severity: str\n    rationale: str",
        "    claim_type: str\n    status: str\n    severity: str\n    rationale: str\n"
        "    verifier_note: Optional[str]",
    )
    result = _run_on(tmp_path, backend=broken)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "verifier_note" in result.stdout


def test_gate_fails_closed_when_a_pinned_field_disappears(tmp_path) -> None:
    """Structural break must abort (exit 3), never pass silently."""
    backend = (REPO / BACKEND_REL).read_text(encoding="utf-8")
    broken = _patch(backend, "    claim_sha256: Optional[str]\n", "")
    result = _run_on(tmp_path, backend=broken)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "ABORT" in result.stderr


def test_gate_fails_closed_when_the_interface_is_gone(tmp_path) -> None:
    frontend = (REPO / FRONTEND_REL).read_text(encoding="utf-8")
    broken = _patch(
        frontend,
        "export interface DraftClaim {",
        "export interface DraftClaimRenamed {",
    )
    result = _run_on(tmp_path, frontend=broken)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "ABORT" in result.stderr
