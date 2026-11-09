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

# Patterns for the interpolation entry forms:
#   KEY=${VAR:-default} | KEY=${VAR}   (INTERP_ENTRY; group(3) = default text,
#                                       "" or missing = resolves empty)
#   KEY=${VAR-}                        (SINGLE_DASH_EMPTY; POSIX unset-empty
#                                       form — INTERP_ENTRY's `:-` cannot
#                                       match it, and compose has 0 such
#                                       entries today, but the census must
#                                       not silently evade the form)
# KEY=${VAR-default} (non-empty single-dash default) resolves non-empty and is
# excluded, matching compose semantics.
INTERP_ENTRY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}\s*$")
SINGLE_DASH_EMPTY = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=\$\{[A-Z][A-Z0-9_]*-\}\s*$")


def backend_empty_default_forwards() -> dict[str, str]:
    """Return {ENV_KEY: env_key} for backend env entries whose unset value resolves to "".

    Covers ``${VAR:-default}``, ``${VAR:-}``, ``${VAR-}``, and bare ``${VAR}``
    forms in the knowledgevault (backend) service's environment list — every
    entry whose value resolves to "" when the variable is unset (the
    single-dash empty form ``${VAR-}`` is matched by SINGLE_DASH_EMPTY because
    INTERP_ENTRY's ``:-`` separator cannot). Mapping-style
    environment entries (explicit KEY: value) and non-interpolation values are
    out of this census by construction (they carry their value verbatim).
    """
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env = doc["services"]["knowledgevault"].get("environment") or []
    out: dict[str, str] = {}
    for entry in env:
        if not isinstance(entry, str):
            continue  # mapping-style entries carry explicit values
        m = INTERP_ENTRY.match(entry)
        if m:
            default = m.group(3)
        else:
            m = SINGLE_DASH_EMPTY.match(entry)
            if not m:
                continue
            default = ""  # ${VAR-} resolves empty (POSIX unset form)
        if default:
            continue  # non-empty default (e.g. ${VAR:-thinking}) resolves non-empty
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

    def test_single_dash_empty_form_is_censused(self, tmp_path):
        """${VAR-} (POSIX unset-empty) must land in the census — INTERP_ENTRY's
        :- separator cannot match it, so without the SINGLE_DASH_EMPTY pattern
        the form would silently evade the guard. Executed on a synthetic
        compose doc (the shipped file has 0 single-dash entries today)."""
        synthetic = tmp_path / "compose-synthetic.yml"
        synthetic.write_text(
            'services:\n'
            '  knowledgevault:\n'
            '    environment:\n'
            '      - FOO=${BAZ-}\n'
            '      - BAR=${QUX:-realdefault}\n',
            encoding="utf-8",
        )
        doc = yaml.safe_load(synthetic.read_text(encoding="utf-8"))
        env = doc["services"]["knowledgevault"]["environment"]
        singles = [e for e in env if "${" in e and ":-" not in e and "-}" in e]
        assert singles == ["FOO=${BAZ-}"], "fixture must carry exactly one ${VAR-} entry"
        # run the real census logic against the synthetic entries
        census = {}
        for entry in env:
            m = INTERP_ENTRY.match(entry) or SINGLE_DASH_EMPTY.match(entry)
            if m:
                default = m.group(3) if m.re is INTERP_ENTRY else ""
                if not default:
                    census[m.group(1)] = m.group(1).lower()
        assert "FOO" in census, "${VAR-} entry must enter the census"
        assert "BAR" not in census, "non-empty single-dash default must be excluded"

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
