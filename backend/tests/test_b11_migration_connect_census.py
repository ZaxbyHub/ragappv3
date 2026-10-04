"""Guardrail (issue #700, T1-05-S-05 / class C12): every sqlite3.connect in
the STARTUP/MIGRATION connect class must set a busy timeout >= 30000 ms
before any table access.

Scope: backend/app/models/database.py (outside SQLiteConnectionPool),
backend/app/models/migration_journal.py, backend/app/lifespan.py. A connect
is compliant when it passes ``timeout=<n>`` with n >= 30.0 (resolving
module constants, incl. MIGRATION_CONNECT_TIMEOUT_SECONDS imported from
app.models.migration_journal) or when a ``busy_timeout`` >= 30000 statement
follows within 5 lines with only ``PRAGMA journal_mode`` / comments in
between (the init_db / get_db_connection shape).

Pre-fix baseline (RED at a81e6c42): 79 of 80 census connects non-compliant
— only get_db_connection's PRAGMA shape passed. Request-time service
connects (vector_store, rag_engine, feedback_reranker, security) are a
different class and are intentionally OUT of this census (see the issue's
Phase 4.2 dispositions).
"""

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "app"
TARGETS = [
    BACKEND / "models" / "database.py",
    BACKEND / "models" / "migration_journal.py",
    BACKEND / "lifespan.py",
]
BUSY_OK = re.compile(r"busy_timeout\s*[=:]\s*\"?(\d+)")
TOLERATED = re.compile(r"^\s*(#|conn\.execute\(\s*\"PRAGMA\s+journal_mode)")


def _module_constants(tree: ast.Module, mj_consts: dict[str, float]) -> dict[str, float]:
    consts: dict[str, float] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            try:
                consts[n.targets[0].id] = float(ast.literal_eval(n.value))
            except (ValueError, TypeError):
                continue
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module == "app.models.migration_journal":
            for a in n.names:
                if a.name in mj_consts:
                    consts[a.asname or a.name] = mj_consts[a.name]
    return consts


def _kwarg_timeout(node: ast.Call, consts: dict[str, float]) -> float | None:
    for kw in node.keywords:
        if kw.arg == "timeout":
            try:
                return float(ast.literal_eval(kw.value))
            except (ValueError, TypeError):
                pass
            if isinstance(kw.value, ast.Name) and kw.value.id in consts:
                return consts[kw.value.id]
    return None


def _in_pool_class(tree: ast.Module, lineno: int) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "SQLiteConnectionPool":
            if n.lineno <= lineno <= (n.end_lineno or 0):
                return True
    return False


def _census() -> tuple[list[str], int]:
    mj_tree = ast.parse(TARGETS[1].read_text(encoding="utf-8"))
    mj_consts: dict[str, float] = {}
    for n in mj_tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            try:
                mj_consts[n.targets[0].id] = float(ast.literal_eval(n.value))
            except (ValueError, TypeError):
                continue
    bad: list[str] = []
    total = 0
    for path in TARGETS:
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        consts = _module_constants(tree, mj_consts)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "connect"):
                continue
            if not (isinstance(f.value, ast.Name) and f.value.id == "sqlite3"):
                continue
            if path.name == "database.py" and _in_pool_class(tree, node.lineno):
                continue  # the pool's own factory is its own contract
            total += 1
            t = _kwarg_timeout(node, consts)
            if t is not None and t >= 30.0:
                continue
            ok = False
            for ln in range(
                node.end_lineno or node.lineno,
                min((node.end_lineno or node.lineno) + 5, len(lines)),
            ):
                line = lines[ln]
                m = BUSY_OK.search(line)
                if m and int(m.group(1)) >= 30000:
                    ok = True
                    break
                if TOLERATED.search(line):
                    continue
                break
            if not ok:
                rel = path.relative_to(BACKEND.parent)
                bad.append(f"{rel.as_posix()}:{node.lineno}")
    return bad, total


def test_startup_migration_connects_carry_30s_busy_timeout():
    bad, total = _census()
    assert not bad, (
        f"{len(bad)} of {total} startup/migration sqlite3.connect calls lack a "
        f">=30000 ms busy timeout before table access (issue #700 class C12): "
        f"{bad[:10]}{'...' if len(bad) > 10 else ''}"
    )
