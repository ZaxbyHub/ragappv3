"""C14 guardrail census for issue #791: no 0-row early return without rollback.

`WikiStore` and `KMSStore` hand their connections back to a shared pool, so
every ``if <cursor>.rowcount == 0:`` branch that returns must roll the implicit
transaction back first (issue #791 closed the last two offenders:
``WikiStore.update_lint_finding`` and ``KMSStore.update_entry``). This census
AST-scans both stores and fails when a new 0-row early return ships without a
rollback call in the same branch, and self-tests the detector on a synthetic
violating module so it cannot pass vacuously.
"""

import ast
import unittest
from pathlib import Path

_STORES = ("wiki_store.py", "kms_store.py")


def _rowcount_zero_branches_missing_rollback(tree):
    """Return source lines of `if <expr>.rowcount == 0:` branches that return
    without calling anything named ``rollback`` inside the branch."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == 0
            and isinstance(test.left, ast.Attribute)
            and test.left.attr == "rowcount"
        ):
            continue
        returns = [st for st in ast.walk(node) if isinstance(st, ast.Return)]
        if not returns:
            continue
        has_rollback = any(
            isinstance(st, ast.Call)
            and isinstance(st.func, ast.Attribute)
            and st.func.attr == "rollback"
            for st in ast.walk(node)
        )
        if not has_rollback:
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

    def test_wiki_and_kms_stores_have_no_unguarded_zero_row_returns(self):
        backend = Path(__file__).resolve().parents[1] / "app" / "services"
        violations = {}
        for store in _STORES:
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
