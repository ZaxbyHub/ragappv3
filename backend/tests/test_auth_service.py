"""Tests for auth_service.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Test constants
TEST_SECRET_KEY = "test-secret-key-for-testing-32bytes"
TEST_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15


@pytest.fixture(autouse=True)
def mock_settings():
    """Mock settings for all tests."""
    mock = MagicMock()
    # Configure the MagicMock to return actual values for attribute access
    mock.users_enabled = True
    mock.jwt_secret_key = TEST_SECRET_KEY
    mock.jwt_algorithm = TEST_ALGORITHM
    # auth_service imports settings inside functions via `from app.config import settings`
    with patch("app.config.settings", mock, create=True):
        yield mock


class TestPasswordHashing:
    """Tests for hash_password and verify_password."""

    def test_hash_and_verify_password(self):
        """Hash a password, verify it matches, verify wrong password returns False."""
        from app.services.auth_service import hash_password, verify_password

        password = "mySecurePassword123"
        hashed = hash_password(password)

        # Correct password should verify
        assert verify_password(password, hashed) is True
        # Wrong password should not verify
        assert verify_password("wrongPassword", hashed) is False

    def test_hash_password_different_each_time(self):
        """Verify hashing same password twice produces different hashes."""
        from app.services.auth_service import hash_password

        password = "samePassword"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        # Hashes should be different (salt + argon2id iteration)
        assert hash1 != hash2
        # But both should still verify
        assert hash1 != password
        assert hash2 != password

    def test_hash_password_produces_argon2id_by_default(self):
        """New hashes must use the Argon2id scheme."""
        from app.services.auth_service import hash_password

        hashed = hash_password("anyPassword123!")
        assert hashed.startswith("$argon2id$"), (
            f"Expected $argon2id$ prefix, got: {hashed[:10]}"
        )

    def test_verify_password_accepts_legacy_bcrypt_hash(self):
        """Existing bcrypt hashes must still verify correctly (transparent upgrade path)."""
        import bcrypt

        from app.services.auth_service import verify_password

        # Manually create a legacy bcrypt hash (cost 12, matching old deprecated scheme)
        legacy_hash = bcrypt.hashpw(b"legacyPassword123", bcrypt.gensalt(rounds=12)).decode()
        assert legacy_hash.startswith("$2b$")

        # verify_password must still accept it
        assert verify_password("legacyPassword123", legacy_hash) is True
        assert verify_password("wrongPassword", legacy_hash) is False

    def test_password_needs_rehash_true_for_bcrypt(self):
        """password_needs_rehash returns True for legacy bcrypt hashes."""
        import bcrypt

        from app.services.auth_service import password_needs_rehash

        legacy_hash = bcrypt.hashpw(b"testPassword", bcrypt.gensalt(rounds=12)).decode()
        assert password_needs_rehash(legacy_hash) is True

    def test_password_needs_rehash_false_for_argon2id(self):
        """password_needs_rehash returns False for current scheme (argon2id)."""
        from app.services.auth_service import hash_password, password_needs_rehash

        current_hash = hash_password("anyPassword")
        assert password_needs_rehash(current_hash) is False

    def test_password_needs_rehash_true_for_legacy_bcrypt_cost_14(self):
        """password_needs_rehash returns True for legacy bcrypt cost-14 hashes."""
        import bcrypt

        from app.services.auth_service import password_needs_rehash

        # bcrypt cost 14 (the old scheme)
        legacy_hash = bcrypt.hashpw(b"testPassword", bcrypt.gensalt(rounds=14)).decode()
        assert legacy_hash.startswith("$2b$")
        assert password_needs_rehash(legacy_hash) is True

    def test_password_needs_rehash_false_for_empty_string(self):
        """password_needs_rehash returns False for empty string (does not crash)."""
        from app.services.auth_service import password_needs_rehash

        # Must not raise — should return False or handle gracefully
        result = password_needs_rehash("")
        assert result is False

    def test_password_needs_rehash_false_for_malformed_hash(self):
        """password_needs_rehash returns False for unrecognizable hash strings.

        Note: hashes with a recognized prefix (e.g. $2b$) but invalid content
        still return True because they are identified as the deprecated scheme
        and should be upgraded. Only totally unrecognizable strings return False.
        """
        from app.services.auth_service import password_needs_rehash

        # Unrecognizable strings — must not raise, must return False
        assert password_needs_rehash("not_a_hash_at_all") is False
        assert password_needs_rehash("") is False
        assert password_needs_rehash("$argon2id$garbage") is False
        assert password_needs_rehash("$argon2id$invalid$structure") is False
        # $2b$ prefix IS recognized as bcrypt (deprecated) → needs_update=True
        assert password_needs_rehash("$2b$garbage") is True


class TestTransparentPasswordUpgrade:
    """Tests for the bcrypt→Argon2id transparent upgrade on successful login."""

    def test_new_hash_is_always_argon2id(self):
        """New hashes must always be Argon2id regardless of input password."""
        from app.services.auth_service import hash_password

        # Hash many different passwords — all must be argon2id
        passwords = [
            "short",
            "a" * 128,  # max length
            "пароль",  # unicode
            "🔐🔑🔒",  # emoji
            "P@$$w0rd!#$%",  # special chars
        ]
        for pw in passwords:
            h = hash_password(pw)
            assert h.startswith("$argon2id$"), (
                f"Expected argon2id for password '{pw[:20]}...', got: {h[:10]}"
            )

    def test_bcrypt_legacy_hash_verifies_but_gets_flagged_for_rehash(self):
        """A bcrypt hash verifies correctly but is flagged for rehash to argon2id."""
        import bcrypt

        from app.services.auth_service import (
            hash_password,
            password_needs_rehash,
            verify_password,
        )

        # Create legacy bcrypt hash
        legacy_password = "myOldBcryptPassword"
        legacy_hash = bcrypt.hashpw(legacy_password.encode(), bcrypt.gensalt(rounds=12)).decode()
        assert legacy_hash.startswith("$2b$")

        # Verify it still works
        assert verify_password(legacy_password, legacy_hash) is True
        assert verify_password("wrong", legacy_hash) is False

        # But it should be flagged for upgrade
        assert password_needs_rehash(legacy_hash) is True

        # New hash should be argon2id
        new_hash = hash_password(legacy_password)
        assert new_hash.startswith("$argon2id$")
        assert password_needs_rehash(new_hash) is False


class TestAccessToken:
    """Tests for create_access_token and decode_access_token."""

    def test_create_access_token_returns_string(self, mock_settings):
        """Create token and verify it returns a JWT string."""
        from app.services.auth_service import create_access_token

        token = create_access_token(42, "testuser", "admin")

        # Should return a string
        assert isinstance(token, str)
        # JWT tokens have 3 parts separated by dots
        assert token.count(".") == 2

    def test_create_access_token_contains_required_claims(self, mock_settings):
        """Create token with integer user_id and verify the JWT payload structure.

        NOTE: This test exposes a bug in the source code. PyJWT 2.x requires
        the 'sub' claim to be a string, but create_access_token passes user_id
        as an integer. This causes decode_access_token to return None.
        """
        import jwt

        from app.services.auth_service import create_access_token, get_jwt_config

        user_id = 42
        username = "testuser"
        role = "admin"

        secret, algorithm = get_jwt_config()
        token = create_access_token(user_id, username, role)

        # Decode to inspect payload
        # NOTE: Due to the sub-as-integer bug, we need to use options={"verify_sub": False}
        # to bypass the subject validation
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
        )

        # Verify payload contains required claims
        assert "sub" in payload
        assert payload["sub"] == str(user_id)  # sub must be string per RFC 7519
        assert payload["username"] == username
        assert payload["role"] == role
        assert "exp" in payload

    def test_create_access_token_expiry(self, mock_settings):
        """Create token, verify exp is ~15 minutes from now."""
        import jwt

        from app.services.auth_service import create_access_token, get_jwt_config

        before_creation = datetime.now(timezone.utc)

        secret, algorithm = get_jwt_config()
        token = create_access_token(user_id=1, username="user", role="member")

        # Decode with bypass for sub type validation
        payload = jwt.decode(
            token, secret, algorithms=[algorithm], options={"verify_sub": False}
        )

        after_creation = datetime.now(timezone.utc)
        assert payload is not None

        # Convert exp to datetime
        exp_timestamp = payload["exp"]
        exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)

        # Expiry should be approximately 15 minutes from now
        expected_min = before_creation + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES - 1
        )
        expected_max = after_creation + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES + 1
        )

        assert expected_min <= exp_datetime <= expected_max

    def test_decode_access_token_invalid(self, mock_settings):
        """Decode garbage string raises TokenInvalidError."""
        import pytest

        from app.services.auth_service import TokenInvalidError, decode_access_token

        with pytest.raises(TokenInvalidError):
            decode_access_token("not.a.valid.token.at.all")

        # Also test completely garbage input
        with pytest.raises(TokenInvalidError):
            decode_access_token("garbage!!!")

    def test_decode_access_token_expired(self, mock_settings):
        """Create token, verify expired token raises TokenExpiredError."""
        import jwt
        import pytest

        from app.services.auth_service import TokenExpiredError, decode_access_token

        # Create a token that's already expired
        secret, algorithm = TEST_SECRET_KEY, TEST_ALGORITHM
        expired_payload = {
            "sub": 1,
            "username": "user",
            "role": "member",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),  # Already expired
        }
        expired_token = jwt.encode(expired_payload, secret, algorithm=algorithm)

        # Should raise TokenExpiredError for expired token
        with pytest.raises(TokenExpiredError):
            decode_access_token(expired_token)

    def test_create_access_token_contains_jti(self, mock_settings):
        """Create token and verify the JWT payload contains a unique jti claim."""
        import jwt

        from app.services.auth_service import create_access_token, get_jwt_config

        token = create_access_token(42, "testuser", "admin")
        secret, algorithm = get_jwt_config()
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        assert "jti" in payload
        assert isinstance(payload["jti"], str)
        assert len(payload["jti"]) > 0

    def test_create_access_token_jti_is_unique(self, mock_settings):
        """Two tokens must have different jti values."""
        import jwt

        from app.services.auth_service import create_access_token, get_jwt_config

        token1 = create_access_token(1, "user", "member")
        token2 = create_access_token(1, "user", "member")
        secret, algorithm = get_jwt_config()
        payload1 = jwt.decode(token1, secret, algorithms=[algorithm])
        payload2 = jwt.decode(token2, secret, algorithms=[algorithm])

        assert payload1["jti"] != payload2["jti"]

    def test_compute_client_fingerprint_none(self, mock_settings):
        """compute_client_fingerprint(None) returns hash of empty string."""
        from app.services.auth_service import compute_client_fingerprint

        fpt = compute_client_fingerprint(None)
        assert fpt == compute_client_fingerprint("")

    def test_compute_client_fingerprint_empty_string(self, mock_settings):
        """compute_client_fingerprint('') returns SHA-256 hexdigest of ''."""
        import hashlib

        from app.services.auth_service import compute_client_fingerprint

        fpt = compute_client_fingerprint("")
        expected = hashlib.sha256(b"").hexdigest()
        assert fpt == expected
        assert len(fpt) == 64  # SHA-256 hexdigest length

    def test_compute_client_fingerprint_stable(self, mock_settings):
        """Same UA always produces same fingerprint."""
        from app.services.auth_service import compute_client_fingerprint

        ua = "Mozilla/5.0 TestBrowser"
        assert compute_client_fingerprint(ua) == compute_client_fingerprint(ua)

    def test_compute_client_fingerprint_different_ua_different_fingerprint(self, mock_settings):
        """Different UA strings produce different fingerprints."""
        from app.services.auth_service import compute_client_fingerprint

        fpt1 = compute_client_fingerprint("Mozilla/5.0 BrowserA")
        fpt2 = compute_client_fingerprint("Mozilla/5.0 BrowserB")
        assert fpt1 != fpt2

    def test_create_access_token_with_fingerprint(self, mock_settings):
        """create_access_token with client_fingerprint adds fpt claim to payload."""
        import jwt

        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
            get_jwt_config,
        )

        ua = "TestBrowser/1.0"
        token = create_access_token(42, "testuser", "admin", client_fingerprint=compute_client_fingerprint(ua))
        secret, algorithm = get_jwt_config()
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        assert "fpt" in payload
        assert payload["fpt"] == compute_client_fingerprint(ua)

    def test_create_access_token_without_fingerprint(self, mock_settings):
        """create_access_token without client_fingerprint omits fpt claim."""
        import jwt

        from app.services.auth_service import create_access_token, get_jwt_config

        token = create_access_token(42, "testuser", "admin")
        secret, algorithm = get_jwt_config()
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        assert "fpt" not in payload

    def test_create_access_token_fingerprint_matches_ua(self, mock_settings):
        """Token issued with UA 'X' validates against UA 'X' but not 'Y'."""
        import jwt

        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
            get_jwt_config,
        )

        ua = "BrowserXYZ/1.0"
        token = create_access_token(1, "user", "member", client_fingerprint=compute_client_fingerprint(ua))
        secret, algorithm = get_jwt_config()
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        # Same UA → same fingerprint
        assert payload["fpt"] == compute_client_fingerprint(ua)
        # Different UA → different fingerprint
        assert payload["fpt"] != compute_client_fingerprint("DifferentBrowser/2.0")


class TestAccessTokenDenylistService:
    """Tests for deny_access_token, is_access_token_denied, purge_expired_denied_tokens."""

    def test_deny_access_token_then_is_denied(self, mock_settings):
        """deny_access_token makes is_access_token_denied return True for that jti."""
        import sqlite3
        import tempfile

        from app.services.auth_service import (
            deny_access_token,
            is_access_token_denied,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            jti = "test-jti-12345"
            assert is_access_token_denied(conn, jti) is False

            deny_access_token(conn, jti, user_id=1, expires_at="2025-01-01T00:00:00")
            assert is_access_token_denied(conn, jti) is True

            conn.close()
        finally:
            import os
            os.unlink(db_path)

    def test_deny_access_token_idempotent(self, mock_settings):
        """Calling deny_access_token twice with same jti does not error (INSERT OR REPLACE)."""
        import sqlite3
        import tempfile

        from app.services.auth_service import (
            deny_access_token,
            is_access_token_denied,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            jti = "idempotent-jti"
            deny_access_token(conn, jti, user_id=2, expires_at="2025-01-01T00:00:00")
            deny_access_token(conn, jti, user_id=2, expires_at="2025-01-01T00:00:00")  # no-op

            assert is_access_token_denied(conn, jti) is True
            conn.close()
        finally:
            import os
            os.unlink(db_path)

    def test_is_access_token_denied_not_found_returns_false(self, mock_settings):
        """Unknown jti returns False."""
        import sqlite3
        import tempfile

        from app.services.auth_service import is_access_token_denied

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            assert is_access_token_denied(conn, "never-denied-jti") is False
            conn.close()
        finally:
            import os
            os.unlink(db_path)

    def test_purge_expired_denied_tokens_deletes_expired(self, mock_settings):
        """purge_expired_denied_tokens removes entries whose expires_at has passed."""
        import sqlite3
        import tempfile
        from datetime import datetime, timezone

        from app.services.auth_service import (
            deny_access_token,
            is_access_token_denied,
            purge_expired_denied_tokens,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            # Expired entry (2020)
            deny_access_token(conn, "expired-jti", user_id=1, expires_at="2020-01-01T00:00:00")
            # Valid entry (far future)
            future = (datetime.now(timezone.utc).year + 10, 1, 1, 0, 0, 0)
            future_str = f"{future[0]}-{future[1]:02d}-{future[2]:02d}T{future[3]:02d}:{future[4]:02d}:{future[5]:02d}+00:00"
            deny_access_token(conn, "valid-jti", user_id=1, expires_at=future_str)

            assert is_access_token_denied(conn, "expired-jti") is True
            assert is_access_token_denied(conn, "valid-jti") is True

            deleted = purge_expired_denied_tokens(conn)

            assert deleted >= 1
            assert is_access_token_denied(conn, "expired-jti") is False
            assert is_access_token_denied(conn, "valid-jti") is True  # still denied

            conn.close()
        finally:
            import os
            os.unlink(db_path)

    def test_purge_expired_denied_tokens_returns_zero_on_error(self, mock_settings):
        """purge_expired_denied_tokens returns 0 and does not raise on bad table."""
        import sqlite3
        import tempfile

        from app.services.auth_service import purge_expired_denied_tokens

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            # No denylist table at all

            result = purge_expired_denied_tokens(conn)
            assert result == 0
            conn.close()
        finally:
            import os
            os.unlink(db_path)

    def test_purge_expired_denied_tokens_same_calendar_day(self, mock_settings):
        """Regression: same-day past expiry must be purged (not just post-midnight).

        Before the fix, expires_at was stored as ISO-8601 with a 'T' separator and
        '+00:00' suffix (e.g. 2026-06-27T12:00:00+00:00). The purge SQL compared
        against datetime('now') which emits a space separator and no tz
        (e.g. 2026-06-27 22:52:35). At byte 10, 'T' (0x54) > ' ' (0x20), so
        same-day entries were never purged until UTC midnight.
        """
        import sqlite3
        import tempfile
        from datetime import datetime, timedelta, timezone

        from app.services.auth_service import (
            deny_access_token,
            purge_expired_denied_tokens,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            # Construct an expires_at that is 30 minutes in the past but on the SAME
            # UTC calendar day as "now" — stored in SQLite-native format.
            now_utc = datetime.now(timezone.utc)
            past_same_day = now_utc - timedelta(minutes=30)
            past_same_day_str = past_same_day.strftime("%Y-%m-%d %H:%M:%S")

            # Also add a far-future entry so we can confirm only the stale one is removed.
            future_same_day = now_utc + timedelta(days=365)
            future_str = future_same_day.strftime("%Y-%m-%d %H:%M:%S")

            deny_access_token(conn, "same-day-past-jti", user_id=1, expires_at=past_same_day_str)
            deny_access_token(conn, "same-day-future-jti", user_id=1, expires_at=future_str)

            deleted = purge_expired_denied_tokens(conn)

            # The past-same-day entry MUST be purged
            assert deleted >= 1, "Expected at least 1 row purged (same-day-past-jti)"
            remaining_jtis = [
                row[0]
                for row in conn.execute(
                    "SELECT jti FROM access_token_denylist"
                ).fetchall()
            ]
            assert "same-day-past-jti" not in remaining_jtis, (
                "same-day-past-jti should have been purged but was not"
            )
            assert "same-day-future-jti" in remaining_jtis, (
                "same-day-future-jti should still be present"
            )

            conn.close()
        finally:
            import os
            os.unlink(db_path)


class TestRefreshToken:
    """Tests for create_refresh_token."""

    def test_create_refresh_token(self):
        """Verify returns (raw_token, sha256_hash) where hash is SHA256 of raw."""
        import hashlib

        from app.services.auth_service import create_refresh_token

        raw_token, sha256_hash = create_refresh_token()

        # Verify types
        assert isinstance(raw_token, str)
        assert isinstance(sha256_hash, str)

        # Verify hash is SHA256 of raw token
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        assert sha256_hash == expected_hash

        # Verify hash length (SHA256 produces 64 hex characters)
        assert len(sha256_hash) == 64

    def test_create_refresh_token_unique(self):
        """Verify two calls produce different tokens."""
        from app.services.auth_service import create_refresh_token

        token1, hash1 = create_refresh_token()
        token2, hash2 = create_refresh_token()

        # Both tokens and hashes should be different
        assert token1 != token2
        assert hash1 != hash2


class TestVerifyAuthConfig:
    """Tests for verify_auth_config."""

    def test_verify_auth_config_raises_when_no_secret(self):
        """users_enabled=True, jwt_secret_key='' → raises RuntimeError."""
        from app.services.auth_service import verify_auth_config

        # Create mock with empty secret
        mock = MagicMock()
        mock.users_enabled = True
        mock.jwt_secret_key = ""

        with patch("app.config.settings", mock, create=True):
            with pytest.raises(RuntimeError) as exc_info:
                verify_auth_config()

            assert "JWT_SECRET_KEY must be set" in str(exc_info.value)

    def test_verify_auth_config_raises_when_default_secret(self):
        """users_enabled=True, jwt_secret_key='change-me-to-a-random-64-char-string' → raises RuntimeError."""
        from app.services.auth_service import verify_auth_config

        mock = MagicMock()
        mock.users_enabled = True
        mock.jwt_secret_key = "change-me-to-a-random-64-char-string"

        with patch("app.config.settings", mock, create=True):
            with pytest.raises(RuntimeError) as exc_info:
                verify_auth_config()

            assert "JWT_SECRET_KEY must be set" in str(exc_info.value)

    def test_verify_auth_config_passes_when_disabled(self):
        """users_enabled=False → no error."""
        from app.services.auth_service import verify_auth_config

        mock = MagicMock()
        mock.users_enabled = False
        mock.jwt_secret_key = ""  # Even with empty secret, should not raise

        with patch("app.config.settings", mock, create=True):
            # Should not raise any exception
            verify_auth_config()

    def test_verify_auth_config_passes_when_valid_secret(self):
        """users_enabled=True with valid secret → no error."""
        from app.services.auth_service import verify_auth_config

        mock = MagicMock()
        mock.users_enabled = True
        mock.jwt_secret_key = "valid-secret-key-12345"

        with patch("app.config.settings", mock, create=True):
            # Should not raise any exception
            verify_auth_config()


class TestGetJwtConfig:
    """Tests for get_jwt_config."""

    def test_get_jwt_config_returns_tuple(self, mock_settings):
        """Verify it returns (secret_key, algorithm) tuple."""
        from app.services.auth_service import get_jwt_config

        secret, algorithm = get_jwt_config()

        assert secret == TEST_SECRET_KEY
        assert algorithm == TEST_ALGORITHM
        assert isinstance(secret, str)
        assert isinstance(algorithm, str)

    def test_get_jwt_config_raises_when_empty_secret(self):
        """Raise RuntimeError when secret_key is empty."""
        from app.services.auth_service import get_jwt_config

        mock = MagicMock()
        mock.users_enabled = True
        mock.jwt_secret_key = ""

        with patch("app.config.settings", mock, create=True):
            with pytest.raises(RuntimeError) as exc_info:
                get_jwt_config()

            assert "JWT_SECRET_KEY must be set" in str(exc_info.value)

    def test_get_jwt_config_raises_when_default_secret(self):
        """Raise RuntimeError when secret_key is the default placeholder."""
        from app.services.auth_service import get_jwt_config

        mock = MagicMock()
        mock.users_enabled = True
        mock.jwt_secret_key = "change-me-to-a-random-64-char-string"

        with patch("app.config.settings", mock, create=True):
            with pytest.raises(RuntimeError) as exc_info:
                get_jwt_config()

            assert "JWT_SECRET_KEY must be set" in str(exc_info.value)
