"""Issue #709 acceptance checks (C04): prompt-version created_by hygiene.

Freezes three contracts about who/what is recorded as the creator of a
prompt version and what the create endpoint tells the operator:

* AC1 — POST /api/prompts must not persist the admin bearer token into
  ``prompt_versions.created_by``. Today ``require_scope`` returns the raw
  token as ``user_id`` (app/security.py) and the create handler stores it
  verbatim (app/api/routes/prompts.py), leaking a durable credential into
  the database. The check reads the raw DB column after a 201 POST and
  compares it against the configured admin secret — the comparison result
  (a bool) is all that may appear in test output, never the token value.

* AC2 — re-running the schema migrations must remediate legacy
  ``prompt_versions`` rows whose ``created_by`` holds the configured admin
  token. A legacy row seeded with the token must not survive a second
  ``run_migrations`` pass unchanged.

* AC4 — the create response must carry a truthy warning field when the
  submitted content lacks the citation-label vocabulary / SECURITY
  BOUNDARY contract markers (a bare "You are a helpful assistant.").
  ``CreatePromptVersionResponse`` has no such field today, so a bare
  content POST returns a warning-free body.

Harness mirrors ``tests/test_issue645_converted_route_http.py``: a
run_migrations temp DB, a static admin token mapped to ``admin:config``
via patched ``settings.admin_token_scopes``, and an explicit
``app.dependency_overrides[csrf_protect]`` override (this module is
CSRF-aware, so the conftest autouse bypass leaves it alone — the module
manages CSRF itself).
"""

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.prompts import router as prompts_router
from app.models.database import run_migrations

# Test-only credential constant (same pattern as test_issue645 / test_prompt_ab_variants).
# Never a real secret; only booleans derived from it may reach test output.
_ADMIN_TOKEN = "test-admin-secret"

# Distinct test-only constant for the legacy-row migration probe (AC2).
_LEGACY_TOKEN = "c04-legacy-created-by-probe-token"


@pytest.fixture()
def seeded_env(monkeypatch):
    """Temp DB (full schema) + patched settings, prompts-route flavor.

    ``settings.data_dir`` drives ``settings.sqlite_path`` (a property), so
    the route handlers' ``get_pool(str(settings.sqlite_path))`` checkouts
    land on the real pool over the temp database.
    """
    temp_dir = tempfile.mkdtemp(prefix="c04_prompts_created_by_")
    db_path = str(Path(temp_dir) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    run_migrations(db_path)

    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    # require_scope("admin:config") for the prompts router: a static admin
    # token mapped to the scope server-side (test_prompt_ab_variants pattern).
    monkeypatch.setattr("app.config.settings.admin_secret_token", _ADMIN_TOKEN)
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {_ADMIN_TOKEN: ["admin:config"]},
    )

    env = {"db_path": db_path, "temp_dir": temp_dir}
    yield env

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture()
def prompts_client(seeded_env):
    """App exposing the prompts router with the module-managed CSRF override."""
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# AC1 — the create handler must not persist the admin bearer token
# ---------------------------------------------------------------------------


def test_post_does_not_persist_bearer_token(prompts_client, seeded_env):
    """A 201 POST must leave the raw admin token OUT of created_by.

    Reads the raw DB column (not the API response) so the durable leak is
    what is checked. ``leaked`` is a bool; the assertion output can never
    contain the credential value itself.
    """
    resp = prompts_client.post(
        "/api/prompts",
        json={
            "version": "c04-leak-probe",
            "content": "You are KnowledgeVault. Cite documents as [S1], [S2].",
            "activate": True,
        },
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text

    from app.config import settings

    conn = sqlite3.connect(seeded_env["db_path"])
    try:
        row = conn.execute(
            "SELECT created_by FROM prompt_versions WHERE version = ?",
            ("c04-leak-probe",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "created row must exist after 201"
    created_by = row[0]

    leaked = created_by == settings.admin_secret_token
    assert leaked is False


# ---------------------------------------------------------------------------
# AC2 — re-running migrations must remediate legacy token-bearing rows
# ---------------------------------------------------------------------------


def test_migration_remediates_legacy_token_rows(monkeypatch, tmp_path):
    """A legacy created_by row holding the admin token must not survive a
    fresh run_migrations pass unchanged."""
    monkeypatch.setattr("app.config.settings.admin_secret_token", _LEGACY_TOKEN)

    db_path = str(tmp_path / "c04_legacy_created_by.db")
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "prompt_versions" in tables, "migrations must create prompt_versions"
        # Seed the legacy row using the schema the migrations create.
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("c04-legacy", "legacy leaked content", 0, _LEGACY_TOKEN),
        )
        conn.commit()
    finally:
        conn.close()

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        created_by_values = [
            r[0]
            for r in conn.execute("SELECT created_by FROM prompt_versions")
        ]
    finally:
        conn.close()

    leaked = any(value == _LEGACY_TOKEN for value in created_by_values)
    assert leaked is False


# ---------------------------------------------------------------------------
# AC4 — the create response must warn about missing contract markers
# ---------------------------------------------------------------------------


def test_create_warns_when_contract_markers_missing(prompts_client, seeded_env):
    """A bare content POST (no citation-label vocabulary, no SECURITY
    BOUNDARY markers) must return a body with a truthy warning field."""
    resp = prompts_client.post(
        "/api/prompts",
        json={"version": "c04-bare", "content": "You are a helpful assistant."},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text

    has_warning = any(
        "warn" in key.lower() and bool(value)
        for key, value in resp.json().items()
    )
    assert has_warning is True
