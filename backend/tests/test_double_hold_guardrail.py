"""Synthetic contract tests for scripts/check_double_hold_routes.py.

The guardrail is the #1199 defect-class gate (C-DBLHOLD-1/2/3). Running it
against the live routes tree proves only that today's known shape is
recognized; these tests pin its CONTRACTS on synthetic sources so each rule
and classification is proven to bite independently of the live tree:

- unknown require_*/get_current*-prefixed handler-default dependency fails closed (C-DBLHOLD-2 scanned lane)
- every acquisition form is detected under a holding dependency
  (get_connection_async, get_connection, connection_async, connection)
- a direct Depends(get_db) default is itself a holding dependency
- non-holding gates (require_scope, require_model_ready, ...) pass with
  their own acquisition-free bodies
- the migrated request-scoped shape passes cleanly
- manual release under a holding dependency trips C-DBLHOLD-3 even with no
  acquisition
- the documented draft_room upload_draft_input exception is honored by
  (file, handler) name and is NARROW (a different handler in the same file
  is not exempt)

RED for these contracts wherever the corresponding rule is missing from the
script; GREEN with the script as shipped.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_double_hold_routes.py"
_spec = importlib.util.spec_from_file_location("check_double_hold_routes", _SCRIPT)
guard = importlib.util.module_from_spec(_spec)
sys.modules["check_double_hold_routes"] = guard
_spec.loader.exec_module(guard)


def _route(handler: str, defaults: str, body: str) -> str:
    return (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        f"@router.get('/x')\n"
        f"async def {handler}({defaults}):\n"
        f"    {body}\n"
    )


def test_unknown_auth_dependency_fails_closed():
    src = _route(
        "h",
        "_a=Depends(require_brand_new_gate)",
        "return 1",
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-2" in v and "require_brand_new_gate" in v for v in violations), violations


def test_every_acquisition_form_detected_under_holding_dep():
    for body in (
        "conn = await pool.get_connection_async()",
        "conn = pool.get_connection()",
        "async with db_pool.connection_async() as conn:\n        pass",
        "with db_pool.connection() as conn:\n        pass",
    ):
        src = _route(
            "h",
            "user=Depends(require_role('admin'))",
            body,
        )
        violations = guard.scan_source("synthetic.py", src)
        assert any("C-DBLHOLD-1" in v for v in violations), (body, violations)


def test_direct_get_db_default_is_holding():
    src = _route(
        "h",
        "conn=Depends(get_db)",
        "async with db_pool.connection():\n        pass",
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_get_evaluate_policy_is_holding():
    src = _route(
        "h",
        "evaluate=Depends(get_evaluate_policy)",
        "conn = await pool.get_connection_async()",
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_non_holding_gates_pass_with_clean_body():
    for gate in ("require_scope", "require_service_account", "require_model_ready",
                 "require_ingest_ready", "require_kms_enabled"):
        src = _route("h", f"_a=Depends({gate}('x'))" if gate != "require_model_ready"
                     else "_a=Depends(require_model_ready)", "return 1")
        assert guard.scan_source("synthetic.py", src) == [], gate


def test_migrated_request_scoped_shape_passes():
    src = _route(
        "h",
        "user=Depends(require_role('admin')), db=Depends(get_db)",
        "conn = db\n    return 1",
    )
    assert guard.scan_source("synthetic.py", src) == []


def test_manual_release_under_holding_dep_trips_release_rule():
    src = _route(
        "h",
        "user=Depends(require_role('admin')), db=Depends(get_db)",
        "conn = db\n    pool.release_connection(conn)\n    return 1",
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-3" in v for v in violations), violations
    assert not any("C-DBLHOLD-1" in v for v in violations), violations


def test_draft_room_exception_is_narrow_and_exact():
    # Approved shape: connection_async INSIDE the nested _commit_upload task.
    excepted = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    return 1\n"
    )
    assert guard.scan_source("draft_room.py", excepted) == []
    sibling = _route(
        "some_other_handler",
        "user=Depends(require_role('admin'))",
        "async with db_pool.connection_async():\n        pass",
    )
    assert any("C-DBLHOLD-1" in v for v in guard.scan_source("draft_room.py", sibling))
    other_file = _route(
        "upload_draft_input",
        "user=Depends(require_role('admin'))",
        "async with db_pool.connection_async():\n        pass",
    )
    assert any("C-DBLHOLD-1" in v for v in guard.scan_source("other.py", other_file))


def test_draft_exception_does_not_hide_other_acquisitions():
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_draft_exception_rejects_disallowed_form_inside_commit_task():
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_draft_exception_still_fires_manual_release():
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    pool.release_connection(conn)\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-3" in v for v in violations), violations


def test_get_current_prefixed_unclassified_dependency_fails_closed():
    """The fail-closed lane covers BOTH name prefixes.

    Regression pin: deleting the ``get_current`` half of
    ``_is_unclassified_auth`` must not survive this suite (the previous
    suite only exercised a ``require_``-prefixed unknown name, so that half
    of the rule was unpinned).
    """
    src = _route(
        "h",
        "_a=Depends(get_current_brand_new_gate)",
        "return 1",
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-2" in v for v in violations), violations


def test_classified_name_sets_are_pinned():
    """Every classified dependency name is pinned by this suite.

    Regression pin: removing a name from HOLDING_AUTH or NON_HOLDING_AUTH
    (e.g. ``get_current_active_user`` — the auth-chain gate used across the
    users routes) silently disables C-DBLHOLD-1/3 for routes using it and
    survives the behavioural tests, because the fail-closed rule only
    catches NEW unclassified names. Pin the SETS themselves.
    """
    assert guard.HOLDING_AUTH == {
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
    assert guard.NON_HOLDING_AUTH == {
        "require_scope",
        "require_service_account",
        "require_model_ready",
        "require_ingest_ready",
        "require_kms_enabled",
        "require_health_probe_auth",
        "require_deep_health_probe_auth",
    }


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_every_router_method_is_scanned(method):
    """All five router methods must be scanned, not just get/post.

    Regression pin: dropping put/patch/delete from ROUTER_METHODS would
    silently unscanned 54 of the repo's 262 routes (and 6 of the 18
    organizations handlers) while every other test stayed green.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        f"@router.{method}('/x')\n"
        "async def h(user=Depends(require_role('admin'))):\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), (method, violations)


