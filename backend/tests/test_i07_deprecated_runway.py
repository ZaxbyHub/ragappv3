"""Frozen issue-tracer checks for issue #754 (deprecated runway).

Acceptance checks AC1-AC3, AC5 and AC6 of the #754 remediation plan:

- AC1: no backend application code may call the deprecated
  ``AsyncConnection.table_names()`` (superseded by ``list_tables()`` in
  the pinned lancedb; a future release in the unpinned ``<1.0`` range
  removes the method outright).
- AC2: a real ``VectorStore`` initialising its table and counting a
  file's rows against the real lancedb must emit no ``table_names``
  DeprecationWarning — behavioural proof that the replacement keeps the
  table-existence check working.
- AC3: pytest ``filterwarnings`` must escalate ``DeprecationWarning``
  from ``lancedb`` to an error, so a host deprecation on a pinned
  library fails CI before a version bump ships it silently.
- AC5: ``scripts/validate_vite_env.mjs`` must have a real caller (a
  frontend/package.json script, a workflow, or a Dockerfile RUN/COPY
  that uses it) or be deleted — the Dockerfiles carry inlined copies,
  so the standalone script is unwired drift.
- AC6: ``scripts/migrate_memories.py --retention`` must actually be read
  inside ``migrate()`` or the option must be removed.

AC4 is NON-EXECUTABLE (product decision: recording the Python 3.12 move
as a tracked maintainer decision). AC7 is PRESERVING and stays pinned by
the existing suites (``tests/test_vector_store_async.py``,
``tests/test_fts_index_detection_integration.py`` and
``tests/test_issue258_build_contracts.py::test_ac9_runtime_contract_script_passes``)
— not re-implemented here.

All five checks are RED at the recorded base tree and turn GREEN when
the #754 fix lands. Only AC2 imports application code; the import is
deferred into the test so the structural checks stay file/AST reads.
"""

import ast
import json
import re
import tomllib
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
APP_DIR = BACKEND / "app"
PYPROJECT = BACKEND / "pyproject.toml"
VALIDATE_VITE_ENV = REPO_ROOT / "scripts" / "validate_vite_env.mjs"
MIGRATE_MEMORIES = REPO_ROOT / "scripts" / "migrate_memories.py"


# ── AC1: no deprecated table_names() calls under backend/app ────────────────


def test_no_table_names_calls_in_app():
    offenders = []
    for python_file in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(python_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "table_names"
            ):
                offenders.append(
                    f"{python_file.relative_to(REPO_ROOT)}:{node.lineno}"
                )
    assert len(offenders) == 0, (
        "backend/app must not call the deprecated "
        f"AsyncConnection.table_names() (use list_tables()): {offenders}"
    )


# ── AC2: real VectorStore emits no table_names DeprecationWarning ───────────


@pytest.mark.asyncio
async def test_vector_store_emits_no_table_names_deprecation(tmp_path):
    # Deferred import: the structural checks in this module must not depend
    # on app modules (only this behavioural node touches application code).
    from app.services.vector_store import VectorStore

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = VectorStore(db_path=tmp_path / "lancedb")
        await store.init_table(4)
        await store.count_by_file("1")

    deprecations = [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
        and "table_names" in str(warning.message)
    ]
    assert len(deprecations) == 0, (
        "VectorStore table init/count must not emit a table_names() "
        "DeprecationWarning against the real lancedb "
        f"({[str(w.message) for w in deprecations]})"
    )


# ── AC3: pytest escalates lancedb DeprecationWarnings ───────────────────────


def test_pytest_escalates_lancedb_deprecations():
    with PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    entries = (
        pyproject.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("filterwarnings", [])
    )
    escalated = [
        entry
        for entry in entries
        if entry.startswith("error::DeprecationWarning") and "lancedb" in entry
    ]
    assert len(escalated) >= 1, (
        "backend/pyproject.toml filterwarnings must escalate lancedb "
        "DeprecationWarnings to errors (an 'error::DeprecationWarning:"
        "lancedb' entry); a host deprecation on a pinned library must fail "
        f"CI, not scroll past (current entries: {entries})"
    )


# ── AC5: validate_vite_env.mjs is wired or removed ──────────────────────────


def test_validate_vite_env_is_wired_or_removed():
    ok = 0 if VALIDATE_VITE_ENV.exists() else 1

    if ok == 0:
        # A frontend/package.json script that invokes it.
        package_json = json.loads(
            (REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        )
        if "validate_vite_env" in json.dumps(package_json.get("scripts", {})):
            ok = 1

    if ok == 0:
        # Any workflow reference.
        for workflow in sorted(
            (REPO_ROOT / ".github" / "workflows").glob("*.yml")
        ):
            if "validate_vite_env" in workflow.read_text(encoding="utf-8"):
                ok = 1
                break

    if ok == 0:
        # A non-comment RUN/COPY line in either Dockerfile. (The current
        # Dockerfile mentions are comments plus heredoc JS console.log
        # strings inside the inlined copies — not a caller.)
        for dockerfile in (
            REPO_ROOT / "Dockerfile",
            REPO_ROOT / "frontend" / "Dockerfile",
        ):
            for line in dockerfile.read_text(encoding="utf-8").splitlines():
                stripped = line.lstrip()
                if (
                    stripped.startswith(("RUN", "COPY"))
                    and "validate_vite_env" in line
                ):
                    ok = 1
                    break
            if ok == 1:
                break

    assert ok == 1, (
        "scripts/validate_vite_env.mjs has no caller (no frontend/package."
        "json script, no workflow, no Dockerfile RUN/COPY uses it — both "
        "Dockerfiles inline their own copies); delete it or wire it as the "
        "real caller"
    )


# ── AC6: migrate_memories --retention is used or removed ────────────────────


def test_migrate_memories_retention_is_used_or_removed():
    tree = ast.parse(MIGRATE_MEMORIES.read_text(encoding="utf-8"))

    retention_registered = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_argument":
            for argument in node.args:
                if (
                    isinstance(argument, ast.Constant)
                    and argument.value == "--retention"
                ):
                    retention_registered = True

    ok = 0 if retention_registered else 1
    if retention_registered:
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "migrate"
            ):
                loads = [
                    inner
                    for inner in ast.walk(node)
                    if isinstance(inner, ast.Name)
                    and inner.id == "retention"
                    and isinstance(inner.ctx, ast.Load)
                ]
                if loads:
                    ok = 1
    assert ok == 1, (
        "scripts/migrate_memories.py registers --retention but migrate() "
        "never reads it (the docstring says 'not yet implemented'); use the "
        "value in migrate() or remove the option"
    )
