"""P03 db_transaction removal pin (issue #799 AC9).

``db_transaction`` was removed from backend/app (its recorded failure mode:
a CancelledError inside the ``async with`` block skipped the rollback, so a
cancelled transaction was left open). This check PINS the accepted removal
branch: no module under backend/app may define or import ``db_transaction``
again. If a transaction-shaped module exporting it ever returns, the check
instead demands the cancellation-safety contract — cancelling a task inside
``async with db_transaction(pool)`` must roll the transaction back exactly
once.

Expected pre-fix outcome: GREEN (the module is absent at base — this is a
pin, not a reproducing check).
"""

import ast
import asyncio
import contextlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"


def _module_defines_or_imports(tree: ast.Module) -> bool:
    """True when the parsed module defines or imports ``db_transaction``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "db_transaction":
                return True
        elif isinstance(node, ast.ClassDef):
            if node.name == "db_transaction":
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (
                    alias.name == "db_transaction"
                    or alias.name.endswith(".db_transaction")
                    or alias.asname == "db_transaction"
                ):
                    return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if any(
                    isinstance(part, ast.Name) and part.id == "db_transaction"
                    for part in ast.walk(target)
                ):
                    return True
    return False


def _modules_shipping_db_transaction() -> list[Path]:
    """backend/app .py files (tests excluded) defining/importing the symbol."""
    hits: list[Path] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "test" in path.parts:
            continue
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except SyntaxError:
            continue
        if _module_defines_or_imports(tree):
            hits.append(path)
    return hits


class _FakeCursor:
    """Minimal sync cursor surface a transaction block might touch."""

    def execute(self, *args, **kwargs):  # noqa: ARG002 - shape parity
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter([])


class _FakeConnection:
    """sqlite3-shaped connection counting rollbacks/commits."""

    def __init__(self):
        self.rollbacks = 0
        self.commits = 0

    def execute(self, *args, **kwargs):  # noqa: ARG002 - shape parity
        return _FakeCursor()

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


class _FakePool:
    """Permissive pool double for the unknown helper's acquire surface."""

    def __init__(self):
        self.connection = _FakeConnection()

    async def acquire(self):
        return self.connection

    async def release(self, _conn):
        return None

    def connection(self):
        return self.connection


async def _cancellation_probe(db_transaction) -> int:
    """Cancel a task inside ``async with db_transaction(pool)``; report
    the rolled-back count observed on the pool's connection."""
    pool = _FakePool()

    async def hold_the_block():
        async with db_transaction(pool) as conn:
            _touch = getattr(conn, "rollbacks", None)  # noqa: F841 - enter
            await asyncio.sleep(30)

    task = asyncio.ensure_future(hold_the_block())
    await asyncio.sleep(0.05)  # let the task enter the async-with body
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return pool.connection.rollbacks


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestDBTransactionRemovalPin(unittest.TestCase):
    """AC9: db_transaction stays removed — or rolls back on cancellation."""

    def test_db_transaction_stays_removed_or_rolls_back_on_cancel(self):
        hits = _modules_shipping_db_transaction()
        if not hits:
            # The pin holds: the removal branch (the accepted state).
            return
        transaction_like = [p for p in hits if "transaction" in p.stem]
        self.assertTrue(
            transaction_like,
            "AC9: db_transaction re-appeared in backend/app outside a "
            f"transaction-shaped module ({[str(p) for p in hits]}); the "
            "accepted state is that it does not ship at all",
        )
        module = _load_module(transaction_like[0])
        if not hasattr(module, "db_transaction"):
            # A reference exists but the callable is not exported; treat as
            # the removal branch (nothing executable to probe).
            return
        rollbacks = asyncio.run(_cancellation_probe(module.db_transaction))
        self.assertEqual(
            rollbacks,
            1,
            "AC9: cancelling a task inside "
            "``async with db_transaction(pool)`` must roll the transaction "
            "back exactly once (observed rollbacks: "
            f"{rollbacks}); this is the recorded failure mode that made the "
            "helper's removal the accepted branch",
        )


if __name__ == "__main__":
    unittest.main()
