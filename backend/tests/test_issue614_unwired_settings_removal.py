"""Removal guard for issue #614 (unwired operator-facing settings).

``tri_vector_search_enabled`` and ``flag_embedding_url`` were declared on
``Settings`` but never read anywhere in the app (issue #614) and were
removed outright. This guard pins the removal so the fields cannot be
silently reintroduced without a wiring decision: a future Settings field
must be declared AND consumed, matching the repo's settings-pipeline
contract (SettingsUpdate, ALLOWED_FIELDS, SettingsResponse,
_build_settings_dict, NEW_DIRECT_KEYS).
"""

from app.config import Settings

REMOVED_FIELDS = ("tri_vector_search_enabled", "flag_embedding_url")


def test_issue614_removed_fields_absent_from_model_fields():
    model_fields = Settings.model_fields
    for field in REMOVED_FIELDS:
        assert field not in model_fields, (
            f"Settings.{field} was removed as unwired (issue #614); "
            "reintroduce it only together with its full settings-pipeline "
            "wiring and a consumer, not as a declaration-only field"
        )


def test_issue614_removed_fields_absent_from_settings_instance():
    settings_obj = Settings()
    for field in REMOVED_FIELDS:
        assert not hasattr(settings_obj, field), (
            f"Settings instance should not have attribute {field} - "
            "it was removed as unwired (issue #614)"
        )
