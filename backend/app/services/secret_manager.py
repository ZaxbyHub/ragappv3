"""Simple secret manager helper."""

import logging
import os
from typing import Optional

from app.services.audit_keys import derive_audit_key

logger = logging.getLogger(__name__)

# Below this length an HMAC key is considered weak. We only warn (not raise) so
# existing deployments with short-but-set keys are not broken; operators should
# rotate to a >=32-byte key. 32 bytes = 256 bits, the recommended floor for a
# keyed hash (HMAC-SHA256).
_HMAC_KEY_MIN_BYTES = 32

# The fallback derivation may fire on every audit write; warn once per process
# per key version instead of once per request (issue #561).
_fallback_warned_versions: set[str] = set()


class SecretManagerError(RuntimeError):
    """Raised when a secret cannot be retrieved."""


class SecretManager:
    """Helper for retrieving secrets such as audit HMAC keys."""

    def __init__(self) -> None:
        self.default_hmac_version = os.getenv("AUDIT_HMAC_KEY_VERSION", "v1")
        self.default_aes_version = os.getenv("AES_KEY_VERSION", "v1")

    def get_hmac_key(self, version: Optional[str] = None) -> tuple[bytes, str]:
        """Return the HMAC key and version.

        Resolution order: ``AUDIT_HMAC_KEY_<VERSION>`` -> ``AUDIT_HMAC_KEY`` ->
        shared fallback derivation (JWT secret -> admin token -> development
        constant, identical to ``security_audit_log``'s key, see
        ``app.services.audit_keys``). The fallback keeps ``document_actions``
        audit rows flowing on configurations that never set a dedicated key
        (issue #561); the version string stays the requested version so
        versioned reporting is unchanged.
        """
        version = (version or self.default_hmac_version).lower()
        env_name = f"AUDIT_HMAC_KEY_{version.upper()}"
        key = os.getenv(env_name) or os.getenv("AUDIT_HMAC_KEY")
        if not key:
            key = derive_audit_key().decode("utf-8")
            if version not in _fallback_warned_versions:
                _fallback_warned_versions.add(version)
                logger.warning(
                    "AUDIT_HMAC_KEY for version '%s' is not configured; "
                    "document-action audit HMAC falls back to the shared "
                    "derivation from JWT_SECRET_KEY/ADMIN_SECRET_TOKEN. Set "
                    "%s (or AUDIT_HMAC_KEY) for a dedicated audit key.",
                    version,
                    env_name,
                )
        if len(key) < _HMAC_KEY_MIN_BYTES:
            # Non-breaking: warn only. Existing deployments may use short keys;
            # raising would break them. Operators should rotate to >=32 bytes.
            logger.warning(
                "AUDIT_HMAC_KEY for version '%s' is %d bytes; >=%d bytes is "
                "recommended for HMAC-SHA256. Please rotate to a stronger key.",
                version,
                len(key),
                _HMAC_KEY_MIN_BYTES,
            )
        return key.encode("utf-8"), version

    def get_aes_key(self, version: Optional[str] = None) -> tuple[bytes, str]:
        """Return the AES key and version."""
        version = (version or self.default_aes_version).lower()
        env_name = f"AES_KEY_{version.upper()}"
        key = os.getenv(env_name) or os.getenv("AES_KEY")
        if not key:
            raise SecretManagerError(
                f"AES key for version '{version}' is not configured"
            )
        return key.encode("utf-8"), version
