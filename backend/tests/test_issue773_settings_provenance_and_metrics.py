"""Focused regression coverage for #773 settings provenance and metric replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from app.api.routes import settings as settings_route
from app.config import Settings, settings
from app.lifespan import _load_persisted_settings


def test_dotenv_source_uses_the_settings_instances_configured_file(tmp_path: Path) -> None:
    dotenv = tmp_path / "operator.env"
    dotenv.write_text("RERANKER_URL=https://dotenv.example.test\n", encoding="utf-8")

    loaded = Settings(_env_file=dotenv)

    with patch.object(settings_route, "settings", loaded):
        assert settings_route._compute_effective_sources(None)["reranker_url"] == "env"


def test_empty_environment_wins_over_dotenv_and_keeps_default_badge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / "operator.env"
    dotenv.write_text("RERANKER_URL=https://dotenv.example.test\n", encoding="utf-8")
    monkeypatch.setenv("RERANKER_URL", "")
    loaded = Settings(_env_file=dotenv)

    with patch.object(settings_route, "settings", loaded):
        assert settings_route._compute_effective_sources(None)["reranker_url"] == "default"


def test_settings_without_a_configured_dotenv_keeps_the_default_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RERANKER_URL", raising=False)
    loaded = Settings(_env_file=None)

    with patch.object(settings_route, "settings", loaded):
        assert settings_route._compute_effective_sources(None)["reranker_url"] == "default"


def test_environment_alias_uses_the_shared_metric_canonicalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTOR_METRIC", "euclidean")

    assert Settings(_env_file=None).vector_metric == "l2"


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("euclidean", "l2"),
        ("dot_product", "dot"),
        ("unrecognized_metric", "unrecognized_metric"),
    ],
)
def test_settings_update_normalizes_aliases_before_live_apply_and_persistence(
    monkeypatch: pytest.MonkeyPatch, submitted: str, expected: str
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    update = settings_route.SettingsUpdate(vector_metric=submitted)
    values = settings_route._validate_settings_update(update)
    monkeypatch.setattr(settings, "vector_metric", "cosine")
    try:
        settings_route._persist_settings(conn, update)
        settings_route._apply_validated_settings(values)

        assert settings.vector_metric == expected
        persisted = conn.execute(
            "SELECT value FROM settings_kv WHERE key = 'vector_metric'"
        ).fetchone()
        assert persisted is not None
        assert json.loads(persisted[0]) == expected
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        (json.dumps("euclidean"), "l2"),
        ("euclidean", "l2"),
        (json.dumps("dot_product"), "dot"),
        ("dot_product", "dot"),
        (json.dumps("unrecognized_metric"), "unrecognized_metric"),
    ],
)
def test_persisted_metric_replay_canonicalizes_only_legacy_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted: str,
    expected: str,
) -> None:
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
