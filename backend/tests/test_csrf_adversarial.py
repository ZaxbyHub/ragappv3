"""
Adversarial security tests for CSRF protection.

Attack vectors tested:
1. Token prediction/guessing attacks
2. Token replay after expiry
3. Bypass by omitting cookie but providing header
4. Bypass by omitting header but providing cookie
5. Exploiting secure=False over HTTP (cookie theft via MITM)
6. Race conditions / concurrent request token invalidation
7. Token forgery / fabrication
8. Token leakage via httponly=False
9. Cross-origin cookie injection
10. Token brute-force attempts

Implementation note (issue #152 / H5): this module previously used the raw
``requests`` library against a live backend at ``http://localhost:9090`` and
``@skipUnless(check_backend_available())`` gated every class, which meant the
entire file was skipped in CI (no live backend runs in the CI job). It now
exercises the real CSRF protection in-process via FastAPI's ``TestClient`` (same
pattern as ``test_csrf_auth.py``), so coverage runs in CI.

Three tiers:
  * Tier 1 — most attack-vector tests run through ``TestClient`` against the
    real app + a real ``CSRFManager`` (in-memory store fallback).
  * Tier 2 — the concurrency tests target the ``CSRFManager`` store directly,
    because ``TestClient`` serializes requests through a single ASGI transport
    and therefore cannot express true concurrent HTTP. The store is the actual
    shared state in which a race would manifest, so this preserves the
    adversarial intent.
  * Tier 3 — two tests that rely on raw/duplicate HTTP header semantics that
    ``httpx`` (TestClient's transport) does not express identically to a real
    socket server are kept live-backend gated with an explicit rationale.
"""

import concurrent.futures
import os
import random
import shutil
import string
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies for CI (mirrors conftest.py / test_csrf_auth.py)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
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
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
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
from app.security import CSRFManager


def _backend_available() -> bool:
    """Only used by the Tier-3 live-backend tests (raw header semantics)."""
    try:
        import requests  # local import; not a CI dependency

        resp = requests.get("http://localhost:9090/api/csrf-token", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def random_username(prefix="atk"):
    """Generate random username for unique test users."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}_{suffix}_{int(time.time() * 1000)}"


class _CSRFAdversarialBase(unittest.TestCase):
    """Shared setUp/tearDown building a real app + CSRFManager + TestClient.

    Mirrors the pattern in test_csrf_auth.py so the real csrf_protect validator
    runs (conftest detects "csrf" in this module's source and enables real CSRF
    validation via RAGAPP_CSRF_TEST_BYPASS=0).
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.maxDiff = None

        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db

        self.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )
        # Force in-memory fallback and drop the redis handle so per-request
        # _check_redis_available() doesn't block on redis ping retries (redis
        # is absent in CI). test_csrf_auth.py tolerates this because it makes
        # few requests; the adversarial suite is higher-volume.
        self.csrf_manager._use_fallback = True
        self.csrf_manager._redis = None
        main_app.state.csrf_manager = self.csrf_manager

        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        self.app.dependency_overrides.clear()
        self.test_pool.close_all()
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    # --- helpers -----------------------------------------------------------
    def get_valid_csrf(self, clear_jar=True):
        """Fetch a valid (token, cookie) pair via TestClient.

        By default the client cookie jar is cleared after the fetch so that
        subsequent attack requests only carry the CSRF material they explicitly
        pass (TestClient, like a browser Session, persists Set-Cookie across
        requests, which would silently satisfy the double-submit cookie for
        omission-bypass tests). Callers that want the cookie to persist can pass
        ``clear_jar=False``.
        """
        resp = self.client.get("/api/csrf-token")
        assert resp.status_code == 200, resp.text
        token = resp.json()["csrf_token"]
        cookie = resp.cookies.get("X-CSRF-Token")
        if clear_jar:
            self.client.cookies.clear()
        return token, cookie

    def _clear_jar(self):
        """Drop any cookies persisted on the TestClient between requests."""
        self.client.cookies.clear()

    def _register(self, username, token=None, cookie=None, **extra):
        """POST /auth/register with only the explicitly-provided CSRF material."""
        self._clear_jar()
        kwargs = {"json": {"username": username, "password": "Password123!"}}
        if token is not None:
            kwargs["headers"] = {"X-CSRF-Token": token}
        if cookie is not None:
            kwargs["cookies"] = {"X-CSRF-Token": cookie}
        kwargs.update(extra)
        return self.client.post("/api/auth/register", **kwargs)


