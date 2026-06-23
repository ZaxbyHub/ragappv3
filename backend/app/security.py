"""Authentication, CSRF, and toggle utilities."""

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from typing import Callable, Dict
from urllib.parse import urlsplit

import redis
from fastapi import Header, HTTPException, Request, Response

from app.config import settings
from app.utils.paths import csrf_cookie_path

logger = logging.getLogger("security")
CSRF_COOKIE_NAME = "X-CSRF-Token"


class _InMemoryCSRFStore:
    """Thread-safe in-memory fallback for CSRF tokens when Redis is unavailable.

    Includes max-size eviction and lazy cleanup to prevent unbounded memory growth.
    """

    MAX_SIZE = 10_000

    def __init__(self, ttl: int = 900) -> None:
        self.ttl = ttl
        self._store: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _cleanup_expired(self) -> None:
        """Remove expired tokens. Must be called with lock held."""
        now = time.time()
        expired = [k for k, exp in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            # Periodic cleanup on write to prevent unbounded growth
            if len(self._store) >= self.MAX_SIZE:
                self._cleanup_expired()
            # If still at capacity after cleanup, evict oldest entry
            if len(self._store) >= self.MAX_SIZE:
                oldest_key = min(self._store, key=self._store.get)
                del self._store[oldest_key]
            self._store[key] = time.time() + ttl

    def get(self, key: str) -> str | None:
        with self._lock:
            expiry = self._store.get(key)
            if expiry is None:
                return None
            if time.time() > expiry:
                del self._store[key]
                return None
            return "1"

    def expire(self, key: str, ttl: int) -> None:
        with self._lock:
            if key in self._store:
                self._store[key] = time.time() + ttl

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def ping(self) -> bool:
        return True


class _SQLiteCSRFStore:
    """Worker-safe SQLite-backed CSRF token store.

    All workers share the same SQLite database file, so tokens generated
    by one worker are visible to all workers. Replaces _InMemoryCSRFStore
    as the primary fallback when Redis is unavailable.
    """

    def __init__(self, db_path: str, ttl: int = 900) -> None:
        self.ttl = ttl
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS csrf_tokens (token_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def _cleanup_expired(self) -> None:
        try:
            self._conn.execute(
                "DELETE FROM csrf_tokens WHERE expires_at <= ?",
                (time.time(),),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._cleanup_expired()
            expires_at = time.time() + ttl
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO csrf_tokens (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                    (key, time.time(), expires_at),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error("SQLite CSRF store error during setex: %s", exc)
                raise RuntimeError("CSRF storage unavailable") from exc

    def get(self, key: str) -> str | None:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "SELECT 1 FROM csrf_tokens WHERE token_hash = ? AND expires_at > ?",
                    (key, time.time()),
                )
                return "1" if cursor.fetchone() else None
            except sqlite3.Error as exc:
                logger.error("SQLite CSRF store error during get: %s", exc)
                raise RuntimeError("CSRF storage unavailable") from exc

    def expire(self, key: str, ttl: int) -> None:
        with self._lock:
            expires_at = time.time() + ttl
            try:
                self._conn.execute(
                    "UPDATE csrf_tokens SET expires_at = ? WHERE token_hash = ?",
                    (expires_at, key),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error("SQLite CSRF store error during expire: %s", exc)
                raise RuntimeError("CSRF storage unavailable") from exc

    def delete(self, key: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM csrf_tokens WHERE token_hash = ?", (key,)
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error("SQLite CSRF store error during delete: %s", exc)
                raise RuntimeError("CSRF storage unavailable") from exc

    def ping(self) -> bool:
        with self._lock:
            try:
                self._conn.execute("SELECT 1")
                return True
            except sqlite3.Error:
                return False


class CSRFManager:
    def __init__(self, redis_url: str, ttl: int = 900, db_path: str = "") -> None:
        self.ttl = ttl
        self._redis: redis.Redis | None = None
        self._fallback_store: _InMemoryCSRFStore | _SQLiteCSRFStore | None = None
        self._use_fallback = False
        self._lock = threading.Lock()

        try:
            self._redis = redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            logger.info("CSRFManager connected to Redis successfully")
        except Exception as exc:
            logger.warning("Redis unavailable for CSRF: %s", exc)
            self._use_fallback = True
            self._fallback_store = None
            # Try SQLite first (shared across workers), then fall back to in-memory
            if db_path:
                try:
                    self._fallback_store = _SQLiteCSRFStore(db_path, ttl=ttl)
                    logger.info("CSRFManager using SQLite-backed store at %s", db_path)
                except Exception as sqlexc:
                    logger.warning("SQLite CSRF store init failed: %s", sqlexc)
            if not self._fallback_store:
                self._fallback_store = _InMemoryCSRFStore(ttl=ttl)
                logger.warning("CSRFManager using in-memory fallback (not worker-safe!)")

    def _get_store(self):
        """Returns the active store (Redis or in-memory fallback)."""
        if self._use_fallback and self._fallback_store:
            return self._fallback_store
        if self._redis:
            return self._redis
        raise HTTPException(status_code=503, detail="CSRF storage unavailable")

    def _check_redis_available(self) -> bool:
        """Check if Redis is available, switch back from fallback if recovered."""
        with self._lock:
            if self._use_fallback and self._redis:
                try:
                    self._redis.ping()
                    logger.info(
                        "Redis recovered, switching from in-memory fallback to Redis"
                    )
                    self._use_fallback = False
                    self._fallback_store = None
                    return True
                except redis.RedisError:
                    pass
            return not self._use_fallback

    def generate_token(self) -> str:
        self._check_redis_available()
        store = self._get_store()
        token = secrets.token_urlsafe(16)
        key = f"csrf:{token}"
        try:
            store.setex(key, self.ttl, "1")
        except (redis.RedisError, ConnectionError, TimeoutError, RuntimeError) as exc:
            logger.error("Storage error during token generation: %s", exc)
            raise HTTPException(status_code=503, detail="CSRF storage unavailable")
        return token

    def validate_token(self, token: str) -> bool:
        if not token:
            return False
        self._check_redis_available()
        store = self._get_store()
        key = f"csrf:{token}"
        try:
            exists = store.get(key)
        except (redis.RedisError, ConnectionError, TimeoutError, RuntimeError) as exc:
            logger.error("Storage error during token validation: %s", exc)
            raise HTTPException(status_code=503, detail="CSRF storage unavailable")
        if exists:
            try:
                store.expire(key, self.ttl)
            except (redis.RedisError, ConnectionError, TimeoutError, RuntimeError):
                logger.warning("Failed to extend CSRF token TTL")
            return True
        return False

    def revoke_token(self, token: str) -> None:
        store = self._get_store()
        try:
            store.delete(f"csrf:{token}")
        except (redis.RedisError, ConnectionError, TimeoutError, RuntimeError):
            logger.warning("Failed to revoke CSRF token (storage error)")


def get_csrf_manager(request: Request) -> CSRFManager:
    manager = getattr(request.app.state, "csrf_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="CSRF service unavailable")
    return manager


def require_scope(scope: str) -> Callable:
    """Require a specific scope derived from a server-side token→scopes mapping.

    The X-Scopes header is INTENTIONALLY IGNORED for security — it was a
    privilege-escalation vector where any caller with a valid admin token
    could claim any scope. Scopes now derive from settings.admin_token_scopes
    (a server-side dict) keyed on the verified admin token.

    Authentication is verified FIRST via secrets.compare_digest (constant-time),
    then authorization is checked via the server-side scope mapping. This
    order prevents timing side-channels in the dict lookup and ensures the
    scope decision is only made for authenticated callers.
    """
    def dependency(
        authorization: str | None = Header(None),
    ) -> dict[str, str]:
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header missing")
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        parts = authorization.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        token = parts[1].strip()
        # Guard: if admin_secret_token is empty/unset, fail explicitly rather
        # than silently accepting any token (which would happen because
        # compare_digest(token, "") is True when token == "").
        if not settings.admin_secret_token:
            raise HTTPException(status_code=503, detail="Authentication not configured")
        # Authentication FIRST: verify token identity with constant-time comparison.
        # This must happen before any authz decision to avoid timing side-channels
        # and to ensure the scope lookup is only performed for authenticated callers.
        if not secrets.compare_digest(token, settings.admin_secret_token):
            raise HTTPException(status_code=403, detail="Unauthorized token")
        # Authorization: derive scopes from server-side mapping (NOT from any
        # client-supplied header). The X-Scopes header is intentionally ignored.
        token_scopes = settings.admin_token_scopes.get(token, [])
        if scope.lower() not in [s.lower() for s in token_scopes]:
            raise HTTPException(status_code=403, detail="Missing required scope")
        return {"user_id": token}

    return dependency


def _csrf_test_bypass_active() -> bool:
    """Test-only CSRF bypass, active only under pytest.

    Returns True ONLY when BOTH signals are present:
      - ``PYTEST_CURRENT_TEST`` — set automatically by pytest while a test runs.
      - ``RAGAPP_CSRF_TEST_BYPASS=1`` — toggled per-test by the test harness for
        CSRF-naive route suites (many build their own app and don't reconstruct
        the double-submit cookie/header flow).

    Both conditions can never hold in production, so this cannot disable CSRF
    for real traffic. The dedicated CSRF suites leave the toggle off and
    therefore exercise the real validator below.
    """
    return (
        os.environ.get("RAGAPP_CSRF_TEST_BYPASS") == "1"
        and "PYTEST_CURRENT_TEST" in os.environ
    )


def _normalize_origin(value: str) -> str | None:
    """Reduce a URL or origin string to ``scheme://host[:port]`` (lowercased).

    Returns ``None`` when the value cannot be parsed into a scheme + authority,
    so callers can treat it as "no usable origin".
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    # Drop the default port so "https://h" and "https://h:443" compare equal
    # (browsers omit the default port, but non-browser clients may include it).
    if (scheme == "https" and netloc.endswith(":443")) or (
        scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    return f"{scheme}://{netloc}"


def _csrf_allowed_origins() -> set[str] | None:
    """Origin allowlist derived from the configured CORS origins.

    Returns ``None`` to signal "do not enforce" when the allowlist is empty or
    contains a ``*`` wildcard — enforcing in those cases would either reject all
    browser traffic or be meaningless. A legitimate browser's ``Origin`` is the
    app's own public origin, which must already appear in ``backend_cors_origins``
    for the SPA's credentialed requests to pass CORS, so this list is the
    authoritative set of trusted origins.
    """
    raw = settings.backend_cors_origins or []
    if any(str(o).strip() == "*" for o in raw):
        return None
    allowed = {_normalize_origin(str(o)) for o in raw}
    allowed.discard(None)
    return allowed or None


def _validate_csrf_origin(request: Request) -> None:
    """Reject state-changing requests whose Origin/Referer is not trusted.

    Defense-in-depth on top of the double-submit cookie + SameSite=Lax. Kept
    deliberately conservative to avoid breaking legitimate or proxied traffic:
      * No ``Origin`` and no ``Referer`` header -> allow (non-browser/legacy
        clients; matches the existing contract that the token endpoint works
        without an Origin).
      * Allowlist empty or wildcard -> allow (enforcement disabled).
      * Otherwise the request's origin must be in the allowlist.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    allowed = _csrf_allowed_origins()
    if allowed is None:
        return
    if _normalize_origin(source) not in allowed:
        raise HTTPException(status_code=403, detail="CSRF origin validation failed")


def csrf_protect(
    request: Request,
    x_csrf_token: str = Header(""),
) -> str:
    if _csrf_test_bypass_active():
        return "test-bypass"
    _validate_csrf_origin(request)
    csrf_manager = get_csrf_manager(request)
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie or not x_csrf_token or cookie != x_csrf_token:
        raise HTTPException(status_code=403, detail="CSRF token missing or mismatch")
    if not csrf_manager.validate_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return x_csrf_token


def issue_csrf_token(response: Response, csrf_manager: CSRFManager) -> str:
    token = csrf_manager.generate_token()
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=settings.csrf_token_ttl,
        samesite="lax",
        secure=settings.csrf_cookie_secure,
        httponly=False,
        path=csrf_cookie_path(),
    )
    return token


def require_auth(
    authorization: str | None = Header(None),
) -> dict:
    """Simple Bearer token auth. Requires valid token when admin_secret_token is configured."""
    # Authentication is always required - check if token is configured
    if not settings.admin_secret_token:
        # No token configured - require auth header to be set
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please set ADMIN_SECRET_TOKEN in environment.",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization scheme")
    parts = authorization.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = parts[1].strip()
    if not secrets.compare_digest(token, settings.admin_secret_token):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return {"authenticated": True}


def log_action_digest(key: bytes, *parts: str) -> str:
    message = "|".join(parts)
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
