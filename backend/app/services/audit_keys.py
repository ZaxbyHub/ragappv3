"""Shared audit HMAC key derivation.

Both audit tables (`document_actions` via `SecretManager.get_hmac_key` and
`security_audit_log` via `security_audit._audit_key`) derive their HMAC key
from this single function when no dedicated `AUDIT_HMAC_KEY*` environment
variable is set, so the two subsystems can never diverge on
key-availability behavior again (issue #561).

Precedence (issue #993): `JWT_SECRET_KEY` when actually CHANGED from the
well-known insecure default -> `ADMIN_SECRET_TOKEN` -> warn-and-use
development constant. The unchanged-default JWT secret is treated as unset:
it is a public constant, so a plain truthiness chain would hand the audit
trail a forgeable integrity key in single-admin deployments
(``USERS_ENABLED=false``) where ``ADMIN_SECRET_TOKEN`` is the real — and
only — credential and the refuse-unchanged-defaults validator does not fire.
"""

import logging

from app.config import _INSECURE_DEFAULT_JWT_SECRET, settings

logger = logging.getLogger(__name__)

_DEVELOPMENT_AUDIT_KEY = "development-audit-key"


def derive_audit_key() -> bytes:
    """Return the fallback audit HMAC key for this deployment.

    Never raises: when neither a changed-from-default JWT secret nor an
    admin secret is configured, a development constant is used and a warning
    is logged (the deployment is not production-grade anyway in that state).
    Deployments that want a dedicated key should set ``AUDIT_HMAC_KEY``/
    ``AUDIT_HMAC_KEY_<VERSION>`` for the document-action audit path, which
    takes precedence over this derivation.

    The well-known insecure JWT default is treated as unset (issue #993):
    ``settings.jwt_secret_key`` defaults to that public constant, so in
    single-admin mode the key must fall through to the configured admin
    token rather than sign audit rows with a value anyone can compute.
    """
    jwt_secret = settings.jwt_secret_key.strip()
    if not jwt_secret or jwt_secret == _INSECURE_DEFAULT_JWT_SECRET:
        jwt_secret = None
    secret = jwt_secret or settings.admin_secret_token.strip()
    if not secret:
        logger.warning(
            "audit_key: neither JWT_SECRET_KEY nor ADMIN_SECRET_TOKEN is set; "
            "audit HMAC will use a development fallback that is not persistent "
            "across restarts. Set at least one secret for production."
        )
        secret = _DEVELOPMENT_AUDIT_KEY
    return secret.encode("utf-8")