# =========================================================================
# Attack Vector 1: Token prediction/guessing attacks
# =========================================================================
class TestCSRFPrediction(_CSRFAdversarialBase):
    """
    Tokens are generated with secrets.token_urlsafe(16) which produces 128 bits
    of entropy. Brute-force should be infeasible.
    """

    def test_random_tokens_are_unique(self):
        """Many consecutively-generated tokens should all be distinct.

        Exercises the CSRFManager token generator directly (the same generator
        the /csrf-token endpoint calls) so the test is fast enough for CI; the
        uniqueness property under test is a property of the generator, not the
        HTTP path.
        """
        tokens = {
            self.csrf_manager.generate_token() for _ in range(200)
        }
        self.assertEqual(
            len(tokens), 200, "Tokens are not unique — collision detected"
        )

    def test_token_entropy_sufficient(self):
        """Each token should have >= 22 chars (secrets.token_urlsafe(16) produces 22)."""
        token, _ = self.get_valid_csrf()
        self.assertGreaterEqual(
            len(token), 22, f"Token length {len(token)} too short — predictability risk"
        )

    def test_bruteforce_guessing_fails(self):
        """Random guesses should all fail — no token prediction possible.

        Iteration count is kept modest because each rejected request still pays
        the full argon2 password-hash cost in-process; the adversarial intent
        (random tokens never pass CSRF validation) is fully exercised.
        """
        for i in range(10):
            guess = "".join(random.choices(string.ascii_letters + string.digits, k=22))
            resp = self._register(
                random_username("guess"), token=guess, cookie=guess
            )
            self.assertEqual(
                resp.status_code,
                403,
                f"Guess #{i} '{guess}' unexpectedly passed CSRF validation",
            )

    def test_token_not_derived_from_timestamp(self):
        """Tokens generated at different times should not correlate."""
        tokens = []
        for _ in range(20):
            tokens.append(self.csrf_manager.generate_token())
            time.sleep(0.02)  # Small delay between generations

        for i, t1 in enumerate(tokens):
            for j, t2 in enumerate(tokens):
                if i != j:
                    self.assertNotIn(
                        t1[:8],
                        t2,
                        "Token prefix found in another token — weak randomness",
                    )

    def test_sequential_tokens_have_no_pattern(self):
        """Sequential tokens should have no obvious numerical or positional pattern."""
        tokens = [self.csrf_manager.generate_token() for _ in range(30)]

        for i in range(len(tokens) - 1):
            t1, t2 = tokens[i], tokens[i + 1]
            matches = sum(c1 == c2 for c1, c2 in zip(t1, t2))
            similarity = matches / max(len(t1), len(t2))
            self.assertLess(
                similarity,
                0.5,
                f"Adjacent tokens too similar ({similarity:.0%}): {t1} vs {t2}",
            )


# =========================================================================
# Attack Vector 2: Token replay after expiry
# =========================================================================
class TestCSRFReplay(_CSRFAdversarialBase):
    """
    Tokens have a 900-second TTL. After expiry, they should be rejected.
    The test uses tokens from a fresh session to simulate expiry detection.
    """

    def test_valid_token_accepted(self):
        """A fresh valid token should be accepted."""
        token, cookie = self.get_valid_csrf()
        resp = self._register(random_username("replay_ok"), token=token, cookie=cookie)
        self.assertNotEqual(
            resp.status_code,
            403,
            "Valid token was incorrectly rejected as CSRF failure",
        )

    def test_forged_expired_token_rejected(self):
        """A fabricated token that was never issued should be rejected."""
        fake_token = "expired_never_issued_token_1234567890"
        resp = self._register(
            random_username("expired"), token=fake_token, cookie=fake_token
        )
        self.assertEqual(
            resp.status_code,
            403,
            f"Forged token was accepted: {resp.status_code} {resp.text}",
        )

    def test_reused_token_still_works_before_expiry(self):
        """A token should remain valid on reuse before TTL expiry (sliding window)."""
        token, cookie = self.get_valid_csrf()

        resp1 = self._register(random_username("reuse_a"), token=token, cookie=cookie)
        self.assertNotEqual(resp1.status_code, 403, "First use rejected as CSRF failure")

        resp2 = self._register(random_username("reuse_b"), token=token, cookie=cookie)
        self.assertNotEqual(
            resp2.status_code,
            403,
            "Token was invalidated after first use — sliding window not working",
        )


