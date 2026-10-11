"""C01 acceptance check (issue #706, vector-store migration safety).

Structural contract: the startup lifespan must not put its vector-store
``migrate_add_*`` calls under a CANCELLING deadline. The migrations run
through the validated staging swap (``_swap_chunks_table``) whose whole
point is durability — a hard timeout that cancels the await mid-swap (or
between the canonical drop and the final create) is exactly the crashed
window the swap machinery exists to recover from. A migration await that
must not be interrupted has to either run outside ``_safe_await`` /
``asyncio.wait_for`` or pass an explicit ``timeout=None``.

The check is AST-structural against the REAL ``lifespan()`` source (same
idiom as ``test_lifespan_fts_validation.py``::``test_hybrid_disabled_skips_fts_check``
— assert the production call sites' shape, not a copy of them).
"""

import ast
import inspect
import os
import sys
from pathlib import Path

# Hermetic env BEFORE any app import (mirrors
# tests/issue513_checks/test_c9_reindex_dimension_migration.py::_hermetic_env
# and backend/tests/conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

from app.lifespan import lifespan  # noqa: E402


def test_lifespan_migrations_have_no_cancelling_deadline() -> None:
    """No ``await _safe_await(<migrat*()>, ...)`` / ``asyncio.wait_for``
    without an explicit ``timeout=None`` may appear in ``lifespan()``."""
    source = inspect.getsource(lifespan)
    tree = ast.parse(source)

    def _func_name(func: ast.expr):
        """Name for a Name or Attribute call target, else None."""
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _is_timeout_none(keyword: ast.keyword) -> bool:
        return (
            keyword.arg == "timeout"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is None
        )

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        if _func_name(value.func) not in ("_safe_await", "wait_for"):
            continue
        # (a) the first positional argument is a DIRECT migration call
        #     (e.g. app.state.vector_store.migrate_add_vault_id()).
        if not value.args:
            continue
        first_arg = value.args[0]
        if not isinstance(first_arg, ast.Call):
            continue
        inner_name = _func_name(first_arg.func)
        if inner_name is None or "migrat" not in inner_name:
            continue
        # (b) an explicit timeout=None keyword exempts the call; anything
        #     else (a finite timeout, or the wrapper's default deadline)
        #     counts as a cancelling deadline.
        if any(_is_timeout_none(kw) for kw in value.keywords):
            continue
        count += 1

    assert count == 0
