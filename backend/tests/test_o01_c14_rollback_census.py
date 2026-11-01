"""C14 guardrail census for issue #791: no 0-row early return without rollback.

`WikiStore` and `KMSStore` hand their connections back to a shared pool, so
every ``if <cursor>.rowcount == 0:`` branch that returns must roll the implicit
transaction back first (issue #791 closed the last two offenders:
``WikiStore.update_lint_finding`` and ``KMSStore.update_entry``). This census
AST-scans both stores and fails when a new 0-row early return ships without a
rollback call in the same branch, and self-tests the detector on synthetic
violating modules so it cannot pass vacuously.

swarm-pr-review round (pr930-20261015, PRR-016): the original detector only
matched the literal ``X.rowcount == 0`` shape, only scanned two hardcoded
filenames, and accepted a rollback anywhere in the If subtree (including an
un-taken nested branch). It now also matches the equivalent spellings
(``not X.rowcount``, ``X.rowcount < 1``, constant-on-left), requires the
rollback to sit on the taken path before the return, scans every store module
under ``app/services``, and self-tests each of those extensions.
"""

import ast
import unittest
from pathlib import Path


def _store_modules(services_dir: Path):
    """Every ``*_store.py`` under app/services (the modules that hold pooled
    connections and 0-row early returns)."""
    return sorted(p.name for p in services_dir.glob("*_store.py"))


def _is_rowcount_zero_test(test: ast.AST) -> bool:
    """True for the equivalent spellings of "this UPDATE matched no rows":

    ``cur.rowcount == 0`` · ``0 == cur.rowcount`` · ``cur.rowcount < 1`` ·
    ``not cur.rowcount`` (the last one also covers ``<= 0``-style guards
    written as a negation of the truthy rowcount).
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        operand = test.operand
        return (
            isinstance(operand, ast.Attribute) and operand.attr == "rowcount"
        )
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        left, right = test.left, test.comparators[0]

        def is_zero(node):
            return isinstance(node, ast.Constant) and node.value == 0

        def is_one(node):
            return isinstance(node, ast.Constant) and node.value == 1

        def is_rowcount(node):
            return isinstance(node, ast.Attribute) and node.attr == "rowcount"

        if isinstance(op, ast.Eq) and (
            (is_rowcount(left) and is_zero(right))
            or (is_zero(left) and is_rowcount(right))
        ):
            return True
        # ``cur.rowcount < 1`` is ``rowcount == 0`` for a non-negative count.
        if isinstance(op, ast.Lt) and is_rowcount(left) and is_one(right):
            return True
    return False


def _taken_path_rolls_back(branch: ast.If) -> bool:
    """True when a ``rollback`` call executes on the path that reaches the
    return: directly in the branch body, or in every guard of a nested
    conditional that itself returns. A rollback hidden in an un-taken nested
    branch does not count.
    """
    for stmt in branch.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if isinstance(func, ast.Attribute) and func.attr == "rollback":
                return True
        if isinstance(stmt, ast.If) and _returns(stmt):
            # A nested guard that returns: its own body must roll back.
            if not _taken_path_rolls_back(stmt):
                return False
    return False


def _returns(node: ast.AST) -> bool:
    return any(isinstance(st, ast.Return) for st in ast.walk(node))


def _rowcount_zero_branches_missing_rollback(tree):
    """Return source lines of "matched zero rows and returned" branches that
    do not roll the implicit transaction back on the taken path."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _is_rowcount_zero_test(node.test):
            continue
        if not _returns(node):
            continue
        if not _taken_path_rolls_back(node):
            violations.append(node.lineno)
    return violations


class TestZeroRowPathsRollBack(unittest.TestCase):
    def test_census_detects_synthetic_violation(self):
        """Anti-vacuity: the detector flags a synthetic offending branch and
        clears it once a rollback is present."""
        bad = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 0:\n"
            "            return None\n"
            "        self._db.commit()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(bad), [4])
        good = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 0:\n"
            "            self._db.rollback()\n"
            "            return None\n"
            "        self._db.commit()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(good), [])

    def test_detector_covers_equivalent_spellings(self):
        """PRR-016: the equivalent spellings of a 0-row guard are matched."""
        spellings = [
            "if cur.rowcount == 0:",
            "if 0 == cur.rowcount:",
            "if cur.rowcount < 1:",
            "if not cur.rowcount:",
        ]
        for spelling in spellings:
            bad = ast.parse(
                "class S:\n"
                "    def f(self):\n"
                "        cur = self._db.execute('UPDATE t SET x = 1')\n"
                f"        {spelling}\n"
                "            return None\n"
                "        self._db.commit()\n"
            )
            self.assertEqual(
                _rowcount_zero_branches_missing_rollback(bad),
                [4],
                f"{spelling} must be detected",
            )
            good = ast.parse(
                "class S:\n"
                "    def f(self):\n"
                "        cur = self._db.execute('UPDATE t SET x = 1')\n"
                f"        {spelling}\n"
                "            self._db.rollback()\n"
                "            return None\n"
                "        self._db.commit()\n"
            )
            self.assertEqual(
                _rowcount_zero_branches_missing_rollback(good),
                [],
                f"{spelling} must clear once the rollback is present",
            )

    def test_rollback_in_untaken_nested_branch_does_not_count(self):
        """PRR-016: a rollback the taken path never reaches is not a guard."""
        hidden = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 0:\n"
            "            if other:\n"
            "                self._db.rollback()\n"
            "            return None\n"
            "        self._db.commit()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(hidden), [4])

    def test_nested_guard_that_returns_must_roll_back(self):
        nested_bad = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 0:\n"
            "            if other:\n"
            "                return None\n"
            "            self._db.rollback()\n"
            "            return None\n"
            "        self._db.commit()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(nested_bad), [4])
        nested_good = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 0:\n"
            "            if other:\n"
            "                self._db.rollback()\n"
            "                return None\n"
            "            self._db.rollback()\n"
            "            return None\n"
            "        self._db.commit()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(nested_good), [])

    def test_non_rowcount_guards_are_ignored(self):
        unrelated = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        cur = self._db.execute('UPDATE t SET x = 1')\n"
            "        if cur.rowcount == 2:\n"
            "            return None\n"
            "        if not rows:\n"
            "            return None\n"
            "        if cur.something == 0:\n"
            "            return None\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(unrelated), [])

    def test_census_scans_every_store_module(self):
        """PRR-016: a new store module is scanned, not just the two named."""
        backend = Path(__file__).resolve().parents[1] / "app" / "services"
        modules = _store_modules(backend)
        self.assertIn("wiki_store.py", modules)
        self.assertIn("kms_store.py", modules)
        self.assertGreaterEqual(len(modules), 2)

    def test_wiki_and_kms_stores_have_no_unguarded_zero_row_returns(self):
        backend = Path(__file__).resolve().parents[1] / "app" / "services"
        violations = {}
        for store in _store_modules(backend):
            source = (backend / store).read_text(encoding="utf-8")
            found = _rowcount_zero_branches_missing_rollback(ast.parse(source))
            if found:
                violations[store] = found
        self.assertEqual(
            violations,
            {},
            "0-row early return(s) without rollback — every "
            "`if cur.rowcount == 0:` branch that returns must call "
            "self._db.rollback() first (issue #791, C14)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