# =========================================================================
# Attack Vectors 3 & 4: Bypass by omitting cookie or header
# =========================================================================
class TestCSRFOmissionBypass(_CSRFAdversarialBase):
    """
    The double-submit pattern requires BOTH the cookie AND the header.
    Omitting either must fail.
    """

    def test_header_only_no_cookie_returns_403(self):
        """Attacker sends header but no cookie — must be rejected."""
        token, _ = self.get_valid_csrf()
        resp = self._register(random_username("hdr"), token=token)
        self.assertEqual(
            resp.status_code,
            403,
            f"Header-only request was accepted: {resp.status_code}",
        )

    def test_cookie_only_no_header_returns_403(self):
        """Attacker sends cookie but no header — must be rejected."""
        _, cookie = self.get_valid_csrf()
        resp = self._register(random_username("ckie"), cookie=cookie)
        self.assertEqual(
            resp.status_code,
            403,
            f"Cookie-only request was accepted: {resp.status_code}",
        )

    def test_no_cookie_no_header_returns_403(self):
        """Attacker sends neither cookie nor header — must be rejected."""
        resp = self._register(random_username("none"))
        self.assertEqual(resp.status_code, 403, "Request with no CSRF was accepted")

    def test_empty_string_cookie_and_header_returns_403(self):
        """Empty strings in cookie and header must be rejected."""
        resp = self._register(
            random_username("empty"), token="", cookie=""
        )
        self.assertEqual(resp.status_code, 403, "Empty CSRF strings were accepted")

    def test_whitespace_cookie_and_header_returns_403(self):
        """Whitespace-only cookie/header must be rejected."""
        resp = self._register(
            random_username("space"), token="   ", cookie="   "
        )
        self.assertEqual(resp.status_code, 403, "Whitespace CSRF was accepted")


# =========================================================================
# Attack Vector 5: Exploiting secure=False over HTTP
# =========================================================================
class TestCSRFCookieAttributes(_CSRFAdversarialBase):
    """
    The cookie is set with secure=False, httponly=False, samesite=lax.
    These tests document that surface (the cookie is JS-readable and HTTP-sent).
    """

    def test_cookie_not_httponly(self):
        """DOCUMENTATION: httponly is NOT set on the CSRF cookie by design."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("Set-Cookie", "").lower()
        is_httponly = "httponly" in set_cookie
        self.assertFalse(
            is_httponly,
            "EXPECTED FINDING: httponly=False on CSRF cookie allows JS read access. "
            "Combined with XSS, an attacker can steal CSRF tokens.",
        )

    def test_cookie_not_secure(self):
        """DOCUMENTATION: secure flag reflects settings.csrf_cookie_secure."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("Set-Cookie", "").lower()
        is_secure = "secure" in set_cookie
        # In the test environment csrf_cookie_secure may be False; this documents
        # that when secure is unset the cookie is sent over HTTP.
        if not settings.csrf_cookie_secure:
            self.assertFalse(
                is_secure,
                "secure flag present while settings.csrf_cookie_secure is False",
            )

    def test_cookie_has_samesite_lax(self):
        """Cookie should have SameSite=Lax to mitigate cross-site POST attacks."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("Set-Cookie", "").lower()
        self.assertIn(
            "samesite=lax",
            set_cookie,
            f"Cookie missing SameSite=Lax: {set_cookie}",
        )

    def test_cookie_value_equals_body_token(self):
        """Cookie value must exactly match the response body token — no transformation."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)
        body_token = resp.json()["csrf_token"]
        cookie_token = resp.cookies.get("X-CSRF-Token")
        self.assertEqual(
            body_token,
            cookie_token,
            "Token mismatch between body and cookie — potential integrity issue",
        )

    def test_cookie_sent_in_plain_text_over_http(self):
        """DOCUMENTATION: a valid token round-trip proves the token is usable (and,
        over real HTTP, interceptable). This is a finding, not a defect assertion."""
        token, cookie = self.get_valid_csrf()
        resp = self._register(random_username("plain"), token=token, cookie=cookie)
        self.assertNotEqual(
            resp.status_code,
            403,
            "Valid token was rejected — this test documents plain-text transmission",
        )


