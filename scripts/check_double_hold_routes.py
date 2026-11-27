#!/usr/bin/env python3
"""Fail when a route handler double-holds pooled SQLite connections (#1199 class).

The #205/#798 double-hold class: a FastAPI route handler declares a
dependency whose chain resolves through the holding ``Depends(get_db)``
generator (a pooled connection held for the whole request) AND its body
performs a standalone pool acquisition — two pooled connections per request.
The fix pattern shipped by PR #1198 for users.py (and now organizations.py,
issue #1199) is the request-scoped ``Depends(get_db)`` connection shared with
the auth chain via FastAPI's per-request dependency cache.

Contracts enforced:

  C-DBLHOLD-1: no route handler in backend/app/api/routes/ may combine a
      HOLDING dependency with a standalone pool acquisition
      (``.get_connection_async()`` / ``.get_connection()`` /
      ``.connection_async()`` / ``.connection()``) in its subtree.

  C-DBLHOLD-2 (fail-closed within the scanned lane): any handler whose
      PARAMETER DEFAULTS carry a bare ``Depends(require_...)`` or
      ``Depends(get_current...)``-shaped call NOT in the classified sets
      (holding or non-holding) is reported as a violation naming the
      handler. KNOWN LIMITATIONS of the scanned lane (deliberate for this
      gate's first rung; widening is NOT tracked anywhere): router-level
      ``APIRouter(dependencies=[...])`` attachments, ``Annotated[..., Depends(...)]``
      parameters, alias-imported names, ``head``/``options``/``trace``/
      ``api_route`` decorators, and pool acquisitions extracted into
      module-level helpers are NOT scanned (a LIVE instance of that lane is
      tracked in #1217) — new auth gates should be
      declared as handler-default ``Depends(...)`` calls with classified
      names, and acquisitions belong inside the handler body. A HOLDING
      dependency whose name carries neither the ``require_`` nor the
      ``get_current_`` prefix (e.g. ``verify_admin``) also escapes both
      rules: C-DBLHOLD-1 needs a classified name, C-DBLHOLD-2 needs one of
      the two prefixes.

  C-DBLHOLD-3: no route handler with a HOLDING dependency may call
      ``.release_connection(...)`` manually — with the request-scoped
      pattern the dependency generator owns release; a manual release is
      either a double-release of the dependency's connection or a leftover
      from a removed standalone checkout.

Classification provenance (verified at issue #1199's base c403ae10 by
reading the dependency sources):
  - HOLDING (resolve through the Depends(get_db) generator or hold it
    directly): require_role, require_admin_role, require_vault_permission,
    require_document_admin, get_current_active_user,
    get_current_user_or_service_account, _optional_current_user
    (backend/app/api/routes/documents.py:97),
    get_evaluate_policy (backend/app/api/deps.py:804), and a direct
    ``Depends(get_db)`` default.
  - NON-HOLDING: require_scope, require_service_account
    (backend/app/security/__init__.py: header-only or
    acquire-and-release-inside-the-dependency), require_model_ready /
    require_ingest_ready (deps.py:199/:228, get_vector_store only),
    require_kms_enabled (kms.py:29, no deps), require_health_probe_auth /
    require_deep_health_probe_auth (deps.py:722/:778 — they acquire and
    release a connection inside the dependency on the user path, so they
    hold nothing across the handler).

Documented exception (independent-lifetime ownership, NOT a migration site):
  - draft_room.py ``upload_draft_input``: its detached commit task
    (``_commit_upload``, draft_room.py:3388-3399,3456) acquires
    ``db_pool.connection_async()`` for commit/audit work whose lifetime must
    SURVIVE request cancellation (compensation semantics), while the request
    awaits the task (draft_room.py:3507-3526). Moving it onto the
    request-scoped connection would change cancellation ownership; it is an
    intentional exception, allowlisted by (file, handler) name.

Exit 0: zero unapproved violations. Exit 1: violations printed (count last,
so a count-only consumer reads the final line).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HOLDING_AUTH = {
    "require_role",
    "require_admin_role",
    "require_vault_permission",
    "require_document_admin",
    "get_current_active_user",
    "get_current_user_or_service_account",
    "_optional_current_user",
    "get_evaluate_policy",
    "get_db",
}
NON_HOLDING_AUTH = {
    "require_scope",
    "require_service_account",
    "require_model_ready",
    "require_ingest_ready",
    "require_kms_enabled",
    "require_health_probe_auth",
    "require_deep_health_probe_auth",
}
ACQUIRE_ATTRS = {
    "get_connection_async",
    "get_connection",
    "connection_async",
    "connection",
}
RELEASE_ATTRS = {"release_connection"}
ROUTER_METHODS = {"get", "post", "put", "patch", "delete"}
# (file name, handler name) -> reason; narrow, documented exceptions only.
EXCEPTIONS = {
    ("draft_room.py", "upload_draft_input"): (
        "detached commit task owns an independent-lifetime connection for "
        "cancellation/compensation semantics (see module docstring)"
    ),
}


def _dep_name(node: ast.AST) -> str | None:
    """Inner function name of a Depends(...) call, else None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "Depends"):
        return None
    arg = node.args[0] if node.args else None
    if isinstance(arg, ast.Call):
        inner = arg.func
        if isinstance(inner, ast.Name):
            return inner.id
        if isinstance(inner, ast.Attribute):
            return inner.attr
    if isinstance(arg, ast.Name):
        return arg.id
    if isinstance(arg, ast.Attribute):
        return arg.attr
    return None


