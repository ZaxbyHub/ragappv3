"""Issue #945 AC3: direct pins on ``app.security.admin_scope_fingerprint``.

``admin_scope_fingerprint`` (backend/app/security/__init__.py) is the
non-secret distinguisher persisted in audit columns (``created_by``) in
place of the admin token itself — the secret must never be stored or
echoed. The #945 review found the existing shape pin
(``test_c04_review_hardening.test_admin_scope_fingerprint_shape``)
non-discriminating on three axes; this dedicated module pins the helper
completely:

- 16 LOWERCASE-HEX characters on the function's ACTUAL output (charset,
  not just length on a locally recomputed expectation);
- the fixed-scope expected digest, pinned as a literal so a change to
  the formula (separator, slice width, casing) fails deliberately;
- ORDER INDEPENDENCE, fed deliberately unsorted scope lists — an
  implementation that dropped ``sorted()`` must fail here (the shape
  pin's input happened to be pre-sorted, so it could not catch that);
- the empty-scope sentinel: an empty scope list and an unmapped token
  both digest to sha256("")[:16];
- EQUALITY with the service-account ``created_by`` formula: a live
  ``POST /api/service-accounts`` must persist exactly
  ``admin_scope_fingerprint(<creating admin token>)`` — the same helper
  the audit-redaction migration matches (app/models/database.py). The
  creating token carries several scopes in deliberately unsorted order, so
  a route that re-inlined its own formula (no ``sorted()``, another
  separator, a hardcoded digest) diverges from the helper and fails.
"""

import hashlib
import re
import sqlite3

import pytest
from c04_fixtures import _ADMIN_TOKEN as SEEDED_ADMIN_TOKEN
from c04_fixtures import seeded_env  # noqa: F401 (consumed via usefixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.service_accounts import router as service_accounts_router
from app.config import settings
from app.security import admin_scope_fingerprint

# This module issues a real POST to a route that depends on csrf_protect
# without exercising the double-submit handshake itself; declare the
# naive policy so the conftest CSRF bypass applies (the source mentions
# "csrf", which would otherwise opt the module into real enforcement).
CSRF_TEST_POLICY = "naive"

_HEX16 = re.compile(r"^[0-9a-f]{16}$")

# sha256("admin:config,admin:users").hexdigest()[:16]
_FIXED_A = "e212fd4a49f9ab57"
# sha256("admin:config").hexdigest()[:16]
_FIXED_B = "554f0701a721d9f4"
# sha256(b"").hexdigest()[:16]
_EMPTY = "e3b0c44298fc1c14"


def test_fingerprint_is_16_lowercase_hex_on_actual_output(monkeypatch):
    """The digest is 16 lowercase hex chars — charset pinned, not just length."""
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {
            "tok-multi": ["admin:config", "admin:users", "documents:read"],
            "tok-single": ["admin:config"],
        },
    )
    for token in ("tok-multi", "tok-single", "totally-unmapped"):
        value = admin_scope_fingerprint(token)
        assert _HEX16.fullmatch(value), (
            f"{token}: {value!r} is not 16 lowercase hex characters"
        )


def test_fixed_scope_expected_value(monkeypatch):
    """A configured scope set digests to its pinned sha256 value.

    The literals are derived once (commented beside each definition) so a
    formula change fails here loudly instead of the test silently
    re-deriving the same wrong answer it asserts against.
    """
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {
            "tok-a": ["admin:config", "admin:users"],
            "tok-b": ["admin:config"],
        },
    )
    assert admin_scope_fingerprint("tok-a") == _FIXED_A
    assert admin_scope_fingerprint("tok-b") == _FIXED_B
    assert admin_scope_fingerprint("tok-a") != admin_scope_fingerprint("tok-b")
    # Cross-check the pinned literal against the documented derivation.
    assert _FIXED_A == hashlib.sha256(
        "admin:config,admin:users".encode()
    ).hexdigest()[:16]


def test_scope_order_independence(monkeypatch):
    """The digest depends on the scope SET, not the configured list order.

    The two scope lists are deliberately ordered differently, so an
    implementation that dropped ``sorted()`` would hash different
    comma-joined strings and fail.
    """
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {
            "tok-unsorted": ["admin:users", "admin:zz-last", "admin:config"],
            "tok-sorted": ["admin:config", "admin:users", "admin:zz-last"],
        },
    )
    assert admin_scope_fingerprint("tok-unsorted") == admin_scope_fingerprint(
        "tok-sorted"
    )


def test_empty_scope_value(monkeypatch):
    """An empty scope list and an unmapped token both digest to sha256("")[:16]."""
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes", {"tok-empty": []}
    )
    assert admin_scope_fingerprint("tok-empty") == _EMPTY
    assert admin_scope_fingerprint("never-configured") == _EMPTY


@pytest.mark.usefixtures("seeded_env")
def test_service_account_created_by_equals_fingerprint(monkeypatch):
    """``created_by`` persisted by POST /api/service-accounts is exactly
    ``admin_scope_fingerprint(<creating admin token>)``.

    The route and the audit-redaction migration both call the helper, but
    nothing asserted the join (issue #945 residual gap 3). This issues a
    real creation request against a temporary, fully migrated database and
    compares the stored column with the helper's output for the same admin
    token.

    ``c04_fixtures.seeded_env`` points ``settings.data_dir`` (so
    ``settings.sqlite_path``) at a temp directory and drains and closes this
    run's pool entry on teardown, so the route's real ``get_pool`` checkout
    lands on the temp database and no open connection outlives the test.

    The fixture maps its admin token to a single scope, under which every
    join/sort variant of the formula collapses to the same digest. Remap it
    to several scopes in deliberately unsorted order (still including the
    ``admin:config`` the route requires) so the equality discriminates.
    """
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {SEEDED_ADMIN_TOKEN: ["admin:users", "admin:config"]},
    )
    db_path = str(settings.sqlite_path)
    app = FastAPI()
    app.include_router(service_accounts_router, prefix="/api")
    expected = admin_scope_fingerprint(SEEDED_ADMIN_TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/api/service-accounts",
            json={"name": "fingerprint-eq-probe", "scopes": ["documents:read"]},
            headers={"Authorization": f"Bearer {SEEDED_ADMIN_TOKEN}"},
        )

    assert response.status_code == 201, response.text
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT created_by FROM service_accounts"
            " WHERE name = 'fingerprint-eq-probe'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "service-account row missing"
    # Equality with the helper for the SAME creating admin token...
    assert row[0] == expected
    # ...and with the documented formula for that token's scope set.
    assert row[0] == _FIXED_A
    # The secret token itself is never persisted in the audit column.
    assert row[0] != SEEDED_ADMIN_TOKEN
