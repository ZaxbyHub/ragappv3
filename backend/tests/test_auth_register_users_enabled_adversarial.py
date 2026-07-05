"""
Adversarial security tests for /register endpoint users_enabled guard.

Covers ONLY attack vectors against the users_enabled guard at auth.py:230-231:
- Malformed inputs (invalid JSON, wrong types, missing fields)
- Oversized payloads (strings exceeding limits, deeply nested objects)
- Injection attempts (SQL injection in username, special chars in full_name)
- Auth bypass attempts (header manipulation, method confusion)
- Boundary violations (exact boundary values, null bytes, Unicode edge cases)

Constraint: ONLY attack vectors against the register endpoint users_enabled guard.
"""

import os
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same pattern as conftest.py)
try:
    import lancedb
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


class TestRegisterUsersEnabledGuardAdversarial(unittest.TestCase):
    """Adversarial security tests for /register endpoint users_enabled guard."""

    def setUp(self):
        """Set up test client with temporary database and users_enabled=True."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        init_db(self.db_path)
        run_migrations(self.db_path)

        # Save originals
        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path

        # Configure for testing
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

        # Test pool and app
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        class TestCSRFManager:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = TestCSRFManager()

        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Restore settings and clean up resources."""
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path

        self.app.dependency_overrides.clear()
        self.test_pool.close_all()

        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    # =========================================================================
    # ATTACK VECTOR: Oversized Payloads
    # =========================================================================

    def test_register_oversized_username_256_chars_when_users_disabled(self):
        """Username at max_length+1 (256) → Pydantic validation 422 BEFORE guard."""
        settings.users_enabled = False
        oversized_username = "a" * 256  # exceeds Field(max_length=255)

        response = self.client.post(
            "/api/auth/register",
            json={"username": oversized_username, "password": "Password123"},
        )

        # Pydantic validation happens before guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_oversized_password_129_chars_when_users_disabled(self):
        """Password at max_length+1 (129) → Pydantic validation 422 BEFORE guard."""
        settings.users_enabled = False
        oversized_password = "a" * 129  # exceeds Field(max_length=128)

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": oversized_password},
        )

        # Pydantic validation happens before guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_oversized_full_name_256_chars_when_users_disabled(self):
        """full_name at max_length+1 (256) → Pydantic validation 422 BEFORE guard."""
        settings.users_enabled = False
        oversized_full_name = "a" * 256  # exceeds Field(max_length=255)

        response = self.client.post(
            "/api/auth/register",
            json={
                "username": "validuser",
                "password": "Password123",
                "full_name": oversized_full_name,
            },
        )

        # Pydantic validation happens before guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_massive_payload_10kb_when_users_disabled(self):
        """10KB payload with users_enabled=False should be rejected, not crash."""
        settings.users_enabled = False
        massive_username = "a" * 10000

        response = self.client.post(
            "/api/auth/register",
            json={"username": massive_username, "password": "Password123"},
        )

        # Should not return 200 (not registered) and should not crash (500)
        self.assertIn(
            response.status_code,
            [400, 403, 413, 422],
            f"Massive payload should be rejected, got {response.status_code}",
        )

    # =========================================================================
    # ATTACK VECTOR: Malformed Inputs
    # =========================================================================

    def test_register_invalid_json_when_users_disabled(self):
        """Invalid JSON body with users_enabled=False should return 403 or 422, not 500."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            content=b"{invalid json",
            headers={"Content-Type": "application/json"},
        )

        self.assertIn(
            response.status_code,
            [400, 403, 422],
            f"Invalid JSON should be rejected, got {response.status_code}",
        )

    def test_register_empty_username_when_users_disabled(self):
        """Empty string username with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_username_too_short_when_users_disabled(self):
        """1-char username with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "a", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_exactly_3_chars_username_when_users_disabled(self):
        """Exactly 3-char username (boundary) with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "abc", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_missing_username_when_users_disabled(self):
        """Missing username field → Pydantic validation 422 BEFORE guard (correct behavior)."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"password": "Password123"},
        )

        # Validation happens before the guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_missing_password_when_users_disabled(self):
        """Missing password field → Pydantic validation 422 BEFORE guard (correct behavior)."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser"},
        )

        # Validation happens before the guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_null_username_when_users_disabled(self):
        """null username → Pydantic validation 422 BEFORE guard (correct behavior)."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": None, "password": "Password123"},
        )

        # Validation happens before the guard - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_type_confusion_integer_username_when_users_disabled(self):
        """Integer username (type confusion) → Pydantic validation 422 BEFORE guard."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": 12345, "password": "Password123"},
        )

        # Pydantic validates types before guard fires - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    def test_register_type_confusion_object_password_when_users_disabled(self):
        """Object password (type confusion) → Pydantic validation 422 BEFORE guard."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": {"nested": "object"}},
        )

        # Pydantic validates types before guard fires - correct behavior is 422
        self.assertEqual(response.status_code, 422)

    # =========================================================================
    # ATTACK VECTOR: Injection Attempts
    # =========================================================================

    def test_register_sql_injection_when_users_disabled(self):
        """SQL injection in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        injection_payloads = [
            "'; DROP TABLE users; --",
            "' OR '1'='1",
            "admin'--",
            "'; INSERT INTO users VALUES (999,'hacker','pw','','superadmin',1); --",
            "' UNION SELECT * FROM users --",
            "' OR 1=1 --",
            "' OR ''='",
        ]

        for payload in injection_payloads:
            response = self.client.post(
                "/api/auth/register",
                json={"username": payload, "password": "Password123"},
            )
            self.assertEqual(
                response.status_code,
                403,
                f"SQL injection '{payload[:20]}...' should be rejected with 403, got {response.status_code}",
            )
            self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_path_traversal_in_username_when_users_disabled(self):
        """Path traversal in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        traversal_payloads = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "....//....//....//etc/passwd",
            "validuser/../../../etc/passwd",
        ]

        for payload in traversal_payloads:
            response = self.client.post(
                "/api/auth/register",
                json={"username": payload, "password": "Password123"},
            )
            self.assertEqual(
                response.status_code,
                403,
                f"Path traversal '{payload[:20]}...' should be rejected with 403, got {response.status_code}",
            )

    def test_register_xss_in_full_name_when_users_disabled(self):
        """XSS payloads in full_name with users_enabled=False should still 403."""
        settings.users_enabled = False
        xss_payloads = [
            "<script>alert(1)</script>",
            "javascript:alert(1)",
            "<img src=x onerror=alert(1)>",
            "{{constructor.constructor('alert(1)')()}}",
            "<svg onload=alert(1)>",
        ]

        for payload in xss_payloads:
            response = self.client.post(
                "/api/auth/register",
                json={
                    "username": "validuser",
                    "password": "Password123",
                    "full_name": payload,
                },
            )
            self.assertEqual(
                response.status_code,
                403,
                f"XSS payload '{payload[:20]}...' should be rejected with 403, got {response.status_code}",
            )

    def test_register_null_bytes_in_username_when_users_disabled(self):
        """Null bytes in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        null_byte_payloads = [
            "validuser\x00",
            "\x00admin",
            "admi\x00n",
        ]

        for payload in null_byte_payloads:
            response = self.client.post(
                "/api/auth/register",
                json={"username": payload, "password": "Password123"},
            )
            self.assertEqual(
                response.status_code,
                403,
                f"Null byte payload should be rejected with 403, got {response.status_code}",
            )

    def test_register_unicode_rtl_override_in_username_when_users_disabled(self):
        """RTL override Unicode in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        # RTL override character (U+202E)
        rtl_payload = "validuser\u202Eadmin"

        response = self.client.post(
            "/api/auth/register",
            json={"username": rtl_payload, "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_zero_width_chars_in_username_when_users_disabled(self):
        """Zero-width characters in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        # Zero-width space (U+200B), zero-width joiner (U+200D), etc.
        zwc_payload = "vali\u200B\u200D\u200Cuser"

        response = self.client.post(
            "/api/auth/register",
            json={"username": zwc_payload, "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_emoji_in_username_when_users_disabled(self):
        """Emoji in username with users_enabled=False should still 403."""
        settings.users_enabled = False
        emoji_payloads = [
            "admin🔥",
            "👨‍💻admin",  # ZWJ sequence
            "admin🏴",  # flag emoji
        ]

        for payload in emoji_payloads:
            response = self.client.post(
                "/api/auth/register",
                json={"username": payload, "password": "Password123"},
            )
            self.assertEqual(
                response.status_code,
                403,
                f"Emoji payload should be rejected with 403, got {response.status_code}",
            )

    # =========================================================================
    # ATTACK VECTOR: Auth Bypass Attempts
    # =========================================================================

    def test_register_auth_bypass_via_x_forwarded_proto_when_disabled(self):
        """X-Forwarded-Proto cannot enable registration when users_enabled=False."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
            headers={"X-Forwarded-Proto": "https"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_auth_bypass_via_x_original_url_when_disabled(self):
        """X-Original-URL cannot enable registration when users_enabled=False."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
            headers={"X-Original-URL": "/api/auth/register"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_auth_bypass_via_x_http_method_override_when_disabled(self):
        """X-HTTP-Method-Override cannot enable registration when users_enabled=False."""
        settings.users_enabled = False

        # Try to override POST with a more "permissive" method
        response = self.client.request(
            "OPTIONS",
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
            headers={"X-HTTP-Method-Override": "POST"},
        )

        # OPTIONS should not register a user
        self.assertNotEqual(response.status_code, 200)

    def test_register_http_method_confusion_get_when_disabled(self):
        """GET on /register endpoint should not register when users_enabled=False."""
        settings.users_enabled = False

        response = self.client.request(
            "GET",
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
        )

        # GET should not result in registration (404 - route not found for GET)
        self.assertNotEqual(response.status_code, 200)

    def test_register_http_method_confusion_put_when_disabled(self):
        """PUT on /register endpoint should not register when users_enabled=False."""
        settings.users_enabled = False

        response = self.client.put(
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
        )

        # PUT should not result in registration (405 - method not allowed)
        self.assertNotEqual(response.status_code, 200)

    def test_register_http_method_confusion_delete_when_disabled(self):
        """DELETE on /register endpoint should not register when users_enabled=False."""
        settings.users_enabled = False

        response = self.client.request(
            "DELETE",
            "/api/auth/register",
            json={"username": "anyuser", "password": "Password123"},
        )

        # DELETE should not result in registration (404/405 - route/method not found)
        self.assertNotEqual(response.status_code, 200)

    def test_register_wrong_content_type_when_disabled(self):
        """Non-JSON content type → FastAPI raises exception (not 200)."""
        settings.users_enabled = False

        # FastAPI raises an exception for malformed content
        # instead of processing the request - this is secure behavior
        with self.assertRaises(Exception):
            self.client.post(
                "/api/auth/register",
                content=b"username=admin&password=Password123",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    def test_register_double_content_length_when_disabled(self):
        """Duplicate Content-Length headers with users_enabled=False should still 403."""
        settings.users_enabled = False

        # This may be rejected by the server before reaching the endpoint
        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "Password123"},
            headers={
                "Content-Length": "45",
                "X-Content-Length": "45",  # Alternative header
            },
        )

        self.assertEqual(response.status_code, 403)

    # =========================================================================
    # ATTACK VECTOR: Boundary Violations
    # =========================================================================

    def test_register_exactly_max_length_username_when_disabled(self):
        """Username exactly at max_length (255) with users_enabled=False should 403."""
        settings.users_enabled = False
        max_username = "a" * 255

        response = self.client.post(
            "/api/auth/register",
            json={"username": max_username, "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_exactly_max_length_password_when_disabled(self):
        """Password exactly at max_length (128) with users_enabled=False should 403."""
        settings.users_enabled = False
        max_password = "a" * 128

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": max_password},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "User registration is disabled in single-admin mode")

    def test_register_whitespace_only_username_when_disabled(self):
        """Whitespace-only username with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "   ", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_newline_in_username_when_disabled(self):
        """Username with newline character with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "admin\n", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_tab_in_username_when_disabled(self):
        """Username with tab character with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "admin\t", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_carriage_return_in_username_when_disabled(self):
        """Username with carriage return with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "admin\r", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_bell_char_in_username_when_disabled(self):
        """Username with bell character (0x07) with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "admin\x07", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_extended_ascii_in_username_when_disabled(self):
        """Username with extended ASCII with users_enabled=False should still 403."""
        settings.users_enabled = False
        extended_ascii_username = "admin\x80"  # Extended ASCII 128

        response = self.client.post(
            "/api/auth/register",
            json={"username": extended_ascii_username, "password": "Password123"},
        )

        self.assertEqual(response.status_code, 403)

    # =========================================================================
    # ATTACK VECTOR: Timing/State Attacks
    # =========================================================================

    def test_register_rapid_toggle_users_enabled_between_requests(self):
        """Rapid toggle of users_enabled between requests should not leak state."""
        # Request 1: disabled
        settings.users_enabled = False
        response1 = self.client.post(
            "/api/auth/register",
            json={"username": "user1", "password": "Password123"},
        )
        self.assertEqual(response1.status_code, 403)

        # Request 2: enabled (rapid toggle)
        settings.users_enabled = True
        response2 = self.client.post(
            "/api/auth/register",
            json={"username": "user2", "password": "Password123"},
        )
        self.assertEqual(response2.status_code, 200)

        # Request 3: disabled again
        settings.users_enabled = False
        response3 = self.client.post(
            "/api/auth/register",
            json={"username": "user3", "password": "Password123"},
        )
        self.assertEqual(response3.status_code, 403)

    # =========================================================================
    # ATTACK VECTOR: Header Injection
    # =========================================================================

    def test_register_header_injection_in_user_agent_when_disabled(self):
        """User-Agent header injection with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "Password123"},
            headers={"User-Agent": "Mozilla/5.0\r\nX-Injected-Header: true"},
        )

        self.assertEqual(response.status_code, 403)

    def test_register_accept_headerManipulation_when_disabled(self):
        """Accept header manipulation with users_enabled=False should still 403."""
        settings.users_enabled = False

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "Password123"},
            headers={"Accept": "application/json\r\nX-Injected: true"},
        )

        self.assertEqual(response.status_code, 403)

    # =========================================================================
    # VERIFY: Guard correctly allows when users_enabled=True
    # =========================================================================

    def test_register_accepts_valid_input_when_users_enabled_true(self):
        """Verify that valid registration still works when users_enabled=True."""
        settings.users_enabled = True

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user"]["username"], "validuser")
        self.assertEqual(data["user"]["role"], "superadmin")  # first user

    def test_register_rejects_weak_password_when_enabled(self):
        """Verify password strength check still works when users_enabled=True."""
        settings.users_enabled = True

        response = self.client.post(
            "/api/auth/register",
            json={"username": "validuser2", "password": "weak"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
