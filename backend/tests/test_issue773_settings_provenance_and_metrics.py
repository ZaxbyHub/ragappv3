"""Focused regression coverage for #773 settings provenance and metric replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_settings import SettingsError

from app.api.routes import settings as settings_route
from app.config import Settings, settings
from app.lifespan import (
    _batch_validate_persisted,
    _load_persisted_settings,
    _validate_setting_value,
)


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

_HIDDEN_FEATURE_FLAGS = (
    "wiki_llm_curator_enabled",
    "multimodal_enrichment_enabled",
    "multimodal_query_vision_enabled",
)


@pytest.mark.parametrize("source", ("process_environment", "dotenv", "kv"))
@pytest.mark.parametrize(
    ("role", "redacted"),
    (("viewer", True), ("member", True), ("admin", False), ("superadmin", False)),
)
def test_hidden_feature_flag_provenance_is_role_safe_for_each_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    role: str,
    redacted: bool,
) -> None:
    conn: sqlite3.Connection | None = None
    if source == "process_environment":
        for field in _HIDDEN_FEATURE_FLAGS:
            monkeypatch.setenv(field.upper(), "true")
        loaded = Settings(_env_file=None)
        expected_source = "env"
    elif source == "dotenv":
        dotenv = tmp_path / "operator.env"
        dotenv.write_text(
            "\n".join(f"{field.upper()}=true" for field in _HIDDEN_FEATURE_FLAGS),
            encoding="utf-8",
        )
        loaded = Settings(_env_file=dotenv)
        expected_source = "env"
    else:
        loaded = Settings(_env_file=None)
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE settings_kv (key TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO settings_kv(key) VALUES (?)",
            [(field,) for field in _HIDDEN_FEATURE_FLAGS],
        )
        expected_source = "kv"

    try:
        with patch.object(settings_route, "settings", loaded):
            payload = {
                **{field: True for field in _HIDDEN_FEATURE_FLAGS},
                "effective_sources": settings_route._compute_effective_sources(conn),
            }
            response = settings_route._redact_infra_for_non_admin(payload, role)
    finally:
        if conn is not None:
            conn.close()

    for field in _HIDDEN_FEATURE_FLAGS:
        if redacted:
            assert response[field] is False
            assert field not in response["effective_sources"]
        else:
            assert response[field] is True
            assert response["effective_sources"][field] == expected_source


@pytest.mark.parametrize("failing_kind", ("environment", "dotenv"))
@pytest.mark.parametrize("phase", ("construction", "invocation"))
def test_source_inspection_failure_preserves_the_other_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_kind: str,
    phase: str,
) -> None:
    secret = "source-inspection-private-value"

    class FailingSource:
        def __init__(self, *args, **kwargs) -> None:
            if phase == "construction":
                raise OSError(secret)

        def __call__(self) -> dict:
            if phase == "invocation":
                raise SettingsError(secret)
            return {}

    if failing_kind == "environment":
        dotenv = tmp_path / "operator.env"
        dotenv.write_text("RERANKER_URL=https://dotenv.example.test\n", encoding="utf-8")
        loaded = Settings(_env_file=dotenv)
        patched_name = "EnvSettingsSource"
    else:
        monkeypatch.setenv("RERANKER_URL", "https://process.example.test")
        loaded = Settings(_env_file=None)
        patched_name = "DotEnvSettingsSource"

    caplog.set_level("WARNING", logger="app.api.routes.settings")
    with patch.object(settings_route, patched_name, FailingSource), patch.object(
        settings_route, "settings", loaded
    ):
        assert settings_route._compute_effective_sources(None)["reranker_url"] == "env"

    assert secret not in caplog.text


def test_dotenv_inspection_error_retains_kv_badge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "dotenv-inspection-private-value"

    class FailingDotEnvSource:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self) -> dict:
            raise OSError(secret)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings_kv (key TEXT NOT NULL)")
    conn.execute("INSERT INTO settings_kv(key) VALUES ('reranker_url')")
    loaded = Settings(_env_file=None)
    caplog.set_level("WARNING", logger="app.api.routes.settings")
    try:
        with (
            patch.object(settings_route, "DotEnvSettingsSource", FailingDotEnvSource),
            patch.object(settings_route, "settings", loaded),
        ):
            assert settings_route._compute_effective_sources(conn)["reranker_url"] == "kv"
    finally:
        conn.close()

    assert secret not in caplog.text

def test_persisted_metric_alias_is_canonicalized_before_batch_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "settings.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO settings_kv (key, value) VALUES (?, ?)",
        [
            ("chunk_size_chars", json.dumps(150)),
            ("chunk_overlap_chars", json.dumps(100)),
            ("vector_metric", json.dumps("euclidean")),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "chunk_size_chars", 2000)
    monkeypatch.setattr(settings, "chunk_overlap_chars", 200)
    monkeypatch.setattr(settings, "vector_metric", "cosine")

    with patch(
        "app.lifespan._batch_validate_persisted",
        wraps=_batch_validate_persisted,
    ) as batch_validate:
        _load_persisted_settings(str(db_path))

    batch_values = batch_validate.call_args.args[0]
    assert batch_values["vector_metric"] == "l2"
    assert settings.chunk_size_chars == 150
    assert settings.chunk_overlap_chars == 100
    assert settings.vector_metric == "l2"


def test_persisted_validation_warning_omits_value_and_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="app.lifespan")

    assert not _validate_setting_value("chunk_size_chars", "private-value")

    assert "chunk_size_chars" in caplog.text
    assert "private-value" not in caplog.text


def test_batch_replay_warning_omits_value_and_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="app.lifespan")

    assert not _batch_validate_persisted({"chunk_size_chars": "private-value"})

    assert "Persisted settings rejected as a set" in caplog.text
    assert "private-value" not in caplog.text
