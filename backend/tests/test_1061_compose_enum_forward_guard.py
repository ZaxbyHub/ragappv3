"""Issue #1061 durable guard: compose empty-default env forwards must tolerate Settings startup.

RED at base (the empty ``DOCUMENT_PARSING_STRATEGY`` forward makes ``Settings``
construction raise), GREEN after the fix (the forward becomes the short
pass-through form, so the key is omitted when unset and the census entry
disappears).

Guard semantics (plan-critic Round 3 corrected): every currently-present
backend-service ``${VAR:-}``/``${VAR-}``/``${VAR}`` empty-default environment
forward in docker-compose.yml must permit SUCCESSFUL ``Settings`` construction
when the key is injected as the empty string. ANY raised exception — including
a pydantic ValidationError — fails: an empty value that breaks construction
crash-loops the backend container at import (``settings = Settings()`` is
module-level) while every CI gate stays green.

Dispositions (census, recorded in-file):
- REQUIRED bare forwards (``- ADMIN_SECRET_TOKEN=${ADMIN_SECRET_TOKEN}``,
  ``- JWT_SECRET_KEY=${JWT_SECRET_KEY}``): excluded — these keys are required
  secrets whose empty value failing construction is the intended fail-closed
  startup behavior, not the crash class (the operator MUST set them; compose
  never injects an empty default for them because the bare form forwards only
  what the operator's environment provides).
- #750-owned consumer-level defects (JWT_ALGORITHM, ADMIN_RATE_LIMIT,
  VECTOR_METRIC, IMAP_MAILBOX): they PASS construction (verified) and are
  therefore allowed by this guard — their downstream consumer breakage is
  tracked by issue #750, NOT certified safe here.
- MULTIMODAL_MODEL: empty is a documented meaningful value (not #750-owned).

Build/sidecar variables (HF_TOKEN, VITE_API_URL, and friends) never appear in
the backend service's environment block and so are out of this census by
construction.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

# Bare ${VAR} forwards for required secrets: empty must fail construction by
# design, so they are not part of the tolerance census.
REQUIRED_BARE_FORWARDS = {"ADMIN_SECRET_TOKEN", "JWT_SECRET_KEY"}

ENV_ENTRY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=\$\{([A-Z][A-Z0-9_]*)(?::-?|-?)\}\s*$")
EMPTY_DEFAULT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=\$\{[A-Z][A-Z0-9_]*(?::-|-\}|)\}\s*$")


def backend_empty_default_forwards() -> dict[str, str]:
    """Return {ENV_KEY: env_key} for backend env entries whose unset value resolves to "".

    Covers ``${VAR:-}``, ``${VAR-}``, and bare ``${VAR}`` forms in the
    knowledgevault (backend) service's environment list.
    """
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env = doc["services"]["knowledgevault"].get("environment") or []
    out: dict[str, str] = {}
    for entry in env:
        if not isinstance(entry, str):
            continue  # mapping-style entries carry explicit values
        m = EMPTY_DEFAULT.match(entry) or ENV_ENTRY.match(entry)
        if not m:
            continue
        key = m.group(1)
        if key in REQUIRED_BARE_FORWARDS:
            continue
        out[key] = key.lower()
    return out


class TestComposeEmptyForwardStartupTolerance:
    """Every backend empty-default forward must permit Settings construction."""

    def test_census_is_nonempty(self):
        """The census itself must stay populated (guards against parser rot).

        RED-at-base context: 28 entries at base; DOCUMENT_PARSING_STRATEGY
        leaves the census (short form) after the fix, so 27 remain. A floor of
        20 keeps the guard meaningful without pinning exact churn.
        """
        census = backend_empty_default_forwards()
        assert len(census) >= 20, (
            f"backend empty-default forward census collapsed to {len(census)} "
            "entries — either docker-compose.yml moved to short forms (fine, "
            "but revisit this floor) or the parser broke"
        )

    @pytest.mark.parametrize("env_key", sorted(backend_empty_default_forwards().keys()))
    def test_empty_value_permits_settings_construction(self, env_key):
        """Injecting the key as "" must NOT break Settings construction.

        RED at base for DOCUMENT_PARSING_STRATEGY (enum validator rejects "");
        stays GREEN for every #750-owned consumer-level key because those pass
        construction by design.
        """
        env = {
            "ADMIN_SECRET_TOKEN": "a" * 40,
            "JWT_SECRET_KEY": "b" * 40,
            env_key: "",
        }
        with patch.dict("os.environ", env, clear=False):
            Settings(_env_file=None)  # must NOT raise


class TestParsingStrategyShortFormPin:
    """The DOCUMENT_PARSING_STRATEGY forward must stay the short pass-through form."""

    def test_short_form_forward(self):
        """The entry must be bare ``- DOCUMENT_PARSING_STRATEGY`` (no ``=``).

        RED at base (``${VAR:-}`` empty-default form); GREEN after the fix.
        Reintroducing the empty-default form fails here and re-breaks startup.
        """
        text = COMPOSE.read_text(encoding="utf-8")
        assert re.search(r"^\s*- DOCUMENT_PARSING_STRATEGY\s*$", text, re.M), (
            "DOCUMENT_PARSING_STRATEGY must use the short pass-through form "
            "(omitted when unset); the ${VAR:-} empty-default form "
            "crash-loops the backend (enum-strict validator rejects \"\")"
        )
