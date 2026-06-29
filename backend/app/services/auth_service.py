"""Authentication service with Argon2id, bcrypt (legacy), and JWT."""

import asyncio
import hashlib
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Tuple

import jwt  # PyJWT
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    argon2__type="id",  # Argon2id variant
    argon2__memory_cost=19456,  # ~19 MiB, OWASP-recommended
    argon2__time_cost=2,
    argon2__parallelism=1,
)

# Dedicated executor for CPU-bound hashing operations (Argon2id and legacy bcrypt).
# Prevents blocking the async event loop under high concurrency.
_auth_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="auth-cpu")

ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 30
ALGORITHM = "HS256"


def get_jwt_config() -> Tuple[str, str]:
    """Get JWT configuration from settings."""
    from app.config import settings

    secret_key = settings.jwt_secret_key
    if not secret_key or secret_key == "change-me-to-a-random-64-char-string":
        raise RuntimeError("JWT_SECRET_KEY must be set when users are enabled")
    return secret_key, ALGORITHM


def hash_password(plain_password: str) -> str:
    """Hash a password using the default scheme (Argon2id)."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    try:
        # Try passlib first — handles argon2id, bcrypt, and all schemes registered
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        logger.warning("passlib verification failed, falling back to bcrypt")
    # Fallback to bcrypt directly if passlib fails (handles bcrypt version issues)
    try:
        import bcrypt

        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except Exception:
        logger.error("Password verification failed", exc_info=True)
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    """Return True if the hash uses a deprecated/non-default scheme and should be re-hashed.

    Uses passlib's built-in needs_update check, which returns True when the hash
    was produced by a scheme other than the currently configured default (argon2id).

    Returns False for malformed or unrecognized hash strings (does not raise).
    """
    try:
        return pwd_context.needs_update(hashed_password)
    except Exception:
        # Malformed hash — treat as needing upgrade (or at minimum, don't crash)
        return False


async def async_verify_password(plain_password: str, hashed_password: str) -> bool:
    """Async wrapper for verify_password using a dedicated thread executor.

    CPU-bound hashing (Argon2id and legacy bcrypt) must run on a thread pool
    to avoid starving the asyncio event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _auth_executor, verify_password, plain_password, hashed_password
    )


async def async_hash_password(plain_password: str) -> str:
    """Async wrapper for hash_password using a dedicated thread executor.

    CPU-bound hashing (Argon2id) must run on a thread pool to avoid
    starving the asyncio event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _auth_executor, hash_password, plain_password
    )


def password_strength_check(plain_password: str) -> None:
    """Validate password strength. Raises ValueError with specific message if invalid."""
    if not plain_password:
        raise ValueError("Password cannot be empty")
    if len(plain_password) > 128:
        raise ValueError("Password cannot exceed 128 characters")
    if plain_password != plain_password.strip():
        raise ValueError("Password cannot be only whitespace")
    if len(plain_password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    if not any(char.isdigit() for char in plain_password):
        raise ValueError("Password must contain at least one digit")
    if not any(char.isupper() for char in plain_password):
        raise ValueError("Password must contain at least one uppercase letter")


def compute_client_fingerprint(user_agent: str | None) -> str:
    """Compute a SHA-256 hexdigest fingerprint from a User-Agent string.

    Handles None (treated as "") for clients that omit the header.
    The fingerprint is stable per UA — a stolen token replayed from a different
    browser/device will have a different UA and thus be rejected.
    """
    return hashlib.sha256((user_agent or "").encode()).hexdigest()


def create_access_token(
    user_id: int, username: str, role: str, *, client_fingerprint: str | None = None
) -> str:
    """Create a JWT access token with a unique jti claim for revocation support.

    Args:
        user_id: The user's integer ID.
        username: The user's username.
        role: The user's role string.
        client_fingerprint: Optional SHA-256 hexdigest of the client's User-Agent.
            When provided, a ``fpt`` claim is embedded in the token and validated
            on every authenticated request to bind the token to the issuing client.
            A token replayed from a different UA will be rejected.
    """
    secret, algorithm = get_jwt_config()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expires,
        "type": "access",
        "iat": now,
        "jti": secrets.token_urlsafe(16),
    }
    if client_fingerprint is not None:
        payload["fpt"] = client_fingerprint
    return jwt.encode(payload, secret, algorithm=algorithm)


class TokenExpiredError(Exception):
    """Raised when a token has expired."""

    pass


class TokenInvalidError(Exception):
    """Raised when a token is invalid."""

    pass


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.

    Raises:
        TokenExpiredError: When the token has expired
        TokenInvalidError: When the token is invalid or malformed
    """
    try:
        secret, algorithm = get_jwt_config()
        return jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError:
        raise TokenExpiredError("Token has expired")
    except jwt.InvalidTokenError:
        raise TokenInvalidError("Invalid token")
    except Exception as e:
        logger.warning("Unexpected error decoding token: %s", type(e).__name__)
        raise TokenInvalidError("Invalid token")


def create_refresh_token() -> Tuple[str, str]:
    """
    Create a refresh token.
    Returns: (raw_token, sha256_hash)
    Store only the hash in the database.
    """
    raw_token = secrets.token_urlsafe(32)
    sha256_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, sha256_hash


def deny_access_token(db, jti: str, user_id: int, expires_at: str) -> None:
    """Add an access token's jti to the denylist so it cannot be used again.

    Uses INSERT OR REPLACE so re-denial is idempotent.
    """
    from datetime import datetime, timezone

    db.execute(
        "INSERT OR REPLACE INTO access_token_denylist (jti, user_id, expires_at, revoked_at) VALUES (?, ?, ?, ?)",
        (jti, user_id, expires_at, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def is_access_token_denied(db, jti: str) -> bool:
    """Return True if the given jti is in the access-token denylist."""
    row = db.execute(
        "SELECT 1 FROM access_token_denylist WHERE jti = ?", (jti,)
    ).fetchone()
    return row is not None


def purge_expired_denied_tokens(db) -> int:
    """Delete denylist entries whose expires_at has passed.

    Returns the number of rows deleted. Best-effort; errors are logged but
    never raised so this never blocks the hot path.
    """
    try:
        cursor = db.execute(
            "DELETE FROM access_token_denylist WHERE expires_at < datetime('now')"
        )
        db.commit()
        return cursor.rowcount
    except Exception:
        logger.warning("Failed to purge expired access-token denylist entries")
        return 0


def verify_auth_config() -> None:
    """Verify auth configuration is valid. Call at startup."""
    from app.config import settings

    if settings.users_enabled:
        if not settings.jwt_secret_key or settings.jwt_secret_key in (
            "",
            "change-me-to-a-random-64-char-string",
        ):
            raise RuntimeError(
                "JWT_SECRET_KEY must be set when USERS_ENABLED=True. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
