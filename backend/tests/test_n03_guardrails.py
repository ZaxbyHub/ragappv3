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
    def test_no_unguarded_checkpoint_deserialization_remains(self):
        tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
        guarded_spans = [
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_load_checkpoint"
        ]
        self.assertEqual(len(guarded_spans), 1, "one _load_checkpoint expected")
        guard_lo, guard_hi = guarded_spans[0]
        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "model_validate_json":
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if (
                isinstance(arg, ast.Attribute)
                and arg.attr == "artifact_json"
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "reused"
                and not (guard_lo <= node.lineno <= guard_hi)
            ):
                unguarded.append(node.lineno)
        self.assertEqual(
            unguarded,
            [],
            "draft_pipeline must deserialize reused checkpoints only through "
            "the guarded _load_checkpoint helper "
            f"(unguarded at lines: {unguarded})",
        )

    def test_guarded_loader_exists_and_is_used(self):
        source = PIPELINE.read_text(encoding="utf-8")
        self.assertIn("def _load_checkpoint(", source)
        # One definition plus at least the eight call sites.
        self.assertGreaterEqual(source.count("_load_checkpoint("), 9)

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
