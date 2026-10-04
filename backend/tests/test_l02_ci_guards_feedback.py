"""CI reachability wrappers for the issue #773 capability guards.

The guards perform the source-level semantic checks; this test keeps them in
the always-collected backend pytest suite so they cannot remain local-only
scripts or be skipped by a frontend-only change.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARDS = (
    "scripts/check_l02_capability_fixture_keys_feedback.py",
    "scripts/check_l02_capability_reader_keys_feedback.py",
)
DIAGNOSTIC_LIMIT = 12_000


def _run_guard(
    path: str,
    *,
    root: Path = REPO,
    node_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(REPO / path)]
    if root != REPO:
        command.extend(["--root", str(root)])
    environment = os.environ.copy()
    if node_path is not None:
        environment["NODE_PATH"] = str(node_path)
    try:
        return subprocess.run(
            command,
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"{path} could not complete: {exc}")


@pytest.mark.parametrize("guard", GUARDS)
def test_l02_guard_passes_in_backend_ci(guard: str) -> None:
    result = _run_guard(guard)
    diagnostics = (result.stdout + result.stderr)[-DIAGNOSTIC_LIMIT:]
    assert result.returncode == 0, (
        f"{guard} exited {result.returncode}; diagnostics:\n{diagnostics}"
    )


def _synthetic_root(tmp_path: Path, *, fixtures: str, readers: str) -> Path:
    (tmp_path / "backend/app/api/routes").mkdir(parents=True)
    (tmp_path / "frontend/src/components/draft-room").mkdir(parents=True)
    (tmp_path / "frontend/src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend/app/api/routes/draft_room.py").write_text(
        "def get_capabilities():\n"
        "    return DraftRoomCapabilities(limits={\"max_inputs\": 10, \"max_total_input_mb\": 2, \"poll_interval_seconds\": 2, \"max_page_size\": 100})\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend/src/components/draft-room/fixture.test.tsx").write_text(fixtures, encoding="utf-8")
    (tmp_path / "frontend/src/reader.ts").write_text(readers, encoding="utf-8")
    return tmp_path


def test_l02_guard_ownership_is_documented_in_backend_pytest() -> None:
    source = (REPO / "docs/engineering/testing.md").read_text(encoding="utf-8")
    assert "check_l02_capability_fixture_keys_feedback.py" in source
    assert "check_l02_capability_reader_keys_feedback.py" in source
    assert "test_l02_ci_guards_feedback.py" in source
    assert "Backend" in source and "pytest" in source


def test_l02_fixture_guard_covers_ts_and_tsx_shorthand_and_default_forms(tmp_path: Path) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures=(
            "const direct = { limits: { max_inputs: 10 } };\n"
            "const limits = { max_total_input_mb: 2 };\n"
            "const shorthand = { limits };\n"
            "function make(limits = { poll_interval_seconds: 2 }) { return { limits }; }\n"
        ),
        readers="const value = capabilities.limits.max_inputs;\n",
    )
    result = _run_guard(GUARDS[0], root=root, node_path=REPO / "frontend/node_modules")
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    "reader",
    (
        "const alias = capabilities.limits ?? {}; const bad = alias.unknown_nullish;\n",
        "const alias = condition ? capabilities.limits : {}; const bad = alias.unknown_conditional;\n",
        "consume(capabilities.limits);\n",
    ),
)
def test_l02_reader_guard_fails_closed_for_direct_limits_escapes(
    tmp_path: Path,
    reader: str,
) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures="const fixture = { limits: { max_inputs: 10 } };\n",
        readers=reader,
    )
    result = _run_guard(GUARDS[1], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNSUPPORTED reader syntax" in diagnostics


@pytest.mark.parametrize(
    "fixture",
    (
        "const bad = { limits: { unknown_fixture_key: 1 } };\n",
        "const limits = { unknown_shorthand_key: 1 }; const bad = { limits };\n",
        "function make(limits = { unknown_default_key: 1 }) { return { limits }; }\n",
    ),
)
def test_l02_fixture_guard_rejects_unknown_direct_shorthand_and_default_keys(tmp_path: Path, fixture: str) -> None:
    root = _synthetic_root(tmp_path, fixtures=fixture, readers="const value = capabilities.limits.max_inputs;\n")
    result = _run_guard(GUARDS[0], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "FIXTURE-DRIFT" in diagnostics


def test_l02_reader_guard_covers_optional_bracket_destructure_and_alias_reads(tmp_path: Path) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures="const fixture = { limits: { max_inputs: 10 } };\n",
        readers=(
            "const direct = capabilities.limits.max_inputs;\n"
            "const optional = capabilities?.limits?.[\"max_total_input_mb\"];\n"
            "const alias = capabilities.limits; const viaAlias = alias.poll_interval_seconds;\n"
            "const bracketAlias = capabilities[\"limits\"]; const viaBracketAlias = bracketAlias.max_page_size;\n"
            "const { max_inputs, max_total_input_mb: total } = capabilities.limits;\n"
        ),
    )
    result = _run_guard(GUARDS[1], root=root, node_path=REPO / "frontend/node_modules")
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    "reader",
    (
        "const bad = capabilities.limits[\"unknown_bracket_key\"];\n",
        "const alias = capabilities.limits; const bad = alias.unknown_alias_key;\n",
        "const { unknown_destructure_key } = capabilities.limits;\n",
    ),
)
def test_l02_reader_guard_rejects_unknown_bracket_destructure_and_alias_keys(tmp_path: Path, reader: str) -> None:
    root = _synthetic_root(tmp_path, fixtures="const fixture = { limits: { max_inputs: 10 } };\n", readers=reader)
    result = _run_guard(GUARDS[1], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNEMITTED" in diagnostics


@pytest.mark.parametrize(
    "fixture",
    (
        "const max_inputs = 'unknown_dynamic'; const bad = { limits: { [max_inputs]: 1 } };\n",
        "const limits = { max_inputs: 10 }; function make(limits) { return { limits }; }\n",
        "function make(...limits) { return { limits }; }\n",
    ),
)
def test_l02_fixture_guard_fails_closed_for_dynamic_and_shadowed_bindings(
    tmp_path: Path,
    fixture: str,
) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures=fixture,
        readers="const value = capabilities.limits.max_inputs;\n",
    )
    result = _run_guard(GUARDS[0], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNSUPPORTED fixture syntax" in diagnostics


@pytest.mark.parametrize(
    "reader",
    (
        "const max_inputs = 'unknown_dynamic'; const bad = capabilities.limits[max_inputs];\n",
        "let limits; limits = capabilities.limits; const bad = limits.unknown_alias;\n",
        "const limits = capabilities.limits; consume(limits);\n",
        "const { ...rest } = capabilities.limits;\n",
    ),
)
def test_l02_reader_guard_fails_closed_for_dynamic_and_escaping_bindings(
    tmp_path: Path,
    reader: str,
) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures="const fixture = { limits: { max_inputs: 10 } };\n",
        readers=reader,
    )
    result = _run_guard(GUARDS[1], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNSUPPORTED reader syntax" in diagnostics or "UNEMITTED unknown_alias" in diagnostics

@pytest.mark.parametrize(
    "fixture",
    (
        "let limits = { max_inputs: 10 }; limits = { unknown_reassignment: 1 }; const bad = { limits };\n",
        "const limits = { unknown_block_shadow: 1 }; { const limits = { max_inputs: 10 }; } const bad = { limits };\n",
    ),
)
def test_l02_fixture_guard_respects_block_bindings_and_reassignment(
    tmp_path: Path,
    fixture: str,
) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures=fixture,
        readers="const value = capabilities.limits.max_inputs;\n",
    )
    result = _run_guard(GUARDS[0], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics


@pytest.mark.parametrize(
    "fixture",
    (
        "const limits = { max_inputs: 10 }; function make({ limits }) { return { limits }; }\n",
        "const limits = { max_inputs: 10 }; { const { limits } = source; const bad = { limits }; }\n",
        "const limits = { max_inputs: 10 }; function make() { try {} catch ({ limits }) { return { limits }; } }\n",
    ),
)
def test_l02_fixture_guard_fails_closed_for_destructured_limit_bindings(
    tmp_path: Path,
    fixture: str,
) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures=fixture,
        readers="const value = capabilities.limits.max_inputs;\n",
    )
    result = _run_guard(GUARDS[0], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNSUPPORTED fixture syntax" in diagnostics


def test_l02_reader_guard_respects_block_shadowing(tmp_path: Path) -> None:
    root = _synthetic_root(
        tmp_path,
        fixtures="const fixture = { limits: { max_inputs: 10 } };\n",
        readers=(
            "const limits = capabilities.limits;\n"
            "{ const limits = unrelated; }\n"
            "const bad = limits.unknown_block_shadow;\n"
        ),
    )
    result = _run_guard(GUARDS[1], root=root, node_path=REPO / "frontend/node_modules")
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 1, diagnostics
    assert "UNEMITTED unknown_block_shadow" in diagnostics