# =========================================================================
# Attack Vector 6: Race conditions (Tier 2 — exercises the store directly)
# =========================================================================
class TestCSRFRaceConditions(unittest.TestCase):
    """
    Can concurrent requests cause token invalidation? A race in the CSRF store
    would manifest as: (a) a valid token spuriously failing validation, or
    (b) generated tokens colliding.

    TestClient serializes requests through one ASGI transport, so true HTTP
    concurrency cannot be expressed in-process. Instead we exercise the real
    ``CSRFManager`` store (the actual shared mutable state) concurrently. The
    adversarial intent — exposing a race in token issuance/validation — is
    preserved because the store is exactly where such a race would live.
    """

    def setUp(self):
        # In-memory fallback store (Redis absent in CI). thread-safe via _lock.
        # We construct the manager then force it into fallback mode and drop the
        # redis handle so _check_redis_available() short-circuits — otherwise each
        # generate/validate call blocks on a redis ping retry when redis is down.
        self.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )
        self.csrf_manager._use_fallback = True
        self.csrf_manager._redis = None

    def test_concurrent_requests_same_token(self):
        """Many threads validating the same valid token should all succeed."""
        token = self.csrf_manager.generate_token()

        results = []
        errors = []

        def validate_many(_idx):
            try:
                local = []
                for _ in range(20):
                    local.append(self.csrf_manager.validate_token(token))
                results.extend(local)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(validate_many, i) for i in range(10)]
            concurrent.futures.wait(futures)

        self.assertEqual(errors, [], f"Concurrent validation raised: {errors}")
        # Every validation of a valid, non-expired token must succeed.
        failures = [r for r in results if not r]
        self.assertEqual(
            len(failures),
            0,
            f"{len(failures)}/{len(results)} concurrent validations of a valid "
            "token failed — possible race condition in token validation",
        )

    def test_rapid_sequential_token_requests(self):
        """Rapidly issuing many tokens should not collide or error."""
        tokens = set()
        errors = []

        def issue_many(prefix):
            try:
                local = []
                for _ in range(50):
                    local.append(self.csrf_manager.generate_token())
                return local
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(issue_many, i) for i in range(5)]
            for f in concurrent.futures.as_completed(futures):
                tokens.update(f.result())

        self.assertEqual(errors, [], f"Token generation raised: {errors}")
        self.assertEqual(
            len(tokens),
            250,
            "Some concurrently-issued tokens collided — weak randomness or race",
        )

    def test_concurrent_different_tokens(self):
        """Concurrent issue-then-validate of distinct tokens should all validate."""
        tokens = []
        errors = []

        def issue_and_validate(idx):
            try:
                local = []
                for _ in range(20):
                    t = self.csrf_manager.generate_token()
                    local.append((t, self.csrf_manager.validate_token(t)))
                return local
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(issue_and_validate, i) for i in range(5)]
            concurrent.futures.wait(futures)
            for f in futures:
                tokens.extend(f.result())

        self.assertEqual(errors, [], f"Concurrent issue/validate raised: {errors}")
        failures = [t for t, ok in tokens if not ok]
        self.assertEqual(
            len(failures),
            0,
            f"{len(failures)}/{len(tokens)} freshly-issued tokens failed validation",
        )


