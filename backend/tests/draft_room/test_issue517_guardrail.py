"""Source-level guardrail for issue #517 (DRAFT-018 defect class).

Class: "a preservation/exclusion contract enforced at some but not all of its
call sites." Every ``run_deterministic_lint`` / ``apply_bounded_rewrites``
call inside the compile pipeline must forward ``locked_spans`` — a call site
that omits them lets a user-locked span be rewritten away exactly the way
DRAFT-018 described, even when every other call site is correct.

The guardrail parses the module source instead of exercising runtime
behavior, because the contract it protects is *per-call-site* and a runtime
test can only observe the call path its fixture drives.
"""

import ast
import pathlib
import unittest

PIPELINE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "app"
    / "services"
    / "draft_pipeline.py"
)

GUARDED_FUNCTIONS = {"run_deterministic_lint", "apply_bounded_rewrites"}


def _call_sites(source: str) -> list[tuple[str, int]]:
    """Yield (function, lineno) for every guarded call in the module."""
    tree = ast.parse(source)
    sites: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in GUARDED_FUNCTIONS:
            sites.append((name, node.lineno))
    return sites


class LockedSpanExclusionGuardrail(unittest.TestCase):
    def test_every_lint_and_rewrite_call_site_forwards_locked_spans(self):
        source = PIPELINE_PATH.read_text(encoding="utf-8")
        sites = _call_sites(source)
        # If this precondition breaks, the guarded functions were renamed or
        # removed -- the guardrail must be revisited, not silently vacuous.
        self.assertGreaterEqual(
            len(sites), 4, "expected at least the four known call sites"
        )
        for name, lineno in sites:
            with self.subTest(function=name, line=lineno):
                call_text = "\n".join(source.splitlines()[lineno - 1 : lineno + 9])
                self.assertIn(
                    "locked_spans",
                    call_text,
                    f"{name} call at {PIPELINE_PATH.name}:{lineno} does not "
                    "forward locked_spans -- user-locked wording can be "
                    "rewritten away (issue #517 DRAFT-018 defect class)",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
