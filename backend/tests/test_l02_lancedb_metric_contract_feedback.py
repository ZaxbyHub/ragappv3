"""Issue #773 named constraint: persisted metric aliases and real LanceDB."""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RAG_SETTINGS = ROOT / "frontend" / "src" / "components" / "settings" / "RAGSettings.tsx"


def _has_real_lancedb_query_api(tmp_path: Path) -> tuple[bool, str]:
    try:
        distribution = metadata.distribution("lancedb")
        module = importlib.import_module("lancedb")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        return False, f"real LanceDB package unavailable: {exc}"
    module_file = getattr(module, "__file__", None)
    if not module_file or not callable(getattr(module, "connect", None)):
        return False, (
            "imported LanceDB does not expose a real module file and callable connect"
        )
    try:
        module_path = Path(module_file).resolve()
        package_root = Path(distribution.locate_file(".")).resolve()
        module_path.relative_to(package_root)
    except (OSError, ValueError):
        return False, "imported lancedb module is outside the installed distribution"
    try:
        database = module.connect(str(tmp_path / "lancedb-probe"))
        table = database.create_table(
            "query_probe",
            [{"id": 1, "vector": [1.0, 0.0], "text": "metric contract"}],
        )
    except Exception as exc:  # the probe reports an unavailable real engine below
        return False, f"real LanceDB query probe could not create a table: {exc}"
    if not callable(getattr(table, "search", None)):
        return False, "imported LanceDB table lacks the real search query API"
    return True, ""


def test_each_ui_offered_metric_runs_against_real_float_dense_lancedb_query(
    tmp_path: Path,
) -> None:
    source = RAG_SETTINGS.read_text(encoding="utf-8")
    vector_start = source.index('<Label htmlFor="vector-metric">')
    vector_end = source.index("</Select>", vector_start)
    offered = re.findall(
        r'<SelectItem\s+value="([^"]+)"', source[vector_start:vector_end]
    )
    assert set(offered) == {"l2", "cosine", "dot"}

    available, reason = _has_real_lancedb_query_api(tmp_path)
    if not available:
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(reason)
        pytest.skip(reason)

    lancedb = importlib.import_module("lancedb")
    database = lancedb.connect(str(tmp_path / "lancedb"))
    table = database.create_table(
        "float_dense",
        [{"id": 1, "vector": [1.0, 0.0], "text": "metric contract"}],
    )
    for metric in offered:
        rows = table.search([1.0, 0.0]).distance_type(metric).limit(1).to_list()
        assert rows and rows[0]["id"] == 1, metric


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        (json.dumps("euclidean"), "l2"),
        ("euclidean", "l2"),
        (json.dumps("dot_product"), "dot"),
        ("dot_product", "dot"),
    ],
)
def test_startup_replay_maps_legacy_metric_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted: str, expected: str
) -> None:
    """Existing settings_kv aliases must survive startup as LanceDB metric names."""
    from app.config import settings
    from app.lifespan import _load_persisted_settings

    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO settings_kv (key, value) VALUES ('vector_metric', ?)",
        (persisted,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "vector_metric", "cosine")
    _load_persisted_settings(str(db_path))

    assert settings.vector_metric == expected
