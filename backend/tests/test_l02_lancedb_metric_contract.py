"""Issue #773 named constraint: persisted metric aliases and real LanceDB."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import lancedb
import pytest

ROOT = Path(__file__).resolve().parents[2]
RAG_SETTINGS = ROOT / "frontend" / "src" / "components" / "settings" / "RAGSettings.tsx"


def test_each_ui_offered_metric_runs_against_real_float_dense_lancedb_query(tmp_path: Path) -> None:
    source = RAG_SETTINGS.read_text(encoding="utf-8")
    vector_start = source.index('<Label htmlFor="vector-metric">')
    vector_end = source.index("</Select>", vector_start)
    offered = re.findall(
        r'<SelectItem\s+value="([^"]+)"', source[vector_start:vector_end]
    )
    assert offered, "RAGSettings must expose at least one vector metric"

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

    database = lancedb.connect(str(tmp_path / "replay-lancedb"))
    table = database.create_table(
        "replayed_metric",
        [{"id": 1, "vector": [1.0, 0.0]}],
    )
    rows = table.search([1.0, 0.0]).distance_type(expected).limit(1).to_list()
    assert rows and rows[0]["id"] == 1
