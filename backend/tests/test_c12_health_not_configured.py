"""Issue #717 acceptance check — AC5 (unconfigured chat health status).

DISCRIMINATING. RED at base (master), GREEN after the fix.

* AC5 (not_configured health status): when no chat endpoint is configured
  (empty chat URL, no injected client), ``LLMHealthChecker.check_chat()``
  builds a default ``LLMClient``, whose constructor feeds the empty chat
  URL straight into ``assert_url_safe`` — which raises ``URLBlocked("URL
  is empty.")``. ``check_chat`` swallows that as a generic
  ``{"ok": False, "error": "Chat service unexpected error: URL is empty."}``
  — an "unhealthy" verdict with NO ``status`` key. A fresh install with no
  chat backend therefore reports a service DOWN instead of the designed
  "not configured" state (the model checker already short-circuits empty
  pairs to ``status: not_configured``; the health checker must match).

Offline and deterministic: the constructor fails before any network I/O.
"""

import os
import sys
import unittest
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.services.llm_health import LLMHealthChecker


class TestHealthNotConfigured(unittest.IsolatedAsyncioTestCase):
    """AC5 (issue #717): unconfigured chat must surface not_configured."""

    async def test_unconfigured_chat_reports_not_configured(self):
        """AC5: WHEN ``LLMHealthChecker.check_chat()`` runs with no chat
        endpoint configured (empty chat URL, no injected client) THE SYSTEM
        SHALL return a result whose ``status`` is ``"not_configured"``.

        RED at base (master), GREEN after the fix.
        """
        # LLMClient.__init__ reads settings.ollama_chat_url as the default
        # chat base_url (and passes it to assert_url_safe), so emptying that
        # field is the "no chat endpoint configured" arrangement. No client
        # is injected, forcing check_chat down the construct-locally path.
        with patch.object(settings, "ollama_chat_url", ""):
            result = await LLMHealthChecker(timeout=5).check_chat()

        self.assertEqual(
            result.get("status"),
            "not_configured",
            f"unconfigured chat must surface status=not_configured, got: {result}",
        )
