"""Issue #773 effective source provenance checks.

The dotenv test constructs the same Settings object used by the application,
while keeping the dotenv value out of os.environ.  The remaining tests pin the
existing kv precedence and non-admin provenance redaction contracts.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_settings_without_leaking_optional_import_shims():
    """Import the settings route on the dev venv without polluting co-runs."""
    names = (
        "lancedb",
        "pyarrow",
        "unstructured",
        "unstructured.partition",
        "unstructured.partition.auto",
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in names}
    shim_names: set[str] = set()

    def install_shim(name: str, module: types.ModuleType) -> None:
        sys.modules[name] = module
        shim_names.add(name)

    try:
        try:
            import lancedb  # noqa: F401
        except ImportError:
            install_shim("lancedb", types.ModuleType("lancedb"))
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            install_shim("pyarrow", types.ModuleType("pyarrow"))

        try:
            from unstructured.partition.auto import partition  # noqa: F401
        except ImportError:
            unstructured = types.ModuleType("unstructured")
            unstructured.__path__ = []
            partition_pkg = types.ModuleType("unstructured.partition")
            partition_pkg.__path__ = []
            partition_auto = types.ModuleType("unstructured.partition.auto")
            partition_auto.partition = lambda *args, **kwargs: []
            partition_pkg.auto = partition_auto
            unstructured.partition = partition_pkg
            install_shim("unstructured", unstructured)
            install_shim("unstructured.partition", partition_pkg)
            install_shim("unstructured.partition.auto", partition_auto)

        from app.api.routes import settings as settings_route
        from app.config import Settings

        return Settings, settings_route
    finally:
        for name in shim_names:
            value = previous[name]
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


Settings, settings_route = _load_settings_without_leaking_optional_import_shims()


def test_dotenv_only_value_is_labelled_env(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("RERANKER_URL=https://reranker.example.test\n", encoding="utf-8")
    prior = os.environ.pop("RERANKER_URL", None)
    try:
        loaded = Settings(_env_file=dotenv)
        assert loaded.reranker_url == "https://reranker.example.test"
        with patch.object(settings_route, "settings", loaded):
            assert settings_route._compute_effective_sources(None)["reranker_url"] == "env"
    finally:
        if prior is not None:
            os.environ["RERANKER_URL"] = prior


def test_settings_kv_wins_over_environment_source() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings_kv (key TEXT NOT NULL)")
    conn.execute("INSERT INTO settings_kv(key) VALUES ('reranker_url')")
    prior = os.environ.get("RERANKER_URL")
    os.environ["RERANKER_URL"] = "https://reranker.example.test"
    try:
        assert settings_route._compute_effective_sources(conn)["reranker_url"] == "kv"
    finally:
        conn.close()
        if prior is None:
            os.environ.pop("RERANKER_URL", None)
        else:
            os.environ["RERANKER_URL"] = prior


def test_non_admin_does_not_receive_redacted_source_provenance() -> None:
    payload = {
        "reranker_url": "https://internal.example.test",
        "effective_sources": {"reranker_url": "env", "chunk_size_chars": "default"},
    }

    redacted = settings_route._redact_infra_for_non_admin(payload, "viewer")

    assert redacted["reranker_url"] == ""
    assert "reranker_url" not in redacted["effective_sources"]
    assert redacted["effective_sources"]["chunk_size_chars"] == "default"