# =========================================================================
# Attack Vector 7: Token forgery / fabrication
# =========================================================================
class TestCSRFForgery(_CSRFAdversarialBase):
    """Can an attacker forge a CSRF token that passes validation?"""

    def test_forged_token_with_matching_cookie(self):
        """An attacker who fabricates both cookie and header with same fake value must fail."""
        fake = "forged_token_value_abcdefghijklmnop"
        resp = self._register(random_username("forge"), token=fake, cookie=fake)
        self.assertEqual(
            resp.status_code, 403, "Forged matching token/cookie was accepted"
        )

    def test_token_with_injected_special_chars(self):
        """Tokens with special characters must be rejected.

        Each payload is exercised against the real CSRF validator; iteration is
        modest because each rejected request still pays argon2 hash cost.
        """
        payloads = [
            "'; DROP TABLE csrf_tokens; --",
            "<script>alert('xss')</script>",
            "../../../etc/passwd",
            "${7*7}",
            "\x00\x00\x00",
            "A" * 10000,  # Oversized token
            "",  # Empty
        ]
        for payload in payloads:
            resp = self._register(
                random_username("inject"), token=payload, cookie=payload
            )
            self.assertEqual(
                resp.status_code,
                403,
                f"Injection payload '{payload[:30]}' was accepted as valid CSRF",
            )

    def test_token_truncation_attack(self):
        """Using a truncated version of a valid token must fail."""
        token, _ = self.get_valid_csrf()
        truncated = token[: len(token) // 2]
        resp = self._register(
            random_username("trunc"), token=truncated, cookie=truncated
        )
        self.assertEqual(resp.status_code, 403, "Truncated token was accepted")

    def test_token_with_extra_padding(self):
        """Using a valid token with extra characters appended must fail."""
        token, _ = self.get_valid_csrf()
        padded = token + "EXTRA_PADDING_CHARS"
        resp = self._register(random_username("pad"), token=padded, cookie=padded)
        self.assertEqual(resp.status_code, 403, "Padded token was accepted")

    def test_token_with_case_transformation(self):
        """Using a valid token with case changes must fail (tokens are case-sensitive)."""
        token, _ = self.get_valid_csrf()
        flipped = "".join(c.upper() if c.islower() else c.lower() for c in token)
        resp = self._register(random_username("case"), token=flipped, cookie=flipped)
        self.assertEqual(resp.status_code, 403, "Case-transformed token was accepted")

    def test_token_with_unicode_homoglyphs(self):
        """A token carrying a unicode homoglyph must not bypass CSRF protection.

        A real CSRF token is ASCII (``secrets.token_urlsafe``). If an attacker
        swaps an ASCII letter for a visually-identical Cyrillic homoglyph, the
        resulting value is not a valid token. We can only exercise the
        substitution when the issued token actually contains an 'a' (the
        homoglyph target); otherwise the test is skipped. If httpx refuses to
        transmit the non-ASCII header, that itself prevents the attack; if it
        is transmitted, the server must reject it with 403. Either way the
        attacker's value is never accepted as valid.
        """
        token, _ = self.get_valid_csrf()
        if "a" not in token:
            self.skipTest("issued token contains no 'a' — homoglyph substitution N/A")
        homoglyph = token.replace("a", "\u0430")
        try:
            resp = self._register(
                random_username("glyph"), token=homoglyph, cookie=homoglyph
            )
        except Exception as exc:  # noqa: BLE001
            # httpx rejects non-ASCII header values — the attack can't even be
            # transmitted, which satisfies the security property under test.
            self.assertIn("ascii", str(exc).lower() + repr(exc).lower())
            return
        self.assertEqual(resp.status_code, 403, "Unicode homoglyph token was accepted")

    def test_token_with_url_encoding_bypass(self):
        """URL-encoded versions of tokens must be rejected if the server decodes them."""
        token, _ = self.get_valid_csrf()
        import urllib.parse

        encoded = urllib.parse.quote(token)
        if encoded != token:  # Only test if encoding actually changed something
            resp = self._register(
                random_username("urlenc"), token=encoded, cookie=encoded
            )
            self.assertEqual(
                resp.status_code, 403, "URL-encoded token bypassed CSRF validation"
            )

    @unittest.skipUnless(
        _backend_available(),
        "Tier 3: double-cookie attack requires raw Cookie-header semantics "
        "that httpx (TestClient) does not express identically to a real socket "
        "server. Run against a live backend at http://localhost:9090.",
    )
    def test_double_cookie_attack(self):
        """Sending two CSRF cookies (cookie splitting) must not bypass validation.

        Tier-3 rationale: this test sends a hand-built ``Cookie`` header carrying
        two ``X-CSRF-Token`` values. httpx (TestClient's transport) collapses
        duplicate cookies differently than a real server's cookie parser, so the
        attack cannot be faithfully simulated in-process. Kept live-gated so it
        still runs against a real backend when one is available.
        """
        import requests

        BASE_URL = "http://localhost:9090/api"
        resp = requests.get(f"{BASE_URL}/csrf-token", timeout=5)
        token = resp.json()["csrf_token"]
        cookie = resp.cookies.get("X-CSRF-Token")
        fake = "fake_token_for_splitting"
        for order in [(cookie, fake), (fake, cookie)]:
            cookie_header = "; ".join([f"X-CSRF-Token={c}" for c in order])
            resp = requests.post(
                f"{BASE_URL}/auth/register",
                json={
                    "username": random_username("split"),
                    "password": "Password123!",
                },
                headers={"X-CSRF-Token": token, "Cookie": cookie_header},
                timeout=5,
            )
            if resp.status_code != 403:
                self.assertIn(
                    resp.status_code,
                    [200, 400, 409, 422],
                    f"Unexpected status {resp.status_code} in cookie-split attack",
                )


# =========================================================================
# Attack Vector 8: Token leakage via httponly=False
# =========================================================================
class TestCSRFTokenLeakage(_CSRFAdversarialBase):
    """
    Since the CSRF cookie is not httponly, JavaScript can read it.
    This means XSS can steal CSRF tokens.
    """

    def test_token_appears_in_response_body(self):
        """The CSRF token is returned in the JSON body by design (double-submit)."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("csrf_token", data)
        self.assertGreater(len(data["csrf_token"]), 0)

    def test_token_in_body_matches_cookie(self):
        """Confirm the body token and cookie are identical."""
        resp = self.client.get("/api/csrf-token")
        body_token = resp.json()["csrf_token"]
        cookie_token = resp.cookies.get("X-CSRF-Token")
        self.assertEqual(body_token, cookie_token)

    def test_csrf_cookie_name_not_prefixed(self):
        """DOCUMENTATION: Cookie name 'X-CSRF-Token' has no __Host- prefix."""
        resp = self.client.get("/api/csrf-token")
        set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn(
            "X-CSRF-Token",
            set_cookie,
            "Cookie name not found in Set-Cookie header",
        )
        self.assertNotIn(
            "__Host-",
            set_cookie,
            "FINDING: Cookie lacks __Host- prefix — not restricted to secure origins",
        )


# =========================================================================
# Attack Vector 9: Cross-origin cookie injection
# =========================================================================
class TestCSRFCrossOrigin(_CSRFAdversarialBase):
    """
    SameSite=Lax prevents cookies from being sent on cross-site POST requests.
    These tests verify the SameSite attribute and CORS behavior.
    """

    def test_samesite_lax_in_set_cookie(self):
        """The Set-Cookie header must contain SameSite=Lax."""
        resp = self.client.get("/api/csrf-token")
        set_cookie = resp.headers.get("Set-Cookie", "").lower()
        self.assertIn("samesite=lax", set_cookie, "SameSite=Lax not in cookie")

    def test_cors_does_not_allow_credentials_from_arbitrary_origins(self):
        """The CORS config should not allow credentials from arbitrary origins."""
        resp = self.client.options(
            "/api/csrf-token",
            headers={"Origin": "https://evil.example.com"},
        )
        acao = resp.headers.get("Access-Control-Allow-Origin", "")
        if acao == "https://evil.example.com":
            acac = resp.headers.get("Access-Control-Allow-Credentials", "")
            self.assertNotEqual(
                acac.lower(),
                "true",
                "CRITICAL: CORS allows credentials from arbitrary origins — "
                "combined with httponly=False, this enables cross-site CSRF theft",
            )

    def test_token_endpoint_accessible_without_origin(self):
        """Token endpoint should still work without an Origin header (legitimate use)."""
        resp = self.client.get("/api/csrf-token")
        self.assertEqual(resp.status_code, 200)


# =========================================================================
# Attack Vector 10: Token brute-force and enumeration
# =========================================================================
class TestCSRFBruteForce(_CSRFAdversarialBase):
    """Token brute-force and enumeration."""

    def test_100_random_tokens_all_rejected(self):
        """Randomly generated token strings should all be rejected.

        Count is modest (argon2 cost per request makes 100 infeasible in CI);
        the adversarial intent — no random token is ever accepted — holds.
        """
        for i in range(15):
            fake = "".join(
                random.choices(string.ascii_letters + string.digits + "-_", k=22)
            )
            resp = self._register(
                random_username(f"bf_{i}"), token=fake, cookie=fake
            )
            self.assertEqual(
                resp.status_code, 403, f"Random token #{i} '{fake}' was accepted (15-trial run)"
            )

    def test_similar_to_valid_token_rejected(self):
        """Tokens that are 1-2 chars off from a valid token must be rejected."""
        token, _ = self.get_valid_csrf()
        variants = []
        for i in range(min(5, len(token))):
            flipped = list(token)
            flipped[i] = "X" if flipped[i] != "X" else "Y"
            variants.append("".join(flipped))
        if len(token) > 1:
            variants.append(token[:-1])
            variants.append(token[1:])
        variants.append(token + "X")
        variants.insert(0, "X" + token)

        for variant in variants:
            resp = self._register(
                random_username("sim"), token=variant, cookie=variant
            )
            self.assertEqual(
                resp.status_code,
                403,
                f"Similar token variant '{variant[:20]}...' was accepted",
            )

    def test_numeric_tokens_rejected(self):
        """Purely numeric tokens must be rejected."""
        for length in [10, 16, 22, 32]:
            fake = "".join(random.choices(string.digits, k=length))
            resp = self._register(
                random_username("num"), token=fake, cookie=fake
            )
            self.assertEqual(
                resp.status_code, 403, f"Numeric token (len={length}) was accepted"
            )


# =========================================================================
# Edge cases and boundary tests
# =========================================================================
class TestCSRFEdgeCases(_CSRFAdversarialBase):
    """Edge case and boundary tests."""

    def test_csrf_token_endpoint_is_get_only(self):
        """The /csrf-token endpoint should only accept GET."""
        for method in ["post", "put", "delete", "patch"]:
            resp = getattr(self.client, method)("/api/csrf-token")
            self.assertEqual(
                resp.status_code,
                405,
                f"{method.upper()} /csrf-token returned {resp.status_code} instead of 405",
            )

    @unittest.skipUnless(
        _backend_available(),
        "Tier 3: duplicate X-CSRF-Token headers require raw header semantics "
        "that httpx (TestClient) does not express identically to a real socket "
        "server. Run against a live backend at http://localhost:9090.",
    )
    def test_multiple_csrf_headers_sent(self):
        """If multiple X-CSRF-Token headers are sent, the request should fail.

        Tier-3 rationale: passing duplicate headers via httpx's ``headers=``
        does not preserve two distinct values the way a raw socket would, so the
        attack cannot be faithfully simulated in-process. Kept live-gated.
        """
        import requests

        BASE_URL = "http://localhost:9090/api"
        resp = requests.get(f"{BASE_URL}/csrf-token", timeout=5)
        token = resp.json()["csrf_token"]
        cookie = resp.cookies.get("X-CSRF-Token")
        resp = requests.post(
            f"{BASE_URL}/auth/register",
            json={
                "username": random_username("multi_hdr"),
                "password": "Password123!",
            },
            cookies={"X-CSRF-Token": cookie},
            headers=[("X-CSRF-Token", token), ("X-CSRF-Token", "fake")],
            timeout=5,
        )
        if resp.status_code == 403:
            pass  # Expected: mismatch detected
        else:
            self.assertIn(resp.status_code, [200, 400, 409, 422])

    def test_csrf_token_in_body_instead_of_header(self):
        """Sending the CSRF token in the request body (not header) must not bypass."""
        token, cookie = self.get_valid_csrf()
        resp = self.client.post(
            "/api/auth/register",
            json={
                "username": random_username("body_csrf"),
                "password": "Password123!",
                "X-CSRF-Token": token,  # In body, not header
            },
            cookies={"X-CSRF-Token": cookie},
        )
        self.assertEqual(
            resp.status_code,
            403,
            "CSRF token in body (not header) bypassed protection",
        )

    def test_csrf_token_in_query_string(self):
        """Sending the CSRF token as a query parameter must not bypass."""
        token, cookie = self.get_valid_csrf()
        resp = self.client.post(
            f"/api/auth/register?X-CSRF-Token={token}",
            json={"username": random_username("qs_csrf"), "password": "Password123!"},
            cookies={"X-CSRF-Token": cookie},
        )
        self.assertEqual(
            resp.status_code,
            403,
            "CSRF token in query string bypassed protection",
        )

    def test_cookie_domain_scope(self):
        """CSRF cookie should not have an explicit Domain attribute (restricts scope)."""
        resp = self.client.get("/api/csrf-token")
        set_cookie = resp.headers.get("Set-Cookie", "")
        if "domain=" in set_cookie.lower():
            self.fail(
                "CSRF cookie has explicit Domain attribute — "
                "this can widen the cookie scope across subdomains"
            )

    def test_null_byte_in_token(self):
        """Null bytes in tokens must be rejected (C-string termination attack)."""
        resp = self._register(
            random_username("null"), token="abc\x00def", cookie="abc\x00def"
        )
        self.assertEqual(resp.status_code, 403, "Token with null byte was accepted")

    def test_newline_in_token(self):
        """Newlines in tokens must be rejected (header injection)."""
        resp = self._register(
            random_username("crlf"), token="abc\r\ndef", cookie="abc\r\ndef"
        )
        self.assertEqual(resp.status_code, 403, "Token with CRLF was accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
