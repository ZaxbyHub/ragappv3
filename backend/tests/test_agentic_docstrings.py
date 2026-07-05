"""Tests verifying module docstrings of agentic_planner.py and agentic_tools.py
accurately state they ARE wired into RAGEngine.query behind
settings.agentic_rag_enabled.

This is a source-inspection test since the docstrings are the deliverable
artifact of the task — no behavioural change was made to either module.
"""

import ast
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_module_docstring(source: str) -> str:
    """Return the module-level docstring from a Python source string."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                return node.body[0].value.value
    return ""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestAgenticPlannerDocstring:
    """Verify agentic_planner.py module docstring accuracy."""

    _SOURCE_FILE = Path(__file__).parent.parent / "app" / "services" / "agentic_planner.py"
    _WIRED_CLAIM = re.compile(
        r"wired\s+into\s+``RAGEngine\.query``\s+behind\s+``settings\.agentic_rag_enabled``",
        re.IGNORECASE,
    )
    _NOT_WIRED_CLAIM = re.compile(
        r"not\s+.*wired|not\s+connected|not\s+integrated",
        re.IGNORECASE,
    )

    def test_module_docstring_is_present(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert docstring, "agentic_planner.py must have a module docstring"

    def test_docstring_states_it_is_wired_into_ragengine(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert self._WIRED_CLAIM.search(docstring), (
            "agentic_planner.py module docstring must state it is wired into "
            "`RAGEngine.query` behind `settings.agentic_rag_enabled`. "
            f"Got docstring:\n{docstring!r}"
        )

    def test_docstring_does_not_claim_not_wired(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert not self._NOT_WIRED_CLAIM.search(docstring), (
            "agentic_planner.py module docstring must NOT contain claims like "
            "'not wired' / 'not connected' / 'not integrated' — it IS wired. "
            f"Got docstring:\n{docstring!r}"
        )


class TestAgenticToolsDocstring:
    """Verify agentic_tools.py module docstring accuracy."""

    _SOURCE_FILE = Path(__file__).parent.parent / "app" / "services" / "agentic_tools.py"
    _WIRED_CLAIM = re.compile(
        r"wired\s+into\s+``RAGEngine\.query``\s+behind\s+``settings\.agentic_rag_enabled``",
        re.IGNORECASE,
    )
    _NOT_WIRED_CLAIM = re.compile(
        r"not\s+.*wired|not\s+connected|not\s+integrated",
        re.IGNORECASE,
    )

    def test_module_docstring_is_present(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert docstring, "agentic_tools.py must have a module docstring"

    def test_docstring_states_it_is_wired_into_ragengine(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert self._WIRED_CLAIM.search(docstring), (
            "agentic_tools.py module docstring must state it is wired into "
            "`RAGEngine.query` behind `settings.agentic_rag_enabled`. "
            f"Got docstring:\n{docstring!r}"
        )

    def test_docstring_does_not_claim_not_wired(self):
        source = self._SOURCE_FILE.read_text(encoding="utf-8")
        docstring = _extract_module_docstring(source)
        assert not self._NOT_WIRED_CLAIM.search(docstring), (
            "agentic_tools.py module docstring must NOT contain claims like "
            "'not wired' / 'not connected' / 'not integrated' — it IS wired. "
            f"Got docstring:\n{docstring!r}"
        )


class TestBothModulesStateWiredIndependence:
    """Cross-check: both modules independently state the same wiring claim."""

    def test_both_modules_have_the_wired_claim(self):
        planner_src = (
            Path(__file__).parent.parent / "app" / "services" / "agentic_planner.py"
        ).read_text(encoding="utf-8")
        tools_src = (
            Path(__file__).parent.parent / "app" / "services" / "agentic_tools.py"
        ).read_text(encoding="utf-8")

        wired_pat = re.compile(
            r"wired\s+into\s+``RAGEngine\.query``\s+behind\s+``settings\.agentic_rag_enabled``",
            re.IGNORECASE,
        )

        planner_match = wired_pat.search(_extract_module_docstring(planner_src))
        tools_match = wired_pat.search(_extract_module_docstring(tools_src))

        assert planner_match, "agentic_planner.py docstring missing wiring claim"
        assert tools_match, "agentic_tools.py docstring missing wiring claim"
