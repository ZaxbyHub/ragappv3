"""
Regression tests for issue #231 — reverse-proxy hardening.

Covers four fixes:
1. FORWARDED_ALLOW_IPS docs contradiction (admin-guide no longer recommends *)
2. TrustedHostMiddleware gated on ALLOWED_HOSTS config setting
3. SSE heartbeat keepalive during long generation gaps
4. auth.py session ip_address uses trust-proxy-aware helper (_request_ip)
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (matching test_chat_streaming.py pattern)
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements


class TestForwardedAllowIpsDocs(unittest.TestCase):
    """Fix 1: docs/admin-guide.md no longer recommends FORWARDED_ALLOW_IPS=*."""

    def test_admin_guide_does_not_recommend_star(self):
        """The subpath deployment example must not use FORWARDED_ALLOW_IPS=*."""
        guide_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'docs', 'admin-guide.md'
        )
        with open(guide_path, 'r') as f:
            content = f.read()

        # The env block example must not show FORWARDED_ALLOW_IPS=*
        # Find the minimal configuration block and check it uses a CIDR
        self.assertNotIn(
            'FORWARDED_ALLOW_IPS=*',
            content,
            "admin-guide.md still recommends FORWARDED_ALLOW_IPS=* — "
            "should use a specific CIDR (e.g. 172.16.0.0/12)"
        )


class TestTrustedHostMiddleware(unittest.TestCase):
    """Fix 2: TrustedHostMiddleware is wired and gated on ALLOWED_HOSTS."""

    def test_config_has_allowed_hosts_field(self):
        """Settings must have an allowed_hosts field defaulting to empty list."""
        from app.config import Settings
        # Check the field exists on the model
        field_info = Settings.model_fields.get("allowed_hosts")
        self.assertIsNotNone(field_info, "allowed_hosts field missing from Settings")

    def test_config_parses_comma_separated_allowed_hosts(self):
        """The allowed_hosts validator must parse comma-separated strings."""
        from app.config import Settings
        s = Settings(allowed_hosts="example.com, api.example.com")
        self.assertEqual(s.allowed_hosts, ["example.com", "api.example.com"])

    def test_config_parses_empty_allowed_hosts(self):
        """Empty string should produce an empty list."""
        from app.config import Settings
        s = Settings(allowed_hosts="")
        self.assertEqual(s.allowed_hosts, [])

    def test_main_imports_trusted_host_middleware(self):
        """main.py must import TrustedHostMiddleware."""
        main_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'main.py'
        )
        with open(main_path, 'r') as f:
            content = f.read()
        self.assertIn('TrustedHostMiddleware', content)

    def test_main_gates_on_allowed_hosts(self):
        """TrustedHostMiddleware must be gated on settings.allowed_hosts being non-empty."""
        main_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'main.py'
        )
        with open(main_path, 'r') as f:
            content = f.read()
        self.assertIn('if settings.allowed_hosts:', content)
        self.assertIn('TrustedHostMiddleware', content)

    def test_trusted_host_rejects_unknown_host(self):
        """When ALLOWED_HOSTS is set, requests with unlisted Host are rejected."""
        from fastapi import FastAPI
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        from starlette.testclient import TestClient

        app = FastAPI()
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["trusted.example.com"])

        @app.get("/")
        def read_root():
            return {"hello": "world"}

        client = TestClient(app)

        # Valid host — should pass
        resp = client.get("/", headers={"Host": "trusted.example.com"})
        self.assertEqual(resp.status_code, 200)

        # Invalid host — should be rejected
        resp = client.get("/", headers={"Host": "evil.example.com"})
        self.assertEqual(resp.status_code, 400)


class TestSSEHeartbeat(unittest.TestCase):
    """Fix 3: SSE heartbeat keepalive comment during long generation gaps."""

    def test_chat_stream_has_heartbeat_logic(self):
        """chat.py stream_chat_response must contain heartbeat logic."""
        chat_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'routes', 'chat.py'
        )
        with open(chat_path, 'r') as f:
            content = f.read()
        self.assertIn('heartbeat', content.lower())
        self.assertIn('HEARTBEAT_INTERVAL', content)

    def test_heartbeat_emitted_on_timeout(self):
        """When the RAG engine stalls, a ': heartbeat\\n\\n' comment is emitted."""
        import asyncio
        import json

        # Build a mock RAG engine whose first chunk arrives after a simulated stall.
        # We patch asyncio.wait_for to raise TimeoutError on the first call to
        # simulate a 15s gap, then delegate to the real wait_for for subsequent calls.
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "late"}
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_engine = MagicMock()
        mock_engine.query = mock_query

        from app.api.routes.chat import stream_chat_response

        original_wait_for = asyncio.wait_for
        call_count = [0]

        async def mock_wait_for(coro, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                coro.close()
                raise asyncio.TimeoutError()
            return await original_wait_for(coro, timeout)

        async def run_test():
            results = []
            with patch('app.api.routes.chat.asyncio.wait_for', mock_wait_for):
                response = stream_chat_response(
                    message="test", history=[], rag_engine=mock_engine
                )
                async for chunk in response.body_iterator:
                    results.append(chunk)
            return results

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(run_test())
        finally:
            loop.close()

        heartbeat_found = any(
            isinstance(r, str) and 'heartbeat' in r for r in results
        )
        self.assertTrue(heartbeat_found, "No heartbeat comment found in SSE output")


class TestAuthSessionIpUsesRequestHelper(unittest.TestCase):
    """Fix 4: auth.py session ip_address uses _request_ip, not raw request.client.host."""

    def test_auth_imports_request_ip(self):
        """auth.py must import _request_ip from security_audit."""
        auth_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'routes', 'auth.py'
        )
        with open(auth_path, 'r') as f:
            content = f.read()
        self.assertIn('_request_ip', content)

    def test_auth_no_raw_client_host_for_session_ip(self):
        """auth.py must not use raw request.client.host for session IP capture."""
        auth_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'routes', 'auth.py'
        )
        with open(auth_path, 'r') as f:
            content = f.read()

        # The old pattern: ip_address = request.client.host if request.client else None
        # should no longer appear in the session capture lines
        self.assertNotIn(
            'ip_address = request.client.host',
            content,
            "auth.py still uses raw request.client.host for session IP — "
            "should use _request_ip(request) instead"
        )

    def test_request_ip_respects_trust_proxy_headers(self):
        """_request_ip should read X-Forwarded-For when trust_proxy_headers is True."""
        from app.services.security_audit import _request_ip

        mock_request = MagicMock()
        mock_request.headers = {
            "x-forwarded-for": "203.0.113.5, 10.0.0.1",
        }
        mock_request.client = MagicMock()
        mock_request.client.host = "10.0.0.1"

        with patch('app.services.security_audit.settings') as mock_settings:
            mock_settings.trust_proxy_headers = True
            result = _request_ip(mock_request)
            self.assertEqual(result, "203.0.113.5")

    def test_request_ip_ignores_forwarded_when_trust_off(self):
        """_request_ip should use request.client.host when trust_proxy_headers is False."""
        from app.services.security_audit import _request_ip

        mock_request = MagicMock()
        mock_request.headers = {"x-forwarded-for": "203.0.113.5"}
        mock_request.client = MagicMock()
        mock_request.client.host = "10.0.0.1"

        with patch('app.services.security_audit.settings') as mock_settings:
            mock_settings.trust_proxy_headers = False
            result = _request_ip(mock_request)
            self.assertEqual(result, "10.0.0.1")


if __name__ == '__main__':
    unittest.main()
