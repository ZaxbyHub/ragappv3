"""Guardrail: Draft Room evidence-freshness hook coverage (issue #516, DRAFT-001 class).

Contract: every production function that removes ``files`` rows or overwrites
their content hash must notify ``on_document_changed`` so dependent Draft Room
evidence is invalidated in the same transaction. The single-document delete
path has done this since SPEC 12.6; the delete-all, vault-purge and
upload-overwrite paths were fixed in issue #516 after the audit found them
silently skipping the hook.

Allowlisted sites are compensation helpers that delete a row the SAME
request/attempt created moments earlier (an upload rollback, a promotion
rollback): the row's lifetime is shorter than any compile, so no draft
evidence can cite it yet.

This test is intentionally source-level: it fails loudly when a new bulk
mutation path is added without the hook, which is exactly the defect class
that produced DRAFT-001.
"""

import ast
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Modules that own ``files``-row mutations.
SCANNED_MODULES = [
    Path("app/api/routes/documents.py"),
    Path("app/api/routes/vaults.py"),
    Path("app/services/document_processor.py"),
    Path("app/services/draft_promotion.py"),
]

# SQL fragments that mutate source rows whose deletion/change must invalidate
# dependent draft evidence.
MUTATION_FRAGMENTS = ("DELETE FROM files", "UPDATE files SET file_hash")

# Compensation helpers deleting rows created within the same request/attempt:
# no draft evidence can reference them yet (see module docstring).
HOOK_EXEMPT_FUNCTIONS = {"_delete_row", "_delete_file_row"}


def _innermost_function_name(func_node: ast.FunctionDef) -> str:
    """The function the mutation line is lexically inside.

    Nested ``def`` blocks (transaction helpers such as ``_atomic_delete``)
    attribute to the nested function itself, which is where the transaction —
    and therefore the hook call — must live.
    """
    return func_node.name


def _notifies_on_document_changed(func_node: ast.AST) -> bool:
    """True when the function invokes (or schedules, e.g. via
    ``asyncio.to_thread(on_document_changed, ...)``) the freshness hook."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", "") or getattr(func, "id", "")
            if name == "on_document_changed":
                return True
        if isinstance(node, ast.Name) and node.id == "on_document_changed":
            # Passed as a callable (asyncio.to_thread / partial), not called here.
            return True
    return False


class FreshnessHookCoverageGuardrail(unittest.TestCase):
    def test_every_files_mutation_path_notifies_the_freshness_hook(self):
        violations = []
        for rel in SCANNED_MODULES:
            source = (BACKEND_ROOT / rel).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(rel))
            # Map each top-level and nested function to its source span.
            spans = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    spans.append((node.lineno, node.end_lineno or node.lineno, node))
            lines = source.splitlines()
            for lineno, line in enumerate(lines, start=1):
                code_part = line.split("#", 1)[0]
                if not any(fragment in code_part for fragment in MUTATION_FRAGMENTS):
                    continue
                candidates = [
                    (start, end, node)
                    for start, end, node in spans
                    if start <= lineno <= end
                ]
                if not candidates:
                    violations.append(f"{rel}:{lineno} mutation outside any function")
                    continue
                innermost = max(candidates, key=lambda item: item[1] - item[0] + (lineno - item[0] < item[1] - lineno))
                # Attribution: the deepest function whose span contains the line.
                innermost = min(candidates, key=lambda item: (item[1] - item[0]))
                node = innermost[2]
                if _innermost_function_name(node) in HOOK_EXEMPT_FUNCTIONS:
                    continue
                if _notifies_on_document_changed(node):
                    continue
                violations.append(
                    f"{rel}:{lineno} `{line.strip()[:60]}` inside "
                    f"{node.name}() mutates files rows without calling "
                    "on_document_changed (issue #516 DRAFT-001 class)"
                )
        self.assertEqual(
            violations,
            [],
            "Draft Room evidence invalidation is bypassed on:\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
