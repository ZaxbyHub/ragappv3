"""Structural guardrails for issue #787's defect classes.

* C5 census — every checkpoint-reuse deserialization in ``draft_pipeline``
  must go through the guarded ``_load_checkpoint`` helper (no direct
  ``model_validate_json(reused.artifact_json)`` calls remain).
* C18 census — the Copy/Standards/Research/Fact desk templates must not ask
  the model for any server-computed digest literal (``before_sha256``,
  ``after_sha256``, ``content_sha256``, ``vault_scope_hash``). The draft
  template's caller-fills ``model_call_audit`` convention uses different
  literals (``prompt_sha256``/``output_sha256``) and is untouched.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.draft_prompts import PROMPTS  # noqa: E402

PIPELINE = Path(__file__).resolve().parents[1] / "app" / "services" / "draft_pipeline.py"

BANNED_TEMPLATE_LITERALS = (
    "before_sha256",
    "after_sha256",
    "content_sha256",
    "vault_scope_hash",
)

CENSUSED_TEMPLATES = ("copy", "standards", "research", "fact")


class TestN03CheckpointGuardrails(unittest.TestCase):
    def _deserializations_of_artifact_json(self, tree):
        """Every ``<anything>.model_validate_json(<expr>.artifact_json)`` call.

        Deliberately receiver-agnostic: keying on a local's *name* (the old
        form matched only the identifier ``reused``) let a rename, an alias,
        or a bound-method call reintroduce the unguarded deserialize while
        the census stayed green.
        """
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "model_validate_json" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and arg.attr == "artifact_json":
                hits.append(node.lineno)
        return sorted(hits)

    def _pipeline_tree(self):
        source = PIPELINE.read_text(encoding="utf-8")
        return source, ast.parse(source)

    def test_no_unguarded_checkpoint_deserialization_remains(self):
        source, tree = self._pipeline_tree()
        guarded_spans = [
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_load_checkpoint"
        ]
        self.assertEqual(len(guarded_spans), 1, "one _load_checkpoint expected")
        guard_lo, guard_hi = guarded_spans[0]
        unguarded = [
            line
            for line in self._deserializations_of_artifact_json(tree)
            if not (guard_lo <= line <= guard_hi)
        ]
        self.assertEqual(
            unguarded,
            [],
            "draft_pipeline must deserialize reused checkpoints only through "
            "the guarded _load_checkpoint helper "
            f"(unguarded at lines: {unguarded})",
        )
        # The census is a real guard, not a tautology: the helper itself must
        # contain the deserialization it is trusted to guard.
        self.assertTrue(
            any(
                guard_lo <= line <= guard_hi
                for line in self._deserializations_of_artifact_json(tree)
            ),
            "the census must be able to see the guarded call (self-check)",
        )

    def test_guarded_loader_exists_and_is_used(self):
        source, tree = self._pipeline_tree()
        self.assertIn("def _load_checkpoint(", source)
        # Count CALLS structurally, not as a substring (a comment or docstring
        # mention used to satisfy this proxy, and the floor of 9 sat exactly on
        # the live count so a removed site failed for the wrong reason).
        definitions = 0
        calls = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_load_checkpoint":
                    definitions += 1
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_load_checkpoint":
                    calls += 1
        self.assertEqual(definitions, 1, "exactly one _load_checkpoint definition")
        self.assertEqual(
            calls,
            8,
            "all eight stage reuse sites must go through _load_checkpoint",
        )

    def test_desk_templates_do_not_request_server_computed_digests(self):
        for stage in CENSUSED_TEMPLATES:
            with self.subTest(stage=stage):
                text = PROMPTS[stage].template
                for literal in BANNED_TEMPLATE_LITERALS:
                    self.assertNotIn(
                        literal,
                        text,
                        f"the {stage} template must not ask the model for the "
                        f"server-computed {literal}",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
