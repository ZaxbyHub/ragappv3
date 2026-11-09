"""Issue #1062 review-round pin: the g06 contributing-env-path mode matches
path TOKENS, not substrings, and both FAIL branches carry a descriptive reason.

RED at the pre-round head (484270eb): the branch substring-matched
``backend/.env``, so a paragraph naming only ``backend/.env.example`` (or any
near-miss like ``backend/.environment``) passed the mode, and the pre-existing
``.env.example``-without-``backend/.env`` branch returned a bare
``FAIL contributing-env-path`` with no reason. GREEN after the token-boundary
fix.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "check_g06_setup_docs.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_g06_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mode_with(contributing_text: str) -> list[str]:
    gate = _load_gate()
    gate.read = lambda _path: contributing_text  # monkeypatch the module-level read helper
    return gate.check_contributing_env_path()


class TestContributingEnvPathNearMissesFail:
    def test_backend_env_example_only_fails(self):
        """A paragraph naming only the near-miss backend/.env.example FAILs
        (the substring form passed it)."""
        result = _mode_with(
            "**Environment:** install dependencies, then copy\n"
            "backend/.env.example to configure the backend.\n"
        )
        assert result, "near-miss backend/.env.example must not satisfy the gate"

    def test_backend_environment_near_miss_fails(self):
        result = _mode_with("**Environment:** see backend/.environment for setup.\n")
        assert result, "near-miss backend/.environment must not satisfy the gate"

    def test_template_only_fails(self):
        """A paragraph naming only the root .env.example FAILs with the
        descriptive reason (the pre-branch message was a bare token)."""
        result = _mode_with("**Environment:** copy .env.example from the repo root.\n")
        assert result, "template-only paragraph must fail"
        assert any("backend/.env" in line for line in result), (
            "the FAIL must name the missing backend/.env requirement"
        )


class TestContributingEnvPathAcceptsDocumentedParagraphs:
    def test_names_both_paths_passes(self):
        result = _mode_with(
            "**Environment:** copy .env.example and create backend/.env\n"
            "before launching the backend natively.\n"
        )
        assert result == []

    def test_names_backend_env_only_passes(self):
        result = _mode_with("**Environment:** create backend/.env from the template.\n")
        assert result == []