def test_draft_room_acquisition_outside_commit_upload_still_fires():
    """Only acquisitions INSIDE _commit_upload are exempt.

    Regression pin: dropping the ``nf.name == "_commit_upload"`` check (or
    the line-range narrowing) would exempt every nested function in the
    handler, letting a new acquisition inside the handler body pass.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    async with db_pool.connection_async() as conn:\n"
        "        pass\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_keyword_only_dependency_is_scanned():
    """Keyword-only dependency defaults are scanned too.

    Regression pin: ignoring ``kw_defaults`` would unscanned every
    keyword-only auth dependency.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/x')\n"
        "async def h(*, user=Depends(require_role('admin'))):\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_attribute_form_dependency_is_scanned():
    """``Depends(module.callable)`` is resolved to the callable's name.

    Regression pin: ignoring the attribute form would leave
    ``Depends(deps.get_db)``-style dependencies unscanned.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "import app.api.deps as deps\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/x')\n"
        "async def h(user=Depends(deps.get_db)):\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_second_nested_function_acquisition_still_fires():
    """A SECOND nested function's acquisition must not be exempt.

    Regression pin: dropping the ``_commit_upload`` name check (or loosening
    it to any underscore-prefixed name) made every nested function exempt,
    while a stray acquisition placed directly in the handler body did not
    detect it. The stray acquisition must live in a second nested function.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    async def _compensate():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_call_attribute_depends_form_is_scanned():
    """``Depends(module.callable(...))`` resolves to the callable's name.

    Regression pin: dropping the call-attribute branch of the dependency
    resolver left ``Depends(deps.require_role('admin'))`` unscanned.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "import app.api.deps as deps\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/x')\n"
        "async def h(user=Depends(deps.require_role('admin'))):\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_draft_room_exception_is_file_scoped():
    """The exception is keyed by (file, handler) — not by handler alone.

    Regression pin: keying the exception by handler name only would exempt
    an ``upload_draft_input`` in a DIFFERENT file, where the nested
    ``_commit_upload`` exemption must not apply.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    return 1\n"
    )
    violations = guard.scan_source("other.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_draft_room_exception_matches_the_exact_function_name():
    """Only the EXACT ``_commit_upload`` nested function is exempt.

    Regression pin: loosening the name check to a prefix match
    (``startswith("_commit")``) would silently exempt a sibling nested
    function such as ``_commit_audit`` in the same handler.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/x')\n"
        "async def upload_draft_input(user=Depends(require_role('admin'))):\n"
        "    async def _commit_upload():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    async def _commit_audit():\n"
        "        async with db_pool.connection_async() as conn:\n"
        "            pass\n"
        "    return 1\n"
    )
    violations = guard.scan_source("draft_room.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations


def test_bare_attribute_decorator_form_is_scanned():
    """The scanner accepts the bare-attribute decorator form too.

    Regression pin: the bare-attribute branch of the decorator check had no
    test, so it could be deleted without any test failing.
    """
    src = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get\n"
        "async def h(user=Depends(require_role('admin'))):\n"
        "    conn = await pool.get_connection_async()\n"
        "    return 1\n"
    )
    violations = guard.scan_source("synthetic.py", src)
    assert any("C-DBLHOLD-1" in v for v in violations), violations
