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
name must appear in the interval between the PREVIOUS qualifying drop of
that same backup name (function start for the first) and this DROP — so
every drop site carries its own dedicated preceding call.

This closes the vacuity modes a function-level census would allow:
misplacement (a call after the DROP cannot preserve anything — the backup's
sequence row is already gone), name mismatch (the literals must equal the
dropped backup name AND the table the same function renames away from), and
branch blindness (every qualifying DROP site — main-swap drops AND
stale-backup recovery drops — needs its own preceding call).

Known limits (documented, not silently ignored): a DROP assembled from
non-literal expressions (f-strings, concatenation, .format()), a DROP
issued through an aliased connection object, a helper call in a
never-executed branch, and module-level DDL escape the pairing rule — the
repo convention is literal ``conn.execute`` strings inside rebuild
functions, and ``executescript`` rebuilds are banned outright (checked
below), so the escape routes require abandoning conventions this census
also polices.

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
_RENAME_FROM_RE = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+RENAME\s+TO", re.IGNORECASE
)


def _string_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _execute_literals(func):
    """Yield (call_lineno, attr, sql) for every
    ``.<attr>(<string literal>)`` call (attr is execute or executescript)."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executescript")
            and node.args
        ):
            sql = _string_literal(node.args[0])
            if sql is not None:
                yield (node.lineno, node.func.attr, sql)


def _helper_calls(func):
    """Yield (call_lineno, backup_name, canonical_name) for helper calls
    whose ``backup_table`` and ``canonical_table`` arguments (second and
    third positional, after ``conn``) are string literals."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == HELPER
            and len(node.args) >= 3
        ):
            backup = _string_literal(node.args[1])
            canonical = _string_literal(node.args[2])
            if backup is not None and canonical is not None:
                yield (node.lineno, backup, canonical)


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
            for _, attr, sql in executes
            if attr == "execute"
            for match in _RENAME_RE.finditer(sql)
        }
        # (canonical, backup) pairs actually renamed in this function — a
        # helper call must name one of these exact pairs, so a typo'd or
        # invented canonical_table (a silent no-op UPDATE) fails the census.
        rename_pairs = {
            (from_match.group(1), to_match.group(1))
            for _, attr, sql in executes
            if attr == "execute"
            for from_match in [_RENAME_FROM_RE.search(sql)]
            if from_match
            for to_match in [
                re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*)", sql[from_match.end() :])
            ]
            if to_match
        }
        helpers = list(_helper_calls(func))
        qualifying_drops = []
        for lineno, attr, sql in executes:
            if attr == "executescript":
                # executescript implicitly commits and is banned for
                # rebuilds; a backup DROP inside one is invisible to the
                # pairing rule, so flag it outright.
                match = _DROP_RE.search(sql)
                if match and match.group(1).endswith(BACKUP_SUFFIXES):
                    problems.append(
                        f"{func.name}: executescript drops backup table "
                        f"{match.group(1)} (line {lineno}) — rebuilds must "
                        "use execute() so the pairing rule can see them"
                    )
                continue
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
            qualifying_drops.append((lineno, dropped))

        # ast.walk is breadth-first, not source order — sort before pairing.
        qualifying_drops.sort()

        # Pair each drop with its OWN preceding helper call: the call must
        # sit in the interval between the PREVIOUS qualifying drop of the
        # same backup name and this one (function start for the first), and
        # its (backup, canonical) pair must match an actual rename in the
        # function. An "any earlier call" rule would let one call satisfy
        # every later drop of the same name, so removing or misplacing a
        # non-first call would silently stay GREEN.
        previous_drop_line = {}
        for lineno, dropped in qualifying_drops:
            lower_bound = previous_drop_line.get(dropped, 0)
            if not any(
                lower_bound < helper_lineno < lineno
                and backup == dropped
                and (canonical, backup) in rename_pairs
                for helper_lineno, backup, canonical in helpers
            ):
                problems.append(
                    f"{func.name}: DROP TABLE {dropped} (line {lineno}) has "
                    f"no {HELPER}(conn, \"{dropped}\", ...) call between its "
                    f"predecessor (line {lower_bound or 'function start'}) "
                    "and the drop"
                )
            previous_drop_line[dropped] = lineno

    assert not problems, (
        "rename-rebuild backup drops without high-water preservation:\n"
        + "\n".join(problems)
    )
