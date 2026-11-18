"""CSRF cookie identity (branch: fix/portal-resilience, T4c).

Contract: the CSRF cookie carries a Meridian-specific name (no collision
with another application's cookie on a shared host) and is issued exactly
once, scoped to the app root path — no shadow-deletion cookies are emitted
(the old generic-named cookie is ignored, never deleted, so a shared-origin
sibling application's cookie is never touched).
"""

from starlette.responses import Response

from app.config import settings
from app.security import CSRF_COOKIE_NAME, issue_csrf_token


class _StubManager:
    def generate_token(self) -> str:
        return "stub-token-value"


def _set_cookie_headers(response: Response):
    return response.headers.getlist("Set-Cookie")


def test_cookie_name_is_meridian_specific():
    assert CSRF_COOKIE_NAME == "meridian-csrf-token"
    # The generic name is not used anywhere as the cookie name.
    assert CSRF_COOKIE_NAME != "X-CSRF-Token"


def test_issue_emits_exactly_one_cookie_scoped_to_app_root(monkeypatch):
    monkeypatch.setattr(settings, "app_root_path", "/meridian", raising=False)
    response = Response()
    issue_csrf_token(response, _StubManager())
    cookies = _set_cookie_headers(response)
    assert len(cookies) == 1, cookies
    assert f"{CSRF_COOKIE_NAME}=stub-token-value" in cookies[0]
    assert "Path=/meridian" in cookies[0]
    # No deletion cookies for broader paths (T4c: nothing of another
    # application's is ever deleted).
    assert not any("Max-Age=0" in c for c in cookies)


def test_issue_root_deployment_single_cookie(monkeypatch):
    monkeypatch.setattr(settings, "app_root_path", "", raising=False)
    response = Response()
    issue_csrf_token(response, _StubManager())
    cookies = _set_cookie_headers(response)
    assert len(cookies) == 1, cookies
    assert "Path=/" in cookies[0]
