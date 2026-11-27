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

Issue #934 item 1: the RETURNING-style 0-row exit (``row =
X.execute('UPDATE ... RETURNING ...').fetchone()`` followed by ``if row:`` /
``if not row:``) is a zero-row early return without any rowcount guard, so it
now gets the same treatment — the no-row path must roll back before it
returns. ``try`` / ``with`` bodies on the no-row path are descended into, a
path that falls off the end of the function is an implicit ``return None``,
and the stores' transaction context managers (``with self.transaction():``)
and ``finally:`` rollbacks count as finalizing every exit; shapes it cannot
read statically (a ``raise``, loops, a guard nested under another ``if``, a
rollback under a condition) are not flagged — the matcher prefers a missed
exotic shape over flagging code it cannot prove dirty.
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


def _is_returning_fetchone(value: ast.AST) -> bool:
    """True for ``X.execute(<SQL containing RETURNING>, ...).fetchone()`` —
    the statement returns a row only when the UPDATE matched one."""
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "fetchone"
    ):
        return False
    inner = value.func.value
    if not (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "execute"
    ):
        return False
    return any(
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and "returning" in arg.value.lower()
        for arg in inner.args
    )


def _assigned_returning_names(func: ast.AST) -> set:
    """Names bound to a RETURNING fetchone inside ``func``."""
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_returning_fetchone(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _statements_after(node: ast.AST, parents: dict) -> list:
    """The statements following ``node`` in its enclosing block (identity
    match, so structurally identical siblings cannot be confused)."""
    parent = parents.get(node)
    if parent is None:
        return []
    for field in ("body", "orelse", "finalbody"):
        stmts = getattr(parent, field, None)
        if isinstance(stmts, list):
            for i, stmt in enumerate(stmts):
                if stmt is node:
                    return stmts[i + 1 :]
    return []


def _is_rollback_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rollback"
    )


def _finally_rolls_back(node: ast.Try) -> bool:
    """A ``finally:`` rollback runs before any return in the ``try`` body
    completes, so it covers every exit through that body."""
    return any(
        _is_rollback_call(n) for stmt in node.finalbody for n in ast.walk(stmt)
    )


_TXN_CONTEXT_NAMES = frozenset({"transaction", "_write_txn"})


def _is_txn_context(node: ast.AST) -> bool:
    """True for ``with X.transaction():`` / ``with X._write_txn():`` — the
    stores' own transaction context managers (``WikiStore.transaction``,
    ``JobLease._write_txn``). When one ENCLOSES the RETURNING statement it
    owns that transaction — committing on a normal exit, rolling back on an
    exception — so it finalizes every path out of its body. That holds only
    for the enclosing (climb) case; see ``_zero_path_verdict``."""
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return False
    for item in node.items:
        expr = item.context_expr
        if not isinstance(expr, ast.Call):
            continue
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr in _TXN_CONTEXT_NAMES:
            return True
        if isinstance(func, ast.Name) and func.id in _TXN_CONTEXT_NAMES:
            return True
    return False


def _zero_path_after_guard(guard: ast.If, parents: dict, func: ast.AST):
    """The statements the no-row path of an ``if row:`` guard runs, in
    order, and whether that path falls off the end of ``func`` (an implicit
    ``return None``). Returns ``None`` when an enclosing block finalizes the
    transaction on every exit — a ``try:`` with a ``finally:`` rollback, or
    a store transaction context manager (see ``_is_txn_context``) — clean.

    Climbs out of enclosing ``try:`` bodies (through their ``else`` and
    ``finally`` blocks) and other ``with`` bodies, which the normal path
    leaves by continuing after them. Any other enclosing block (loop,
    ``if``, except handler) conservatively stops the climb, so the path is
    not treated as reaching the function end.
    """
    stmts = list(guard.orelse)
    current = guard
    while True:
        stmts += _statements_after(current, parents)
        parent = parents.get(current)
        if parent is func:
            return stmts, True
        if isinstance(parent, ast.Try) and any(s is current for s in parent.body):
            if _finally_rolls_back(parent):
                return None
            stmts += list(parent.orelse) + list(parent.finalbody)
        elif _is_txn_context(parent):
            return None
        elif not isinstance(parent, (ast.With, ast.AsyncWith)):
            return stmts, False
        current = parent


