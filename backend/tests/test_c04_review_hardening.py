"""Review-follow-up hardening pins for the #709 acceptance checks.

The frozen checks (C1-C12, checkpoint-anchored) pin the acceptance
criteria as issued; this module adds the STRONGER assertions the PR
review demanded without touching the frozen bytes (anti-tampering):
fingerprint-VALUE pins, non-destructive migration semantics, the full
A/B resolver tuple (a re-raising exposure handler must fail), the
disabled-mode composition (a newline must not satisfy AC8's intent),
CSRF RETENTION on mutating routes, and the fingerprint helper's shape.

The seeded_env/prompts_client fixtures intentionally mirror the frozen
file (the manifest pins its bytes, so shared fixtures would edit frozen
history); the duplication is noted in the review ledger.
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
from app.security import admin_scope_fingerprint

_ADMIN_TOKEN = "test-admin-secret"
_LEGACY_TOKEN = "c04-legacy-created-by-probe-token"
_ROTATED_TOKEN = "c04-rotated-before-upgrade-token-9f8e7d6c"


@pytest.fixture()
def seeded_env(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="c04_hardening_")
    db_path = str(Path(temp_dir) / "app.db")
    env = {"db_path": db_path, "temp_dir": temp_dir}
    try:
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        run_migrations(db_path)

        monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
        monkeypatch.setattr("app.config.settings.admin_secret_token", _ADMIN_TOKEN)
        monkeypatch.setattr(
            "app.config.settings.admin_token_scopes",
            {_ADMIN_TOKEN: ["admin:config"]},
        )
        yield env
    finally:
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            if db_path in _pool_cache:
                _pool_cache[db_path].close_all()
                del _pool_cache[db_path]
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture()
def prompts_client(seeded_env):
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# AC1 hardening — the recorded value is the scope fingerprint, not merely
# "not the token"
# ---------------------------------------------------------------------------


def test_post_records_scope_fingerprint(prompts_client, seeded_env):
    resp = prompts_client.post(
        "/api/prompts",
        json={
            "version": "c04-hard-ac1",
            "content": "You are KnowledgeVault. Cite documents as [S1].",
        },
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text

    conn = sqlite3.connect(seeded_env["db_path"])
    try:
        row = conn.execute(
            "SELECT created_by FROM prompt_versions WHERE version = ?",
            ("c04-hard-ac1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # Fingerprint-VALUE pin: NULL/empty/other-constant implementations fail.
    assert row[0] == admin_scope_fingerprint(_ADMIN_TOKEN)


# ---------------------------------------------------------------------------
# AC2 hardening — non-destructive, value-exact, idempotent remediation that
# also catches rotated (format-detectable) token rows
# ---------------------------------------------------------------------------


def test_migration_rewrite_contract_full(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.admin_secret_token", _LEGACY_TOKEN)
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {_LEGACY_TOKEN: ["admin:config"]},
    )
    expected_fingerprint = admin_scope_fingerprint(_LEGACY_TOKEN)
    unrelated_fingerprint = "0123456789abcdef"

    db_path = str(tmp_path / "c04_hard_ac2.db")
    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, 0, ?)",
            [
                ("c04-hard-legacy", "legacy", _LEGACY_TOKEN),
                ("c04-hard-rotated", "rotated", _ROTATED_TOKEN),
                ("c04-hard-fp", "already redacted", unrelated_fingerprint),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    def _snapshot():
        conn = sqlite3.connect(db_path)
        try:
            return dict(
                conn.execute("SELECT version, created_by FROM prompt_versions")
            )
        finally:
            conn.close()

    run_migrations(db_path)
    after = _snapshot()

    # Rows survive (DELETE-based pseudo-remediation fails here).
    assert {"c04-hard-legacy", "c04-hard-rotated", "c04-hard-fp"} <= set(after)
    # Exact rewrite values (NULL-out implementations fail here).
    assert after["c04-hard-legacy"] == expected_fingerprint
    assert after["c04-hard-rotated"] == expected_fingerprint
    assert after["c04-hard-fp"] == unrelated_fingerprint

    # Idempotency: a third pass is a no-op.
    run_migrations(db_path)
    assert _snapshot() == after


# ---------------------------------------------------------------------------
# AC5 hardening — the resolver returns the challenger CONTENT, and all four
# tuple elements agree (a re-raising exposure handler serves control while
# still claiming challenger: only a full-tuple assert catches it)
# ---------------------------------------------------------------------------


def test_exposure_failure_resolver_tuple_is_coherent(monkeypatch, tmp_path):
    import sqlite3 as _sqlite3

    from app.services.ab_testing import ABTestingService
    from app.services.prompt_store import PromptVersionStore

    db_file = tmp_path / "c04_hard_ac5.db"
    run_migrations(str(db_file))
    conn = _sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO organizations (id, name, slug, created_at) "
        "VALUES (?, ?, ?, ?)",
        (10, "Test Org", "test-org", "2024-01-01T00:00:00Z"),
    )
    store = PromptVersionStore(conn)
    store.create_version("c04h_ctrl", "control content", activate=True)
    store.create_version("c04h_chlg", "challenger content")
    ab_service = ABTestingService(conn)
    exp = ab_service.create_experiment(
        "c04h_exp", "c04h_ctrl", "c04h_chlg", split_pct=100
    )
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, org_id, name, created_at) "
        "VALUES (?, ?, ?, ?)",
        (9975, 10, "c04 Hardening Vault", "2024-01-01T00:00:00Z"),
    )
    conn.commit()

    def _raise_locked(self, *args, **kwargs):
        raise _sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ABTestingService, "record_exposure", _raise_locked)

    from app.services.rag_engine import RAGEngine

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    class _FakeEmbSvc:
        def embed_single(self, t):
            return []

        def embed_passage(self, t):
            return []

    engine = RAGEngine(
        db_path=db_path,
        embedding_service=_FakeEmbSvc(),
        vector_store=object(),
        memory_store=object(),
        llm_client=object(),
    )

    prompt_content, ab_experiment_id, ab_variant, prompt_version = (
        engine._resolve_prompt_with_ab_sync(vault_id=9975, user_id=1)
    )
    conn.close()

    # Full-tuple coherence: the challenger CONTENT is served, and every
    # metadata element names the challenger.
    assert prompt_content == "challenger content"
    assert ab_experiment_id == exp.id
    assert ab_variant == "challenger"
    assert prompt_version == "c04h_chlg"


# ---------------------------------------------------------------------------
# AC8 hardening — the disabled composition strips the constant when present
# AND appends the explicit directive; a mere difference (newline) fails
# ---------------------------------------------------------------------------


def test_disabled_mode_composition_is_directive_append():
    from app.services.prompt_builder import (
        CITATION_DISABLED_DIRECTIVE,
        CITATION_INSTRUCTION,
        PromptBuilderService,
    )

    svc = PromptBuilderService()
    override = "You are a custom assistant. Always cite documents as [S1], [S2]."
    default_msgs = svc.build_messages(
        "q", [], [], [], None, system_prompt_override=override, citation_mode=None
    )
    disabled_msgs = svc.build_messages(
        "q", [], [], [], None, system_prompt_override=override, citation_mode="disabled"
    )
    disabled_text = disabled_msgs[0]["content"]
    # The operator's override text is retained...
    assert override in disabled_text
    # ...and the explicit disable directive is appended (composition pin).
    assert CITATION_DISABLED_DIRECTIVE in disabled_text
    # A default-prompt disabled render strips the instruction block while
    # keeping the directive (strip + append semantics, not append-only).
    base_none = svc.build_messages("q", [], [], [], None, citation_mode=None)[0]["content"]
    base_off = svc.build_messages("q", [], [], [], None, citation_mode="disabled")[0]["content"]
    assert CITATION_INSTRUCTION in base_none
    assert CITATION_INSTRUCTION not in base_off
    assert CITATION_DISABLED_DIRECTIVE in base_off


# ---------------------------------------------------------------------------
# CSRF retention — removal was scoped to reads; mutations keep the handshake
# ---------------------------------------------------------------------------


def test_mutating_prompts_routes_still_depend_on_csrf_protect():
    import app.api.routes.prompts as prompts_module
    import app.security as security_module

    def _walk(node):
        yield node
        for sub in getattr(node, "dependencies", None) or []:
            yield from _walk(sub)

    mutating = [
        ("POST", "/prompts"),
        ("POST", "/prompts/{version}/activate"),
        ("POST", "/prompts/ab-experiments"),
        ("POST", "/prompts/ab-experiments/{experiment_id}/end"),
    ]
    matched = 0
    for method, path in mutating:
        hit = False
        for route in prompts_module.router.routes:
            methods = getattr(route, "methods", None) or set()
            if method in methods and route.path == path:
                dependant = getattr(route, "dependant", None)
                nodes = list(_walk(dependant)) if dependant else []
                assert any(
                    getattr(n, "call", None) is security_module.csrf_protect
                    for n in nodes
                ), f"{method} {path} lost its csrf_protect dependency"
                hit = True
                break
        assert hit, f"{method} {path} not found on the prompts router"
        matched += 1
    assert matched == len(mutating)


# ---------------------------------------------------------------------------
# Helper shape pin — empty-scope sentinel and scope-set derivation
# ---------------------------------------------------------------------------


def test_admin_scope_fingerprint_shape(monkeypatch):
    import hashlib

    scopes = {"tok-a": ["admin:config", "admin:users"], "tok-b": ["admin:config"]}
    monkeypatch.setattr("app.config.settings.admin_token_scopes", scopes)

    from app.config import settings

    expected_a = hashlib.sha256(
        ",".join(sorted(settings.admin_token_scopes["tok-a"])).encode()
    ).hexdigest()[:16]
    assert admin_scope_fingerprint("tok-a") == expected_a
    assert len(expected_a) == 16
    assert admin_scope_fingerprint("tok-b") != expected_a
    empty_digest = hashlib.sha256(b"").hexdigest()[:16]
    assert admin_scope_fingerprint("unmapped") == empty_digest
