"""Acceptance check C1/AC1 (issue #993): no public audit-HMAC fallback key in
single-admin mode.

``app.services.audit_keys.derive_audit_key`` resolves the fallback audit HMAC
key as ``jwt_secret_key or admin_secret_token``. ``settings.jwt_secret_key``
defaults to the PUBLIC placeholder ``_INSECURE_DEFAULT_JWT_SECRET``
("change-me-to-a-random-64-char-string"), which is non-empty, so a
single-admin deployment (``users_enabled=False``) with a real
``ADMIN_SECRET_TOKEN`` and no ``JWT_SECRET_KEY``/``AUDIT_HMAC_KEY`` derives
the PUBLIC placeholder as its audit HMAC key: anyone knowing the published
default can forge audit digests. The ``reject_insecure_defaults`` validator
only refuses the JWT default when ``users_enabled`` is True, so this posture
boots today.

RED at base (the placeholder is returned), GREEN after the fix (the
placeholder is never used as audit key material in single-admin mode).

All settings mutations are made on the singleton and restored by the
monkeypatch fixture; the AUDIT_HMAC_KEY env var is cleared for the duration
only.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import _INSECURE_DEFAULT_JWT_SECRET, settings  # noqa: E402
from app.services.audit_keys import derive_audit_key  # noqa: E402

REAL_ADMIN_TOKEN = "issue993-real-admin-token-4f2a"


def test_single_admin_posture_never_derives_placeholder_audit_key(monkeypatch):
    """Single-admin posture: derive_audit_key() must not return the placeholder.

    RED at base, GREEN after the fix. The posture is built by patching the
    settings singleton directly: users_enabled=False, a real
    admin_secret_token, jwt_secret_key LEFT AT its default placeholder value,
    and no AUDIT_HMAC_KEY environment override.
    """
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setattr(settings, "users_enabled", False)
    monkeypatch.setattr(settings, "admin_secret_token", REAL_ADMIN_TOKEN)
    # Exactly the deployment defect: jwt_secret_key still holds the shipped
    # public placeholder (config.py: _INSECURE_DEFAULT_JWT_SECRET).
    monkeypatch.setattr(settings, "jwt_secret_key", _INSECURE_DEFAULT_JWT_SECRET)
    assert settings.jwt_secret_key == _INSECURE_DEFAULT_JWT_SECRET, (
        "posture setup failed: jwt_secret_key must sit at the public "
        "placeholder for this check to mean anything"
    )

    key = derive_audit_key()

    placeholder_bytes = _INSECURE_DEFAULT_JWT_SECRET.encode("utf-8")
    assert key != placeholder_bytes, (
        "issue #993 AC1: derive_audit_key() returned the PUBLIC default JWT "
        "secret placeholder as the audit HMAC key in single-admin mode "
        "(users_enabled=False + real ADMIN_SECRET_TOKEN + JWT_SECRET_KEY left "
        "at its default): anyone who knows the published placeholder can "
        "forge audit digests. The placeholder must never be used as audit "
        "key material."
    )
    # Bind to the literal too, in case the constant is renamed/moved.
    assert key != b"change-me-to-a-random-64-char-string", (
        "issue #993 AC1: derive_audit_key() returned the literal public "
        "placeholder b\"change-me-to-a-random-64-char-string\" as the audit "
        "HMAC key in single-admin mode."
    )
    # Non-vacuous: the derived key must still be usable key material.
    assert isinstance(key, bytes) and key, (
        "derive_audit_key() must keep returning non-empty bytes"
    )