def _zero_path_verdict(stmts, falls_off_end: bool = False) -> str:
    """Verdict for the flat 0-row statement sequence after a guard:

    ``"clean"`` — a rollback call is reached before any exit;
    ``"dirty"`` — a return (or a nested returning guard that does not roll
    back) is reached first, or the path falls off the end of the function
    (implicit ``return None``) with no rollback call anywhere on it;
    ``"unclear"`` — no statically clear exit (e.g. a ``raise``, which the
    enclosing handler may roll back), do not flag.

    ``try`` / ``with`` statements on the path are descended into (to any
    depth), so a rollback wrapped in one still counts as reached. A store
    transaction context manager opened AFTER the guard is descended into
    like any other ``with``: it does not own the UPDATE's still-open
    implicit transaction (``_write_txn`` yields without finalizing when
    ``in_transaction`` is already true; ``transaction()``'s nested ``BEGIN``
    would raise), so it cannot clean the no-row path.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.Expr) and _is_rollback_call(stmt.value):
            return "clean"
        if isinstance(stmt, ast.Return):
            return "dirty"
        if isinstance(stmt, ast.Raise):
            return "unclear"
        if isinstance(stmt, ast.If) and _returns(stmt):
            return "clean" if _taken_path_rolls_back(stmt) else "dirty"
        if isinstance(stmt, ast.Try):
            if _finally_rolls_back(stmt):
                return "clean"
            inner = _zero_path_verdict(
                list(stmt.body) + list(stmt.orelse) + list(stmt.finalbody)
            )
            if inner != "unclear":
                return inner
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            inner = _zero_path_verdict(stmt.body)
            if inner != "unclear":
                return inner
    if falls_off_end and not any(
        _is_rollback_call(node) for stmt in stmts for node in ast.walk(stmt)
    ):
        return "dirty"
    return "unclear"


def _returning_zero_row_exits_missing_rollback(tree):
    """Return source lines of RETURNING-style zero-row exits that do not roll
    the implicit transaction back before returning.

    Two forms, both over names assigned from a RETURNING fetchone:

    * ``if not row:`` — the branch body IS the 0-row path (same taken-path
      rollback requirement as the rowcount guard);
    * ``if row:`` whose body ends in a return (the has-row path always
      exits) — the 0-row path is the else branch and/or the statements
      after the guard, and must roll back before its own return.
    """
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    violations = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = _assigned_returning_names(func)
        if not names:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id in names
            ):
                if _returns(node) and not _taken_path_rolls_back(node):
                    violations.append(node.lineno)
                continue
            if (
                isinstance(test, ast.Name)
                and test.id in names
                and node.body
                and isinstance(node.body[-1], ast.Return)
            ):
                after = _zero_path_after_guard(node, parents, func)
                if after is not None and _zero_path_verdict(*after) == "dirty":
                    violations.append(node.lineno)
    # A guard inside a nested function is visited by both the outer and the
    # inner function walk; report each source line once.
    return sorted(set(violations))


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

    def test_returning_fallthrough_without_rollback_is_detected(self):
        """Issue #934 item 1: the RETURNING shape used by
        WikiStore.claim_next_pending_job — ``if row:`` guard, has-row body
        returns, the 0-row path falls through and must roll back."""
        bad = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 WHERE y = 2 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(bad), [6])
        good = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 WHERE y = 2 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        self._db.rollback()\n"
            "        return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(good), [])

    def test_returning_else_branch_without_rollback_is_detected(self):
        """Issue #934 item 1: a rollback only in an else-branch counts only
        when the else branch is the 0-row path that returns."""
        bad = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        else:\n"
            "            return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(bad), [6])
        good = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        else:\n"
            "            self._db.rollback()\n"
            "            return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(good), [])

    def test_returning_not_row_form_without_rollback_is_detected(self):
        """Issue #934 item 1: ``if not row:`` — the branch body is the 0-row
        path and must roll back before returning."""
        bad = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if not row:\n"
            "            return None\n"
            "        self._db.commit()\n"
            "        return row\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(bad), [6])
        good = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if not row:\n"
            "            self._db.rollback()\n"
            "            return None\n"
            "        self._db.commit()\n"
            "        return row\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(good), [])

    def test_plain_fetchone_without_returning_is_ignored(self):
        """A SELECT fetchone followed by the same guard shape is not a write
        and opens no implicit transaction — it must stay unflagged."""
        plain = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute('SELECT * FROM t').fetchone()\n"
            "        if row:\n"
            "            return row\n"
            "        return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(plain), [])

    def test_returning_implicit_return_without_rollback_is_detected(self):
        """PR #1137 review: deleting both the rollback and the trailing
        ``return None`` leaves an implicit ``return None`` that must still be
        flagged — at function level and inside the ``try:`` body used by
        WikiStore.claim_next_pending_job."""
        bare = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(bare), [6])
        in_try = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        try:\n"
            "            row = self._db.execute(\n"
            "                'UPDATE t SET x = 1 RETURNING *'\n"
            "            ).fetchone()\n"
            "            if row:\n"
            "                self._db.commit()\n"
            "                return row\n"
            "        except Exception:\n"
            "            self._db.rollback()\n"
            "            raise\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(in_try), [7])
        clean = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        try:\n"
            "            row = self._db.execute(\n"
            "                'UPDATE t SET x = 1 RETURNING *'\n"
            "            ).fetchone()\n"
            "            if row:\n"
            "                self._db.commit()\n"
            "                return row\n"
            "            self._db.rollback()\n"
            "        except Exception:\n"
            "            self._db.rollback()\n"
            "            raise\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(clean), [])

    def test_returning_wrapped_rollback_and_raise_are_not_flagged(self):
        """PR #1137 review: a rollback wrapped in ``try:`` / ``with`` on the
        no-row path is reached (no false positive), and a ``raise`` exit is
        left to the enclosing handler (not flagged)."""
        for zero_path in (
            "        try:\n"
            "            self._db.rollback()\n"
            "        except Exception:\n"
            "            pass\n"
            "        return None\n",
            "        with self._lock:\n"
            "            self._db.rollback()\n"
            "        return None\n",
            "        raise LookupError('no pending job')\n",
        ):
            with self.subTest(zero_path=zero_path):
                tree = ast.parse(
                    "class S:\n"
                    "    def f(self):\n"
                    "        row = self._db.execute(\n"
                    "            'UPDATE t SET x = 1 RETURNING *'\n"
                    "        ).fetchone()\n"
                    "        if row:\n"
                    "            self._db.commit()\n"
                    "            return row\n" + zero_path
                )
                self.assertEqual(
                    _returning_zero_row_exits_missing_rollback(tree), []
                )
        # A ``finally:`` rollback runs before the return completes: clean,
        # both on the path and on an enclosing try the guard sits in.
        finally_on_path = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        try:\n"
            "            return None\n"
            "        finally:\n"
            "            self._db.rollback()\n"
        )
        self.assertEqual(
            _returning_zero_row_exits_missing_rollback(finally_on_path), []
        )
        finally_enclosing = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        try:\n"
            "            row = self._db.execute(\n"
            "                'UPDATE t SET x = 1 RETURNING *'\n"
            "            ).fetchone()\n"
            "            if row:\n"
            "                self._db.commit()\n"
            "                return row\n"
            "            return None\n"
            "        finally:\n"
            "            self._db.rollback()\n"
        )
        self.assertEqual(
            _returning_zero_row_exits_missing_rollback(finally_enclosing), []
        )
        # Anti-vacuity for the descent: a try-wrapped path that returns
        # before any rollback (the handler's rollback runs only on error) is
        # still dirty.
        dirty = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        try:\n"
            "            return None\n"
            "        except Exception:\n"
            "            self._db.rollback()\n"
            "            raise\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(dirty), [6])

    def test_returning_inside_store_transaction_is_not_flagged(self):
        """PR #1137 review: ``with self.transaction():`` commits on a normal
        exit, so a no-row path leaving it (implicitly or via a return after
        the block) is finalized — while an arbitrary ``with`` (a lock) is
        climbed through and still flagged."""
        body = (
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            return row\n"
        )
        indented = "".join("    " + line + "\n" for line in body.splitlines())
        for ctx in ("self.transaction()", "self._write_txn()"):
            for tail in ("", "        return None\n"):
                with self.subTest(ctx=ctx, tail=tail):
                    tree = ast.parse(
                        "class S:\n"
                        "    def f(self):\n"
                        f"        with {ctx}:\n" + indented + tail
                    )
                    self.assertEqual(
                        _returning_zero_row_exits_missing_rollback(tree), []
                    )
        lock = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        with self._lock:\n" + indented + "        return None\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(lock), [7])
        # A transaction block opened AFTER the guard does not own the
        # UPDATE's open implicit transaction — returning inside it is dirty.
        for ctx in ("self.transaction()", "self._write_txn()"):
            with self.subTest(after_guard=ctx):
                tree = ast.parse(
                    "class S:\n"
                    "    def f(self):\n" + body + f"        with {ctx}:\n"
                    "            return None\n"
                )
                self.assertEqual(
                    _returning_zero_row_exits_missing_rollback(tree), [6]
                )

    def test_returning_climb_reads_try_else_and_conditional_rollback(self):
        """PR #1137 review: the climb out of a ``try:`` body runs its
        ``else:`` block (a rollback there is reached), and a fall-off path
        whose only rollback sits under a condition is not provably dirty."""
        try_else = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        try:\n"
            "            row = self._db.execute(\n"
            "                'UPDATE t SET x = 1 RETURNING *'\n"
            "            ).fetchone()\n"
            "            if row:\n"
            "                self._db.commit()\n"
            "                return row\n"
            "        except Exception:\n"
            "            raise\n"
            "        else:\n"
            "            self._db.rollback()\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(try_else), [])
        conditional = ast.parse(
            "class S:\n"
            "    def f(self):\n"
            "        row = self._db.execute(\n"
            "            'UPDATE t SET x = 1 RETURNING *'\n"
            "        ).fetchone()\n"
            "        if row:\n"
            "            self._db.commit()\n"
            "            return row\n"
            "        if self._db.in_transaction:\n"
            "            self._db.rollback()\n"
        )
        self.assertEqual(
            _returning_zero_row_exits_missing_rollback(conditional), []
        )

    def test_returning_guard_in_nested_function_reported_once(self):
        """The outer and inner function walks both visit a nested guard; the
        violation is one source line, reported once."""
        tree = ast.parse(
            "def outer(db):\n"
            "    def inner():\n"
            "        row = db.execute('UPDATE t SET x = 1 RETURNING *').fetchone()\n"
            "        if row:\n"
            "            return row\n"
            "        return None\n"
            "    return inner\n"
        )
        self.assertEqual(_returning_zero_row_exits_missing_rollback(tree), [4])

    def test_rowcount_rollback_only_in_else_branch_is_detected(self):
        """Issue #934 item 1: a rollback in the ELSE branch of a rowcount
        guard is not on the 0-row path that returns — still a violation."""
        tree = ast.parse(
            "class S:\n"
            "    def f(self, cur):\n"
            "        if cur.rowcount == 0:\n"
            "            return None\n"
            "        else:\n"
            "            self._db.rollback()\n"
        )
        self.assertEqual(_rowcount_zero_branches_missing_rollback(tree), [3])

    def test_wiki_and_kms_stores_have_no_unguarded_zero_row_returns(self):
        backend = Path(__file__).resolve().parents[1] / "app" / "services"
        violations = {}
        for store in _store_modules(backend):
            source = (backend / store).read_text(encoding="utf-8")
            tree = ast.parse(source)
            found = (
                _rowcount_zero_branches_missing_rollback(tree)
                + _returning_zero_row_exits_missing_rollback(tree)
            )
            if found:
                violations[store] = found
        self.assertEqual(
            violations,
            {},
            "0-row early return(s) without rollback — every "
            "`if cur.rowcount == 0:` branch that returns, and every "
            "RETURNING-fetchone guard's no-row path, must call "
            "self._db.rollback() first (issue #791, C14; issue #934 item 1)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
