"""Issue #776 L05 CI guard tests (review PRR-014).

The two census scripts gate CI (Quality contracts job + justfile), so their
behavior is pinned here following the repo's gate-test precedents
(``test_l02_ci_guards.py`` subprocess runs, ``test_settings_consumers_gate``
wiring pins): both scripts pass on the real tree, both are wired into ci.yml
and the justfile, both fail on synthetic violating trees, and both fail closed
(exit 2) when their scanned tree is missing.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
DIAGNOSTIC_LIMIT = 12_000

RAW_PALETTE_SCRIPT = SCRIPTS / "check_l05_raw_palette.py"
PAGE_HEADERS_SCRIPT = SCRIPTS / "check_l05_page_headers.py"


def _run_guard(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO,
        timeout=120,
        capture_output=True,
        text=True,
    )


def _load_module(script: Path):
    spec = importlib.util.spec_from_file_location(script.stem, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "script",
    [RAW_PALETTE_SCRIPT, PAGE_HEADERS_SCRIPT],
    ids=["raw-palette", "page-headers"],
)
def test_guard_passes_on_real_tree(script: Path) -> None:
    result = _run_guard(script)
    assert result.returncode == 0, result.stdout[-DIAGNOSTIC_LIMIT:] + result.stderr[-DIAGNOSTIC_LIMIT:]


@pytest.mark.parametrize(
    "script",
    [RAW_PALETTE_SCRIPT, PAGE_HEADERS_SCRIPT],
)
def test_guard_wired_into_ci_and_justfile(script: Path) -> None:
    name = script.name
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    justfile = (REPO / "justfile").read_text(encoding="utf-8")
    assert f"python scripts/{name}" in ci, f"{name} missing from ci.yml"
    assert f"python scripts/{name}" in justfile, f"{name} missing from justfile"


def test_raw_palette_scan_flags_violation(tmp_path: Path) -> None:
    module = _load_module(RAW_PALETTE_SCRIPT)
    src = tmp_path / "frontend" / "src"
    (src / "components" / "ui").mkdir(parents=True)
    (src / "Page.tsx").write_text(
        'export const A = () => <div className="bg-amber-500 text-primary">x</div>;\n',
        encoding="utf-8",
    )
    (src / "components" / "ui" / "Vendor.tsx").write_text(
        'export const V = () => <div className="bg-red-500" />;\n', encoding="utf-8"
    )
    (src / "Skipped.test.tsx").write_text(
        'export const T = () => <div className="bg-red-500" />;\n', encoding="utf-8"
    )
    hits, total = module.scan(src, tmp_path, allow_lines=set(), allow_files=set())
    assert total == 1
    assert hits == ["frontend/src/Page.tsx:1:bg-amber-500"]


def test_raw_palette_scan_honours_allowlist(tmp_path: Path) -> None:
    module = _load_module(RAW_PALETTE_SCRIPT)
    src = tmp_path / "frontend" / "src"
    src.mkdir(parents=True)
    (src / "Page.tsx").write_text(
        'export const A = () => <div className="bg-amber-500">x</div>;\n', encoding="utf-8"
    )
    hits, total = module.scan(
        src,
        tmp_path,
        allow_lines={"frontend/src/Page.tsx:1"},
        allow_files=set(),
    )
    assert total == 0 and hits == []
    # A line shift re-binds the line-keyed exemption (documented hazard).
    hits, total = module.scan(
        src,
        tmp_path,
        allow_lines={"frontend/src/Page.tsx:2"},
        allow_files=set(),
    )
    assert total == 1


def test_page_headers_scan_flags_own_h1(tmp_path: Path) -> None:
    module = _load_module(PAGE_HEADERS_SCRIPT)
    pages = tmp_path / "frontend" / "src" / "pages"
    canvas = tmp_path / "frontend" / "src" / "components" / "canvas"
    pages.mkdir(parents=True)
    canvas.mkdir(parents=True)
    (pages / "Alpha.tsx").write_text("export const A = () => <h1>x</h1>;\n", encoding="utf-8")
    (canvas / "CanvasPage.tsx").write_text(
        'export const C = () => <h1 id="c">y</h1>;\n', encoding="utf-8"
    )
    (pages / "Skip.test.tsx").write_text("export const T = () => <h1>t</h1>;\n", encoding="utf-8")
    hits = module.scan((pages, canvas), tmp_path)
    assert sorted(hits) == [
        "frontend/src/components/canvas/CanvasPage.tsx:1",
        "frontend/src/pages/Alpha.tsx:1",
    ]


@pytest.mark.parametrize(
    "script",
    [RAW_PALETTE_SCRIPT, PAGE_HEADERS_SCRIPT],
)
def test_guard_fails_closed_on_missing_tree(script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module(script)
    missing = REPO / "frontend" / "src" / "__does_not_exist__"
    if script.name == "check_l05_raw_palette.py":
        monkeypatch.setattr(module, "FRONTEND_SRC", missing)
    else:
        monkeypatch.setattr(
            module,
            "SCAN_DIRS",
            (missing, REPO / "frontend" / "src" / "__also_missing__"),
        )
    assert module.main() == 2
