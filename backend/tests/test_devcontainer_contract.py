"""Issue #567: the devcontainer is a runtime-contract parity surface.

`.devcontainer/devcontainer.json` pins the Python/Node versions CI uses, and
`scripts/check_runtime_contract.py::check_devcontainer` enforces that parity.
These tests drive the check function against synthetic trees (good/bad pins)
plus the real repo surface, so a devcontainer pin can never silently drift
from CI.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_runtime_contract.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_runtime_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_devcontainer(root: Path, features: dict[str, Any] | None) -> None:
    directory = root / ".devcontainer"
    directory.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"name": "test"}
    if features is not None:
        doc["features"] = features
    (directory / "devcontainer.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


def test_good_pins_pass(tmp_path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    _write_devcontainer(
        tmp_path,
        {
            "ghcr.io/devcontainers/features/python:1": {"version": "3.11"},
            "ghcr.io/devcontainers/features/node:1": {"version": "22.14.0"},
        },
    )
    failures: list[str] = []
    module.check_devcontainer(failures)
    assert failures == []


def test_missing_file_fails(tmp_path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    failures: list[str] = []
    module.check_devcontainer(failures)
    assert len(failures) == 1
    assert "devcontainer.json" in failures[0] and "missing" in failures[0]


def test_wrong_node_pin_fails(tmp_path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    _write_devcontainer(
        tmp_path,
        {
            "ghcr.io/devcontainers/features/python:1": {"version": "3.11"},
            "ghcr.io/devcontainers/features/node:1": {"version": "22.23.0"},
        },
    )
    failures: list[str] = []
    module.check_devcontainer(failures)
    assert any("node" in message for message in failures)


def test_wrong_python_pin_fails(tmp_path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    _write_devcontainer(
        tmp_path,
        {
            "ghcr.io/devcontainers/features/python:1": {"version": "3.14"},
            "ghcr.io/devcontainers/features/node:1": {"version": "22.14.0"},
        },
    )
    failures: list[str] = []
    module.check_devcontainer(failures)
    assert any("python" in message for message in failures)


def test_real_repo_surface_passes() -> None:
    module = _load_module()
    failures: list[str] = []
    module.check_devcontainer(failures)
    assert failures == [], f"real devcontainer violates the runtime contract: {failures}"
