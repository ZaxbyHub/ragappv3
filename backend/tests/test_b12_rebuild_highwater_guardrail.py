"""Issue #701 (Workstream B PR 12) — AUTOINCREMENT high-water guardrail.

Defect class (T1-06-S-02): a rename-rebuild migration that drops its backup
table also drops that table's ``sqlite_sequence`` row, so the recreated
AUTOINCREMENT table reissues ids of rows deleted before the rebuild.

Guardrail: an AST census over ``backend/app/models/database.py``. For every
``conn.execute(<string literal>)`` whose SQL drops a BACKUP-named table (the
repo's backup naming conventions end in ``_old`` or ``_legacy_fk``) inside a
function that also renames a table TO that same backup name, a
``_preserve_autoincrement_high_water(conn, <backup>, ...)`` call whose
``backup_table`` argument (second positional) is the string literal backup
name must appear BEFORE that DROP in AST (line) order within the same
function body.

This closes the vacuity modes a function-level census would allow:
misplacement (a call after the DROP cannot preserve anything — the backup's
sequence row is already gone), name mismatch (the literal must equal the
dropped name), and branch blindness (every qualifying DROP site — main-swap
drops AND stale-backup recovery drops — needs its own preceding call).

Extending the census: a future backup naming convention must be added to
BACKUP_SUFFIXES here.
"""
import ast
import re
from pathlib import Path

HELPER = "_preserve_autoincrement_high_water"
BACKUP_SUFFIXES = ("_old", "_legacy_fk")
DATABASE_PY = Path(__file__).resolve().parents[1] / "app" / "models" / "database.py"

_DROP_RE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
_RENAME_RE = re.compile(r"RENAME\s+TO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _string_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _execute_literals(func):
    """Yield (call_lineno, sql) for every ``.execute(<string literal>)``."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            sql = _string_literal(node.args[0])
            if sql is not None:
                yield (node.lineno, sql)


def _helper_calls(func):
    """Yield (call_lineno, backup_name) for helper calls whose
    ``backup_table`` argument (second positional, after ``conn``) is a
    string literal."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == HELPER
            and len(node.args) >= 2
        ):
            backup = _string_literal(node.args[1])
            if backup is not None:
                yield (node.lineno, backup)


def test_every_backup_drop_preserves_autoincrement_high_water():
    source = DATABASE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    problems = []

    for func in tree.body:
        if not isinstance(func, ast.FunctionDef):
            continue
        executes = list(_execute_literals(func))
        renamed_to = {
            match.group(1)
            for _, sql in executes
            for match in _RENAME_RE.finditer(sql)
        }
        helpers = list(_helper_calls(func))
        for lineno, sql in executes:
            match = _DROP_RE.search(sql)
            if not match:
                continue
            dropped = match.group(1)
            if not dropped.endswith(BACKUP_SUFFIXES):
                continue
            if dropped not in renamed_to:
                # A backup-named table dropped without a rename-to-backup in
                # the same function is not a rename-rebuild disposal site.
                continue
            if not any(
                helper_lineno < lineno and backup == dropped
                for helper_lineno, backup in helpers
            ):
                problems.append(
                    f"{func.name}: DROP TABLE {dropped} (line {lineno}) has "
                    f"no preceding {HELPER}(\"{dropped}\", ...) call in the "
                    "same function"
                )

    assert not problems, (
        "rename-rebuild backup drops without high-water preservation:\n"
        + "\n".join(problems)
    )
