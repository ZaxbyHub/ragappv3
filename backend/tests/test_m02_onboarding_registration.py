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
