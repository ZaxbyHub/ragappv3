"""Shared audit HMAC key derivation.

Both audit tables (`document_actions` via `SecretManager.get_hmac_key` and
`security_audit_log` via `security_audit._audit_key`) derive their HMAC key
from this single function when no dedicated `AUDIT_HMAC_KEY*` environment
variable is set, so the two subsystems can never diverge on
key-availability behavior again (issue #561).

Precedence (mirrors the pre-existing security_audit behavior verbatim):
`JWT_SECRET_KEY` -> `ADMIN_SECRET_TOKEN` -> warn-and-use development constant.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_DEVELOPMENT_AUDIT_KEY = "development-audit-key"


def derive_audit_key() -> bytes:
    """Return the fallback audit HMAC key for this deployment.

    Never raises: when neither JWT nor admin secret is configured, a
    development constant is used and a warning is logged (the deployment is
    not production-grade anyway in that state). Deployments that want a
    dedicated key should set ``AUDIT_HMAC_KEY``/``AUDIT_HMAC_KEY_<VERSION>``
    for the document-action audit path, which takes precedence over this
    derivation.
    """
    secret = settings.jwt_secret_key.strip() or settings.admin_secret_token.strip()
    if not secret:
        logger.warning(
            "audit_key: neither JWT_SECRET_KEY nor ADMIN_SECRET_TOKEN is set; "
            "audit HMAC will use a development fallback that is not persistent "
            "across restarts. Set at least one secret for production."
        )
        secret = _DEVELOPMENT_AUDIT_KEY
    return secret.encode("utf-8")
