"""Frozen acceptance checks for the collated #743/#744 issue-tracer trace (H07).

Issue #744 — fixture fidelity. The route-test harness, migration-script
fixtures, and multi-file index suites hand-copy simplified schemas instead of
building on the production schema, so each fixture is missing exactly the
table, column, or second row that would let it observe the defect it exists
to catch. These checks pin the fixture shapes: two schema-parity checks
against the DB built by ``init_db`` + ``run_migrations``, two mutation kills
against the suites that must consume the rebuilt fixtures, and four
structural checks over the fixture sources.

These checks are RED at base and turn GREEN after the fixture-fidelity fix
lands. No network access; all writes stay inside ``tmp_path``. This module
is self-contained (it deliberately duplicates the small mutation harness
also found in test_h06_mutation_kills.py so each file is independently
copyable).
"""

import ast
import importlib.util
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Frozen ignore set for the mutation-copy harness (identical to the one in
# test_h06_mutation_kills.py; duplicated on purpose for self-containment).
_COPY_IGNORE = shutil.ignore_patterns(
    ".venv",
    "venv",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".swarm",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage*",
)
_SUBPROCESS_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Shared mutation-kill harness (duplicated from test_h06_mutation_kills.py)
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> str:
    """Read a file byte-faithfully (no newline translation)."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_source(path: Path, text: str) -> None:
    """Write a file byte-faithfully (preserve the line endings read)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy backend/ and scripts/ into tmp_path and return the copy root."""
    copy_root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "backend", copy_root / "backend", ignore=_COPY_IGNORE)
    shutil.copytree(REPO_ROOT / "scripts", copy_root / "scripts", ignore=_COPY_IGNORE)
    return copy_root


def _mutate(copy_root: Path, relpath: str, anchor: str, mutated: str) -> int:
    """Apply one textual mutation to <copy>/backend/<relpath>.

    Asserts the anchor occurs at least once so a moved/renamed anchor errors
    loudly instead of silently passing the kill check.
    """
    target = copy_root / "backend" / relpath
    src = _read_source(target)
    occurrences = src.count(anchor)
    assert occurrences >= 1, (
        f"MUTATION ANCHOR MISSING (harness problem, not a mutant-survival "
        f"result): anchor {anchor!r} does not occur in backend/{relpath}. "
        f"Update the anchor per the localization log."
    )
    _write_source(target, src.replace(anchor, mutated, 1))
    return occurrences


def _run_suite(copy_root: Path, suite: list, tmp_path: Path, check_name: str) -> int:
    """Run pytest on ``suite`` inside <copy>/backend and assert >=1 failure."""
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *suite,
        "-q",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(tmp_path / "bt"),
    ]
    proc = subprocess.run(
        argv,
        cwd=str(copy_root / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    match = re.search(r"(\d+) failed", proc.stdout)
    n_failed = int(match.group(1)) if match else 0
    tail = "\n".join(proc.stdout.splitlines()[-15:])
    assert n_failed >= 1, (
        f"{check_name}: the mutant SURVIVED — the suite passed with the "
        f"mutation applied.\n"
        f"suite: {suite}\n"
        f"pytest exit code: {proc.returncode}\n"
        f"--- suite output tail ---\n{tail}"
    )
    return n_failed


# ---------------------------------------------------------------------------
# Production-schema comparison helpers (#744 AC1 / AC2)
# ---------------------------------------------------------------------------


def _ensure_import_paths() -> None:
    """Make repo root and backend/ importable (no-op under pytest from backend/)."""
    for entry in (REPO_ROOT, REPO_ROOT / "backend"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _vault_fk_pairs(conn: sqlite3.Connection) -> set:
    """(referenced table, from-column) pairs of PRAGMA foreign_key_list(vaults)."""
    return {
        (row[2], row[3])
        for row in conn.execute("PRAGMA foreign_key_list(vaults)").fetchall()
    }


def _build_test_and_production_dbs(tmp_path: Path) -> dict:
    """Build the setup_test_db DB and an init_db+run_migrations production DB.

    The env required for importing app.config is set before the imports and
    restored afterwards (the imports of app.models.database /
    backend.tests.user_route_helpers happen inside the window).
    """
    env_updates = {
        "USERS_ENABLED": "false",
        "ADMIN_SECRET_TOKEN": "x" * 48,
        "JWT_SECRET_KEY": "y" * 48,
        "REDIS_URL": "",
        "ALLOW_LOCAL_SERVICES": "1",
    }
    saved = {key: os.environ.get(key) for key in env_updates}
    os.environ.update(env_updates)
    try:
        _ensure_import_paths()
        from backend.tests.user_route_helpers import setup_test_db

        from app.models.database import init_db, run_migrations

        test_conn = setup_test_db(str(tmp_path / "t.db"))
        try:
            test_tables = _table_names(test_conn)
            test_vault_fks = _vault_fk_pairs(test_conn)
        finally:
            test_conn.close()

        prod_path = str(tmp_path / "prod.db")
        init_db(prod_path)
        run_migrations(prod_path)
        prod_conn = sqlite3.connect(prod_path)
        try:
            prod_tables = _table_names(prod_conn)
            prod_vault_fks = _vault_fk_pairs(prod_conn)
        finally:
            prod_conn.close()
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return {
        "test_tables": test_tables,
        "prod_tables": prod_tables,
        "test_vault_fks": test_vault_fks,
        "prod_vault_fks": prod_vault_fks,
    }


def test_route_test_db_has_audit_and_session_tables(tmp_path):
    """#744 AC1: setup_test_db must create the audit and session tables.

    The user/vault route suites run against the schema built by
    setup_test_db (TEST_SCHEMA); when it lacks security_audit_log and
    user_sessions, every users.py audit emission silently no-ops because
    safe_record_security_event swallows the missing-table error.
    """
    dbs = _build_test_and_production_dbs(tmp_path)
    missing = {"security_audit_log", "user_sessions"} - dbs["test_tables"]
    prod_has_both = {"security_audit_log", "user_sessions"} <= dbs["prod_tables"]
    assert len(missing) == 0, (
        f"setup_test_db schema is missing {sorted(missing)} that production "
        f"init_db+run_migrations creates (production has both: "
        f"{prod_has_both}); users.py audit emissions and session revocation "
        f"cannot be observed by any route test"
    )


def test_route_test_db_vaults_fks_match_production(tmp_path):
    """#744 AC2: the test vaults table must declare no FKs production lacks.

    TEST_SCHEMA declares FOREIGN KEY (owner_id) REFERENCES users(id) and
    FOREIGN KEY (org_id) REFERENCES organizations(id) on vaults; the
    production DDL has neither, so vault-create error behavior differs
    between tests and production.
    """
    dbs = _build_test_and_production_dbs(tmp_path)
    extras = dbs["test_vault_fks"] - dbs["prod_vault_fks"]
    assert len(extras) == 0, (
        f"setup_test_db vaults table declares test-only foreign keys "
        f"{sorted(extras)} that the production vaults table does not have; "
        f"vault-create error behavior differs between tests and production"
    )


def test_audit_noop_mutant_is_killed_by_user_route_suite(tmp_path):
    """#744 AC3: safe_record_security_event must not be a silent no-op.

    Inserts an immediate ``return`` after the docstring of
    safe_record_security_event in app/services/security_audit.py; the
    user-route suite must then fail at least one test. At base the mutant
    survives because no test asserts any users.py audit event type (and the
    table is absent from TEST_SCHEMA anyway). Hardening that lands later in
    tests/test_users_routes.py is picked up automatically: the harness runs
    the suite from the copy.
    """
    copy_root = _prepare_copy(tmp_path)
    anchor = '    """Best-effort async wrapper for request handlers."""'
    _mutate(copy_root, "app/services/security_audit.py", anchor, anchor + "\n    return")
    _run_suite(
        copy_root,
        ["tests/test_users_routes.py"],
        tmp_path,
        "safe_record_security_event no-op mutant (issue #744 AC3)",
    )


def test_legacy_memories_upgrade_mutant_is_killed(tmp_path):
    """#744 AC4: run_migrations must be exercised by a legacy upgrade fixture.

    Replaces the migrate_add_memory_retention_columns(sqlite_path) call
    inside run_migrations (app/models/database.py) with ``pass``; the
    schema-unification suite must then fail at least one test. At base the
    mutant survives because the only legacy-upgrade test seeds just
    chat_sessions, and on a fresh init_db DB the columns already exist.
    """
    copy_root = _prepare_copy(tmp_path)
    _mutate(
        copy_root,
        "app/models/database.py",
        "    migrate_add_memory_retention_columns(sqlite_path)",
        "    pass",
    )
    _run_suite(
        copy_root,
        ["tests/test_schema_unification.py"],
        tmp_path,
        "migrate_add_memory_retention_columns skip mutant (issue #744 AC4)",
    )


def test_migrate_embeddings_fixture_has_no_handrolled_files_ddl():
    """#744 AC5: the migrate_embeddings test must build files via the real schema.

    Counts hand-written ``CREATE TABLE ... files`` DDL in
    backend/tests/test_migrate_embeddings_dim.py; the fixture must come from
    init_db/run_migrations so producer/consumer schema drift (partial
    status, phase, partial_embeddings columns) is visible to CI.
    """
    path = REPO_ROOT / "backend" / "tests" / "test_migrate_embeddings_dim.py"
    src = path.read_text(encoding="utf-8")
    n_handrolled = len(
        re.findall(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?files\b", src, re.I)
    )
    assert n_handrolled == 0, (
        f"test_migrate_embeddings_dim.py hand-rolls the files table "
        f"{n_handrolled} time(s) instead of building the fixture with "
        f"init_db/run_migrations; drift between the producer schema and the "
        f"script's consumer view is invisible to CI"
    )


def test_c9_part1_seeds_two_files():
    """#744 AC6: the C9 Part-1 index seed must contain >=2 distinct files.

    AST-parses backend/tests/issue513_checks/test_c9_reindex_dimension_migration.py
    and collects the distinct second positional arguments (the file id) of
    every _record(...) call nested inside a ``store.add_chunks([...])`` call
    whose receiver is the bare Name ``store``. With a single file seeded,
    "replace the whole table" is indistinguishable from a correct per-file
    migration.
    """
    path = (
        REPO_ROOT
        / "backend"
        / "tests"
        / "issue513_checks"
        / "test_c9_reindex_dimension_migration.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    file_ids = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_chunks"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "store"
        ):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.List):
                continue
            for element in arg.elts:
                if (
                    isinstance(element, ast.Call)
                    and isinstance(element.func, ast.Name)
                    and element.func.id == "_record"
                    and len(element.args) >= 2
                ):
                    file_ids.add(ast.unparse(element.args[1]))
    assert len(file_ids) >= 2, (
        f"the C9 Part-1 seed feeds store.add_chunks only "
        f"{len(file_ids)} distinct file id(s) ({sorted(file_ids)}); with a "
        f"single file the check cannot observe a non-migrating file's rows "
        f"surviving the dimension migration"
    )


_LITERAL_VAULT_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+vaults\s*\(\s*id\s*,\s*name\s*\)\s*"
    r"VALUES\s*\(\s*(\d+)\s*,\s*'([^']*)'\s*\)",
    re.I,
)
_PARAM_EXECUTE_RE = re.compile(r"\.execute\(\s*([\"'])(.*?)\1\s*,\s*\(([^)]*)\)", re.S)
_INT_STR_PAIR_RE = re.compile(r"(\d+)\s*,\s*(['\"])(.*?)\2")


def _vault_inserts_in(text):
    """Collect every vault (id, name) literal a text block inserts.

    Matches ``INSERT INTO vaults (id, name) VALUES (<int>, '<name>')`` SQL
    literals, plus integer+string literal pairs passed as parameters to an
    execute() call whose SQL contains an INSERT INTO vaults.
    """
    pairs = []
    for match in _LITERAL_VAULT_INSERT_RE.finditer(text):
        pairs.append((int(match.group(1)), match.group(2)))
    for match in _PARAM_EXECUTE_RE.finditer(text):
        sql = match.group(2)
        if not re.search(r"INSERT\s+INTO\s+vaults", sql, re.I):
            continue
        for pair in _INT_STR_PAIR_RE.finditer(match.group(3)):
            pairs.append((int(pair.group(1)), pair.group(3)))
    return pairs


def test_vault_path_suite_has_name_id_collision_case():
    """#744 AC7: the vault-path suite must cover a name/id collision.

    Collects every vault (id, name) the suite inserts (function scope
    approximated by splitting the source on 'def test_') and counts the test
    functions where one inserted name equals str(id) of ANOTHER vault
    inserted in the same function. Without such a case the suite passes
    while the migration could move or overwrite another vault's storage on
    the collision.
    """
    path = REPO_ROOT / "backend" / "tests" / "test_vault_path_migration.py"
    src = path.read_text(encoding="utf-8")
    function_chunks = src.split("def test_")[1:]
    collision_functions = 0
    for chunk in function_chunks:
        pairs = _vault_inserts_in(chunk)
        has_collision = any(
            name == str(other_id) and other_id != vault_id
            for vault_id, name in pairs
            for other_id, _ in pairs
        )
        if has_collision:
            collision_functions += 1
    assert collision_functions >= 1, (
        f"test_vault_path_migration.py inserts no vault whose name is the "
        f"decimal string of another vault's id (0 of {len(function_chunks)} "
        f"test functions cover the collision); the migration could move or "
        f"overwrite another vault's storage unobserved"
    )


def test_claims_dedup_fixture_includes_wiki_relations():
    """#744 AC8: the claims-dedup fixture must cover the second FK child.

    Imports TestClaimsUniqueDedupRemapsEvidence from
    backend/tests/test_storage_recovery_sqlite.py (by file location) and
    requires the class's fixture to reference wiki_relations — the second FK
    child of wiki_claims whose claim_id ON DELETE CASCADE the dedup must
    remap — unless the fixture is rebuilt from init_db (which creates the
    real wiki_relations table for free).
    """
    _ensure_import_paths()
    module_path = REPO_ROOT / "backend" / "tests" / "test_storage_recovery_sqlite.py"
    spec = importlib.util.spec_from_file_location(
        "test_storage_recovery_sqlite_h07_probe", module_path
    )
    assert spec is not None and spec.loader is not None, (
        f"could not create an import spec for {module_path}"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, "TestClaimsUniqueDedupRemapsEvidence", None)
    assert cls is not None, (
        "TestClaimsUniqueDedupRemapsEvidence not found in "
        "backend/tests/test_storage_recovery_sqlite.py"
    )
    source = inspect.getsource(cls)
    if "init_db" not in source:
        # Only enforce the literal table presence while the fixture still
        # hand-rolls its schema; a fixture rebuilt from init_db gets the
        # real wiki_relations table (and its cascade contract) for free.
        assert source.count("wiki_relations") >= 1, (
            "TestClaimsUniqueDedupRemapsEvidence builds only wiki_claims and "
            "wiki_claim_sources; the real schema's second FK child "
            "(wiki_relations.claim_id ON DELETE CASCADE) is absent from the "
            "fixture, so the dedup's remap of relations is never exercised"
        )
