"""Regression tests for path-prefix deployments behind reverse proxies."""

from pathlib import Path

import pytest
from fastapi import Response
from pydantic import ValidationError

from app.config import Settings, settings
from app.security import issue_csrf_token
from app.utils.paths import (
    csrf_cookie_path,
    external_path,
    normalize_root_path,
    refresh_cookie_path,
)


class DummyCSRFManager:
    def generate_token(self) -> str:
        return "csrf-token"


def test_normalize_root_path_root_and_prefix_variants():
    assert normalize_root_path(None) == ""
    assert normalize_root_path("") == ""
    assert normalize_root_path("/") == ""
    assert normalize_root_path("knowledgevault") == "/knowledgevault"
    assert normalize_root_path("/knowledgevault") == "/knowledgevault"
    assert normalize_root_path("/knowledgevault/") == "/knowledgevault"


@pytest.mark.parametrize(
    "value",
    [
        " https://example.com/knowledgevault",
        "https://example.com/knowledgevault",
        "//example.com/knowledgevault",
        "/knowledgevault?next=/login",
        "/knowledgevault#hash",
        "/knowledgevault/../admin",
        "/knowledgevault/./admin",
        "/knowledgevault//admin",
        "/knowledgevault;Path=/",
        "/knowledge vault",
        "/knowledge\\vault",
        "/knowledgevault\n",
    ],
)
def test_normalize_root_path_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        normalize_root_path(value)


def test_settings_normalizes_and_validates_app_root_path():
    configured = Settings(
        _env_file=None,
        users_enabled=False,
        admin_secret_token="test-admin-key",
        app_root_path="/knowledgevault/",
    )
    assert configured.app_root_path == "/knowledgevault"

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            users_enabled=False,
            admin_secret_token="test-admin-key",
            app_root_path="/knowledgevault/../admin",
        )


def test_external_cookie_paths_default_to_root(monkeypatch):
    monkeypatch.setattr(settings, "app_root_path", "")

    assert external_path("/api/auth/refresh") == "/api/auth/refresh"
    assert refresh_cookie_path() == "/api/auth/refresh"
    assert csrf_cookie_path() == "/"


def test_external_cookie_paths_include_public_prefix(monkeypatch):
    monkeypatch.setattr(settings, "app_root_path", "/knowledgevault")

    assert external_path("/api/auth/refresh") == "/knowledgevault/api/auth/refresh"
    assert refresh_cookie_path() == "/knowledgevault/api/auth/refresh"
    assert csrf_cookie_path() == "/knowledgevault"


def test_issue_csrf_token_sets_app_root_cookie_path(monkeypatch):
    monkeypatch.setattr(settings, "app_root_path", "/knowledgevault")
    response = Response()

    token = issue_csrf_token(response, DummyCSRFManager())

    assert token == "csrf-token"
    assert "Path=/knowledgevault" in response.headers["set-cookie"]


def test_backend_cors_origins_parses_comma_separated_env(monkeypatch):
    monkeypatch.setenv(
        "BACKEND_CORS_ORIGINS",
        "https://medxs.af.mil, https://admin.medxs.af.mil",
    )

    configured = Settings(
        _env_file=None,
        users_enabled=False,
        admin_secret_token="test-admin-key",
    )

    assert configured.backend_cors_origins == [
        "https://medxs.af.mil",
        "https://admin.medxs.af.mil",
    ]


def test_backend_cors_origins_parses_json_list_env(monkeypatch):
    monkeypatch.setenv(
        "BACKEND_CORS_ORIGINS",
        '["https://medxs.af.mil", "https://admin.medxs.af.mil"]',
    )

    configured = Settings(
        _env_file=None,
        users_enabled=False,
        admin_secret_token="test-admin-key",
    )

    assert configured.backend_cors_origins == [
        "https://medxs.af.mil",
        "https://admin.medxs.af.mil",
    ]


def test_all_refresh_cookie_call_sites_use_external_path_helper():
    auth_source = Path("app/api/routes/auth.py").read_text(encoding="utf-8")

    assert 'path="/api/auth/refresh"' not in auth_source
    assert auth_source.count("path=refresh_cookie_path()") == 6


def test_backend_internal_routes_remain_unprefixed():
    main_source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'root_path=normalize_root_path(settings.app_root_path)' in main_source
    assert 'prefix="/api"' in main_source
    assert 'app.mount(\n            "/assets"' in main_source
    assert "/knowledgevault/api" not in main_source
    assert "/knowledgevault/assets" not in main_source
