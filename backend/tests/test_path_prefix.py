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
    is_unstripped_prefix,
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


# ---------------------------------------------------------------------------
# F-006: is_unstripped_prefix proxy guard unit tests
# ---------------------------------------------------------------------------


class TestIsUnstrippedPrefix:
    def test_detects_prefixed_api_path(self):
        assert is_unstripped_prefix("meridian/api/health", "/meridian") is True

    def test_detects_exact_prefix(self):
        assert is_unstripped_prefix("meridian", "/meridian") is True

    def test_ignores_api_path_without_prefix(self):
        assert is_unstripped_prefix("api/health", "/meridian") is False

    def test_ignores_frontend_route(self):
        assert is_unstripped_prefix("chat", "/meridian") is False

    def test_boundary_safe_no_partial_match(self):
        assert is_unstripped_prefix("meridianx/foo", "/meridian") is False

    def test_disabled_for_root_deploy(self):
        assert is_unstripped_prefix("anything", "") is False

    def test_multi_segment_prefix(self):
        assert is_unstripped_prefix("apps/meridian/api", "/apps/meridian") is True

    def test_multi_segment_no_match(self):
        assert is_unstripped_prefix("apps/other/api", "/apps/meridian") is False


# ---------------------------------------------------------------------------
# F-005: delete_cookie must use refresh_cookie_path() helper
# ---------------------------------------------------------------------------


def test_delete_cookie_uses_refresh_cookie_path():
    """delete_cookie must use the same path helper so the browser actually clears it."""
    import re

    auth_source = Path("app/api/routes/auth.py").read_text(encoding="utf-8")
    # No hardcoded refresh cookie paths — all must use the helper
    assert 'path="/api/auth/refresh"' not in auth_source
    # Verify delete_cookie calls exist and use the helper
    delete_calls = re.findall(r"delete_cookie\([^)]*\)", auth_source)
    assert len(delete_calls) > 0, "Expected at least one delete_cookie call"
    for call in delete_calls:
        if "refresh" in call.lower() or "path=" in call:
            assert "refresh_cookie_path()" in call, (
                f"delete_cookie uses hardcoded path: {call}"
            )


# ---------------------------------------------------------------------------
# N-3: StreamingResponse must include X-Accel-Buffering for nginx proxies
# ---------------------------------------------------------------------------


def test_chat_streaming_responses_have_proxy_headers():
    """All StreamingResponse in chat.py must include X-Accel-Buffering for nginx."""
    import re

    chat_source = Path("app/api/routes/chat.py").read_text(encoding="utf-8")
    # Match StreamingResponse(...) including nested parens like error_generator()
    streaming_blocks = re.findall(
        r"StreamingResponse\((?:[^()]*\([^()]*\)[^()]*)*[^()]*\)",
        chat_source,
        re.DOTALL,
    )
    assert len(streaming_blocks) >= 2, (
        f"Expected >=2 StreamingResponse calls, found {len(streaming_blocks)}"
    )
    for block in streaming_blocks:
        assert "X-Accel-Buffering" in block, (
            f"StreamingResponse missing X-Accel-Buffering header: {block[:80]}"
        )


# ---------------------------------------------------------------------------
# F-006: SPA catch-all must call the proxy guard
# ---------------------------------------------------------------------------


def test_spa_catchall_calls_proxy_guard():
    """SPA catch-all must check for unstripped proxy prefix."""
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    assert "is_unstripped_prefix" in main_source
