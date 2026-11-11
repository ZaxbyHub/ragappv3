"""Issue #1062 follow-up pin (AC6): contributing-env-path fails on
names-neither paragraphs.

RED at base, GREEN after the fix: ``check_contributing_env_path``
(``scripts/check_g06_setup_docs.py``) returns ``[]`` for a CONTRIBUTING.md
``**Environment:**`` paragraph naming neither ``.env.example`` nor
``backend/.env`` — the gate fails open on exactly the drift #738 AC15
exists to prevent. The discriminator is the additive names-NEITHER branch;
the shipped-tree leg stays green throughout.

Mirrors the ``_synthetic_repo_texts`` + monkeypatched-``read`` convention in
``backend/tests/test_g06_gate_wiring.py`` (mode-level tests execute the
production gate module against synthetic repo texts).
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "check_g06_setup_docs.py"

NAMES_NEITHER_CONTRIBUTING = (
    "**Environment:** export ADMIN_SECRET_TOKEN and JWT_SECRET_KEY in your "
    "shell profile before starting the backend natively.\n"
)


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("check_g06_setup_docs", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestContributingEnvPathNamesNeither1062:
    """The contributing-env-path mode must FAIL on names-neither paragraphs.

    RED at base (mode returns [] — branch absent), GREEN after the fix
    (additive names-NEITHER branch returns a non-empty FAIL list).
    """

    def test_names_neither_paragraph_fails_the_mode(self, monkeypatch):
        """RED at base, GREEN after the fix: a synthetic CONTRIBUTING.md
        whose **Environment:** paragraph names neither .env.example nor
        backend/.env makes check_contributing_env_path() return a
        non-empty FAIL list."""
        gate = _load_gate_module()
        texts = {"CONTRIBUTING.md": NAMES_NEITHER_CONTRIBUTING}
        monkeypatch.setattr(gate, "read", lambda p: texts[p])
        failures = gate.check_contributing_env_path()
        assert failures, (
            "an **Environment:** paragraph naming neither .env.example nor "
            "backend/.env must FAIL the contributing-env-path mode (the gate "
            "currently fails open on exactly the #738 AC15 drift class)"
        )

    def test_shipped_tree_still_passes(self):
        """PRESERVING pin: against the real shipped files the mode keeps
        returning [] (CONTRIBUTING.md still documents both env-file names),
        so the additive branch cannot break the green tree."""
        gate = _load_gate_module()
        assert gate.check_contributing_env_path() == []