def _is_unclassified_auth(name: str) -> bool:
    return name not in HOLDING_AUTH and name not in NON_HOLDING_AUTH and (
        name.startswith("require_") or name.startswith("get_current")
    )


def _calls_with_attrs(node: ast.AST, attrs: set[str]) -> list[tuple[int, str]]:
    return [
        (c.lineno, c.func.attr)
        for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr in attrs
    ]


def scan_source(file_name: str, source: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source, filename=file_name)
    for fn in tree.body:
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        decorated = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in ROUTER_METHODS
            for d in fn.decorator_list
        ) or any(
            isinstance(d, ast.Attribute) and d.attr in ROUTER_METHODS
            for d in fn.decorator_list
        )
        if not decorated:
            continue
        auth_deps = [
            name
            for default in list(fn.args.defaults)
            + [d for d in fn.args.kw_defaults if d]
            if (name := _dep_name(default))
        ]
        unknown = sorted(
            {name for name in auth_deps if _is_unclassified_auth(name)}
        )
        if unknown:
            violations.append(
                f"C-DBLHOLD-2 {file_name}:{fn.lineno} {fn.name}: unclassified"
                f" auth dependency {unknown} — classify it in this script's"
                " holding/non-holding sets before it can hold silently"
            )
        holding = sorted(set(auth_deps) & HOLDING_AUTH)
        acquisitions = _calls_with_attrs(fn, ACQUIRE_ATTRS)
        releases = _calls_with_attrs(fn, RELEASE_ATTRS)
        if (file_name, fn.name) in EXCEPTIONS and holding:
            # Narrow exception: exempt ONLY connection_async acquisitions
            # inside the nested _commit_upload task (whose connection
            # lifetime must survive request cancellation). Every other
            # acquisition form — even inside _commit_upload — and every
            # acquisition elsewhere in the handler, and every manual
            # release anywhere in it, is still evaluated normally.
            exempt_ranges = [
                (nf.lineno, nf.end_lineno or nf.lineno)
                for nf in ast.walk(fn)
                if isinstance(nf, (ast.FunctionDef, ast.AsyncFunctionDef))
                and nf is not fn
                and nf.name == "_commit_upload"
            ]
            acquisitions = [
                (ln, attr)
                for ln, attr in acquisitions
                if not (
                    attr == "connection_async"
                    and any(lo <= ln <= hi for lo, hi in exempt_ranges)
                )
            ]
        acquisitions = [ln for ln, _ in acquisitions]
        releases = [ln for ln, _ in releases]
        if holding and acquisitions:
            violations.append(
                f"C-DBLHOLD-1 {file_name}:{fn.lineno} {fn.name}: holding"
                f" dependency {holding} plus standalone pool acquisition at"
                f" lines {acquisitions} — migrate to the request-scoped"
                " Depends(get_db) connection (#798/#1199 pattern)"
            )
        if holding and releases:
            violations.append(
                f"C-DBLHOLD-3 {file_name}:{fn.lineno} {fn.name}: holding"
                f" dependency {holding} plus manual release_connection at"
                f" lines {releases} — the Depends(get_db) generator owns"
                " release; a manual release double-releases or is a leftover"
                " from a removed standalone checkout"
            )
    return violations


def scan_file(path: Path) -> list[str]:
    return scan_source(path.name, path.read_text(encoding="utf-8"))


def main() -> int:
    routes_dir = (
        Path(__file__).resolve().parents[1] / "backend" / "app" / "api" / "routes"
    )
    scanned = sorted(routes_dir.glob("*.py"))
    if not scanned:
        # Fail closed: a relocated or empty routes directory must not look
        # like a clean scan (the gate's whole value is that "0 findings"
        # means "scanned and clean", not "scanned nothing").
        print(
            "check_double_hold_routes: FAIL - no route modules found at "
            f"{routes_dir}; refusing to report a clean scan",
            file=sys.stderr,
        )
        return 2
    violations: list[str] = []
    for path in scanned:
        violations.extend(scan_file(path))
    for line in violations:
        print(line)
    print(len(violations))
    if violations:
        print(
            "check_double_hold_routes: FAIL — double-hold or unclassified"
            " auth-dependency handlers found (above)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
