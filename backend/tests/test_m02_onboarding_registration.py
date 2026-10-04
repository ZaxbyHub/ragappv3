"""Issue #782 (UI-ENH-07 stage 2): production-app registration guard.

The frozen milestone checks mount the onboarding router on a local FastAPI()
app, and the e2e tier exercises the STUB — neither proves the two are
connected in the real ``app.main``. Implementation review (Phase 4.5 Round 1)
demonstrated that deleting ``app.include_router(onboarding_router,
prefix="/api")`` from main.py leaves every other test green; this guard is
the pin that catches such a loss (a bad merge silently killing the feature
behind the client's fail-open fetch).

Asserts against ``app.openapi()["paths"]`` rather than ``app.routes``: this
FastAPI version stores each include_router as a lazy ``_IncludedRouter``
wrapper whose ``.path`` is None, so only the resolved OpenAPI schema names
the concrete paths (implementation-review probe, 2026-10-03).
"""

from __future__ import annotations


def test_onboarding_routes_registered_on_production_app():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/onboarding/milestones" in paths, (
        "GET /api/onboarding/milestones is not registered on the production "
        "app — the onboarding router include in app/main.py is missing"
    )
    assert "/api/onboarding/milestones/citation-opened" in paths, (
        "POST /api/onboarding/milestones/citation-opened is not registered "
        "on the production app"
    )
    assert "/api/onboarding/milestones/dismiss" in paths, (
        "POST /api/onboarding/milestones/dismiss is not registered on the "
        "production app"
    )

    # PRR-005: pin the methods at the production-app level too (a GET/POST
    # swap on a write route would otherwise only be caught by the frozen
    # route tests' local app).
    method_map = {path: set(methods) for path, methods in app.openapi()["paths"].items()}
    assert method_map["/api/onboarding/milestones"] == {"get"}
    assert method_map["/api/onboarding/milestones/citation-opened"] == {"post"}
    assert method_map["/api/onboarding/milestones/dismiss"] == {"post"}


def test_onboarding_routes_reject_unauthenticated_requests():
    """PRR-005: no other onboarding test asserts the negative — CSRF runs
    under the naive test policy everywhere, so these pins are the only
    guard that the auth dependency (and, transitively, the security
    posture the PR body advertises) stays wired on the production app."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/api/onboarding/milestones").status_code == 401
    assert (
        client.post("/api/onboarding/milestones/citation-opened").status_code
        == 401
    )
    assert client.post("/api/onboarding/milestones/dismiss").status_code == 401
