"""Issue #652: per-mode LLM client read timeouts are settings-backed.

The factories previously hardcoded ``timeout=300.0`` (editorial/thinking) and
``timeout=120.0`` (instant), so operators could not raise the read timeout for
a slow model deployment. These tests pin the new contract:

- ``Settings`` declares the three ``*_request_timeout_seconds`` fields with
  the pre-#652 constants as defaults;
- every factory resolves its timeout from the settings singleton;
- an explicit ``timeout=`` argument still wins;
- all five settings-pipeline wiring points carry the fields (SettingsUpdate,
  ALLOWED_FIELDS, SettingsResponse, ``_build_settings_dict``, and the
  function-local ``NEW_DIRECT_KEYS`` list in ``_load_persisted_settings`` —
  the fifth is the restart-persistence key, extracted from source like
  ``test_issue494_settings_replay_drift`` does).
"""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.routes.settings import (
    ALLOWED_FIELDS,
    SettingsResponse,
    SettingsUpdate,
    _build_settings_dict,
)
from app.config import Settings, settings
from app.services.llm_client import (
    create_editorial_client,
    create_instant_client,
    create_thinking_client,
)

FIELDS = (
    "thinking_request_timeout_seconds",
    "editorial_request_timeout_seconds",
    "instant_request_timeout_seconds",
)
DEFAULTS = {
    "thinking_request_timeout_seconds": 300.0,
    "editorial_request_timeout_seconds": 300.0,
    "instant_request_timeout_seconds": 120.0,
}
FACTORY_FOR_FIELD = {
    "thinking_request_timeout_seconds": create_thinking_client,
    "editorial_request_timeout_seconds": create_editorial_client,
    "instant_request_timeout_seconds": create_instant_client,
}


def _lifespan_new_direct_keys():
    """Extract the function-local NEW_DIRECT_KEYS list from lifespan.py."""
    source = Path(__file__).resolve().parents[1] / "app" / "lifespan.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "NEW_DIRECT_KEYS":
                    if isinstance(node.value, ast.List):
                        return [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        ]
    raise AssertionError("NEW_DIRECT_KEYS list not found in lifespan.py")


def test_settings_declare_timeout_fields_with_pre652_defaults():
    fresh = Settings(_env_file=None)
    for field, default in DEFAULTS.items():
        assert getattr(fresh, field) == default


def test_factories_read_timeout_from_settings(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
    for field, factory in FACTORY_FOR_FIELD.items():
        monkeypatch.setattr(settings, field, 7.5, raising=False)
        assert factory().timeout == 7.5, field
        monkeypatch.setattr(settings, field, 0.25, raising=False)
        assert factory().timeout == 0.25, field


def test_explicit_timeout_argument_wins(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
    monkeypatch.setattr(settings, "thinking_request_timeout_seconds", 7.5)
    assert create_thinking_client(timeout=42.0).timeout == 42.0


def test_factories_do_not_hardcode_timeout_defaults(monkeypatch):
    # A wrong fix that keeps a numeric literal default would leave the client
    # timeout unchanged when the setting moves; prove the settings value is
    # the only default source.
    monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")
    monkeypatch.setattr(settings, "thinking_request_timeout_seconds", 300.0)
    assert create_thinking_client().timeout == 300.0
    monkeypatch.setattr(settings, "thinking_request_timeout_seconds", 301.0)
    assert create_thinking_client().timeout == 301.0


def test_all_five_settings_pipeline_wiring_points():
    new_direct_keys = _lifespan_new_direct_keys()
    for field in FIELDS:
        assert field in ALLOWED_FIELDS
        assert field in SettingsResponse.model_fields
        assert SettingsResponse.model_fields[field].default == DEFAULTS[field]
        assert field in SettingsUpdate.model_fields
        assert field in _build_settings_dict()
        assert field in new_direct_keys


def test_build_settings_dict_uses_singleton_values(monkeypatch):
    monkeypatch.setattr(settings, "thinking_request_timeout_seconds", 12.5)
    assert _build_settings_dict()["thinking_request_timeout_seconds"] == 12.5


def test_settings_level_validator_rejects_bad_env_values():
    # The env/.env construction path validates the same bounds as the
    # PUT /api/settings path (#654 review F-04/F-05): a zero/negative or
    # absurd (> 24 h) timeout must fail loudly at startup, not per-request.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, thinking_request_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, instant_request_timeout_seconds=-5)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, editorial_request_timeout_seconds=86401)
    ok = Settings(_env_file=None, thinking_request_timeout_seconds=86400)
    assert ok.thinking_request_timeout_seconds == 86400.0
