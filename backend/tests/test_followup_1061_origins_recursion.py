"""Issue #1061 follow-up pin (AC5): origins deep-JSON env fails closed.

RED at base, GREEN after the fix: ``parse_backend_cors_origins`` and the
shared ``parse_allowed_model_origins`` validator call ``json.loads`` without
the ``except RecursionError -> ValueError("... nested too deeply")`` wrap
their three sibling validators already carry, so a pathologically deep JSON
array for ``BACKEND_CORS_ORIGINS`` or the model-origins allowlists escapes
Settings construction as an unhandled RecursionError (crash, not a
fail-closed validation error naming the nesting).

Mirrors ``test_deep_json_env_value_fails_closed`` in
``backend/tests/test_config_alignment.py`` (class ``TestEnvDecoding734``):
same env-setting convention (patch.dict on os.environ, Settings with
``_env_file=None``), same ValidationError match on "nested too deeply".
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.config import Settings

DEEP = "[" * 4000 + "]" * 4000


class TestOriginsDeepJsonFailsClosed1061:
    """Deep-JSON origins env values must fail closed, not crash the import.

    RED at base (RecursionError escapes the two unwrapped validators),
    GREEN after the fix (both fail closed like parse_allowed_hosts).
    """

    @pytest.mark.parametrize(
        "env_name",
        [
            "BACKEND_CORS_ORIGINS",
            "DRAFT_ALLOWED_MODEL_ORIGINS",
            "DRAFT_SENSITIVE_ALLOWED_MODEL_ORIGINS",
            "MULTIMODAL_ALLOWED_MODEL_ORIGINS",
        ],
    )
    def test_deep_json_origins_env_fails_closed(self, env_name):
        """RED at base, GREEN after the fix: a pathologically deep JSON
        array for the CORS env or any model-origins allowlist env raises a
        ValidationError naming 'nested too deeply', never an unhandled
        RecursionError escaping pydantic."""
        with patch.dict("os.environ", {env_name: DEEP}):
            with pytest.raises(ValidationError, match="nested too deeply"):
                Settings(_env_file=None)

    def test_deep_json_allowed_hosts_still_fails_closed(self):
        """PRESERVING pin: the already-wrapped parse_allowed_hosts keeps
        raising the same fail-closed ValidationError ('nested too deeply')
        so the fix cannot regress the #734 sibling behavior."""
        with patch.dict("os.environ", {"ALLOWED_HOSTS": DEEP}):
            with pytest.raises(ValidationError, match="nested too deeply"):
                Settings(_env_file=None)
