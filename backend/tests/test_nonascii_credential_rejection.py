"""Acceptance check C2/AC2 (issue #993): non-ASCII bearer credentials must be
rejected with 4xx, never a TypeError-driven HTTP 500.

CPython's ``secrets.compare_digest`` / ``hmac.compare_digest`` raise
``TypeError: comparing strings with non-ASCII characters is not supported``
when handed a non-ASCII ``str``. User-controlled header values flow straight
into those compares at four sites:

* ``app/security.py`` ``require_scope`` factory (scope-token compare),
* ``app/security.py`` ``require_auth`` (bearer compare),
* ``app/api/deps.py`` ``_resolve_active_user`` single-admin branch (reached
  via ``get_current_active_user`` and ``get_current_user_or_service_account``),
* ``app/api/deps.py`` ``require_health_probe_auth`` (``hmac.compare_digest``
  on the ``X-API-Key`` header).

A raw-bytes header ``{b"authorization": "Bearer tökén-Ω".encode("utf-8")}``
(httpx refuses non-ASCII str header values, but bytes pass; Starlette
latin-1-decodes them into a non-ASCII str server-side) makes every wired
entry point return HTTP 500 today.

RED at base (500 on the non-ASCII legs), GREEN after the fix (4xx). The
wrong-ASCII-token controls pin today's 401/403 answers and must not change.

Wiring notes (probe-verified gotchas): every entry point is wired with
``fastapi.Depends(...)`` — a default-value wiring like ``x: dict = require_auth``
NEVER runs the dependency — and routes are ``async def`` for the async
dependencies. ``TestClient(..., raise_server_exceptions=False)`` so the 500
arrives as a response instead of a raised exception.
"""

import os
import sqlite3
import sys
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_user_or_service_account,
    get_db,
    require_health_probe_auth,
)
from app.config import settings  # noqa: E402
from app.security import require_auth, require_scope  # noqa: E402

ADMIN_TOKEN = "issue993-real-admin-token-4f2a"
HEALTH_KEY = "issue993-real-health-key-8e61"
SCOPE = "admin:config"
# Non-ASCII credential material. Sent as RAW BYTES: httpx rejects non-ASCII
# str header values, but a real client can put arbitrary bytes on the wire;
# Starlette latin-1-decodes them server-side into a non-ASCII str, which is
# the defect trigger for compare_digest-on-str.
NONASCII_BEARER_BYTES = "Bearer tökén-Ω".encode("utf-8")
NONASCII_API_KEY_BYTES = "tökén-Ω-key".encode("utf-8")
WRONG_ASCII_BEARER = {"Authorization": "Bearer wrong-ascii-token-993"}
WRONG_ASCII_API_KEY = {"X-API-Key": "wrong-ascii-key-993"}


def _build_app() -> FastAPI:
    app = FastAPI()

    # Every entry point wired with fastapi.Depends (a default-value wiring
    # never executes the dependency) and async def for the async deps.
    @app.get("/require-auth")
    async def plain_auth(payload: dict = Depends(require_auth)):
        return {"ok": True}

    @app.get("/require-scope")
    async def scoped_auth(payload: dict = Depends(require_scope(SCOPE))):
        return {"ok": True}

    @app.get("/user-or-sa")
    async def user_or_sa(payload: dict = Depends(get_current_user_or_service_account)):
        return {"ok": True}

    @app.get("/health-probe")
    async def health_probe(payload: dict = Depends(require_health_probe_auth)):
        return {"ok": True}

    def _override_get_db():
        # The single-admin auth branch never queries the DB; an in-memory
        # connection keeps the dependency honest without touching disk pools.
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


@contextmanager
def _single_admin_posture(tmp_dir):
    """Patch the settings singleton for the whole check; restore on exit."""
    with patch.object(settings, "admin_secret_token", ADMIN_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {ADMIN_TOKEN: [SCOPE]}
        ):
            with patch.object(settings, "users_enabled", False):
                with patch.object(settings, "health_check_api_key", HEALTH_KEY):
                    # require_health_probe_auth's fallback path acquires a
                    # pooled connection against settings.sqlite_path (a
                    # read-only property derived from data_dir); point data_dir
                    # at a scratch dir and drop the cached pool after.
                    with patch.object(settings, "data_dir", tmp_dir):
                        scratch = str(tmp_dir / "app.db")
                        try:
                            yield
                        finally:
                            from app.models.database import (
                                _pool_cache,
                                _pool_cache_lock,
                            )

                            with _pool_cache_lock:
                                pool = _pool_cache.pop(scratch, None)
                            if pool is not None:
                                pool.close_all()


def _client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


def _assert_4xx_not_5xx(status: int, site: str) -> None:
    assert 400 <= status < 500, (
        f"issue #993 AC2 ({site}): a non-ASCII credential must be rejected "
        f"with 4xx (401/403 per the route's invalid-credential contract), "
        f"got HTTP {status} — compare_digest on a non-ASCII str raised "
        "TypeError inside the auth dependency"
    )


def test_require_auth_nonascii_bearer_is_rejected_not_500(tmp_path):
    with _single_admin_posture(tmp_path):
        client = _client()
        # Control first: a wrong ASCII token keeps today's 403 (pinned).
        r2 = client.get("/require-auth", headers=WRONG_ASCII_BEARER)
        assert r2.status_code == 403, (
            "control regression (require_auth): a wrong ASCII bearer must "
            f"keep returning 403, got {r2.status_code}"
        )
        r = client.get("/require-auth", headers={b"authorization": NONASCII_BEARER_BYTES})
        _assert_4xx_not_5xx(r.status_code, "require_auth")


def test_require_scope_nonascii_bearer_is_rejected_not_500(tmp_path):
    with _single_admin_posture(tmp_path):
        client = _client()
        r2 = client.get("/require-scope", headers=WRONG_ASCII_BEARER)
        assert r2.status_code == 403, (
            "control regression (require_scope): a wrong ASCII bearer must "
            f"keep returning 403, got {r2.status_code}"
        )
        r = client.get("/require-scope", headers={b"authorization": NONASCII_BEARER_BYTES})
        _assert_4xx_not_5xx(r.status_code, "require_scope")


def test_user_or_service_account_nonascii_bearer_is_rejected_not_500(tmp_path):
    with _single_admin_posture(tmp_path):
        client = _client()
        r2 = client.get("/user-or-sa", headers=WRONG_ASCII_BEARER)
        assert r2.status_code == 403, (
            "control regression (get_current_user_or_service_account): a "
            f"wrong ASCII bearer must keep returning 403, got {r2.status_code}"
        )
        r = client.get("/user-or-sa", headers={b"authorization": NONASCII_BEARER_BYTES})
        _assert_4xx_not_5xx(r.status_code, "get_current_user_or_service_account")


def test_health_probe_nonascii_x_api_key_is_rejected_not_500(tmp_path):
    with _single_admin_posture(tmp_path):
        client = _client()
        # Control first: a wrong ASCII X-API-Key falls through to the user
        # path and gets today's 401 (no Authorization header on the request).
        r2 = client.get("/health-probe", headers=WRONG_ASCII_API_KEY)
        assert r2.status_code == 401, (
            "control regression (require_health_probe_auth): a wrong ASCII "
            f"X-API-Key must keep returning 401, got {r2.status_code}"
        )
        r = client.get(
            "/health-probe", headers={b"x-api-key": NONASCII_API_KEY_BYTES}
        )
        _assert_4xx_not_5xx(r.status_code, "require_health_probe_auth X-API-Key")
