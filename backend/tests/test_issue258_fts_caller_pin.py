"""Issue #258 (E2) — pin: the lifespan call site IGNORES validate_fts_index's
return value.

The TEST-003 extraction (app/lifespan.py) broadened validate_fts_index's
surface with a NEW observable bool return. No production caller inspects it:
the single call site inside ``lifespan()`` awaits the coroutine as a bare
expression statement and discards the result — startup behavior is identical
to the former inline block (log on failure, always continue).

This pin fails if anyone starts consuming the return value at the call site
(assigning it or branching on it), which would be a silent startup-behavior
change smuggled in through the new surface.

Mechanism: source-inspection of the REAL lifespan module via AST (the
sanctioned mechanism for non-behaviorally-falsifiable contracts — same
precedent as test_auth_override_async_path's iscoroutine tripwire). The AST
check is structural, not string-matching, so formatting changes cannot
defeat it.
"""

import ast
import inspect
import unittest


def _lifespan_module_ast() -> ast.Module:
    import app.lifespan as lifespan_module

    return ast.parse(inspect.getsource(lifespan_module))


class TestFTSCallerIgnoresReturn(unittest.TestCase):
    """The production validate_fts_index call site discards the bool."""

    def test_lifespan_call_site_discards_validate_fts_index_result(self) -> None:
        tree = _lifespan_module_ast()

        # Parent map for climbing from the call to its syntactic context.
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "validate_fts_index"
        ]
        self.assertEqual(
            len(calls),
            1,
            "expected exactly one production call to validate_fts_index "
            f"(the lifespan startup site); found {len(calls)}",
        )
        call = calls[0]

        # The call must be AWAITED (not an un-awaited coroutine).
        await_node = parents.get(call)
        self.assertIsInstance(
            await_node,
            ast.Await,
            "the lifespan call site must await validate_fts_index",
        )

        # The awaited result must be a bare expression statement — the
        # result is discarded. An assignment (ast.Assign / AnnAssign /
        # NamedExpr) or a branch (ast.If / While / Return / BoolOp / IfExp /
        # Assert ...) would consume the new bool surface and change startup
        # semantics; the pin fails for ANY of those shapes.
        await_parent = parents.get(await_node)
        self.assertIsInstance(
            await_parent,
            ast.Expr,
            "the lifespan call site must NOT assign or branch on "
            "validate_fts_index's return value (it is a fire-and-log "
            f"check); awaiting context is {type(await_parent).__name__}",
        )

        # And the Expr must be a full statement of the lifespan body (not,
        # e.g., the test of an if written as a bare expression — the Expr
        # parent must be a statement container, and the Expr itself carries
        # no value consumers by construction).
        self.assertIsInstance(
            parents.get(await_parent),
            (ast.If, ast.AsyncFor, ast.For, ast.While, ast.With, ast.AsyncWith,
             ast.Try, ast.ExceptHandler, ast.FunctionDef, ast.Module),
            "the discarded-result expression must be a statement of the "
            "lifespan body",
        )

    def test_validate_fts_index_is_a_module_level_coroutine_function(self) -> None:
        """The extracted surface is importable and async (contract shape)."""
        from app.lifespan import validate_fts_index

        self.assertTrue(inspect.iscoroutinefunction(validate_fts_index))
        self.assertTrue(inspect.isfunction(validate_fts_index))


if __name__ == "__main__":
    unittest.main()
