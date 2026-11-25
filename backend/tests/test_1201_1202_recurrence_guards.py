"""Recurrence guardrails for the collated #1201 + #1202 fixes (issue-tracer Phase 4.2).

Static censuses over ``backend/app``. Each one is red on the pre-fix tree for
the defect it names and green after the fix.

1. Owner-or-admin on chat-session mutation (root cause RC-A). Every route
   that mutates a session under ``/chat/sessions/{session_id}`` is classified
   in ``OWNER_RULE_CLASS``. ``ENFORCED`` routes must call
   ``_require_session_owner_or_admin``; ``OWN_CHECK`` routes must keep their
   own owner-or-admin check; ``DECISION_RECORDED`` routes are deliberately not
   enforced because the named maintainer decision is recorded - the entry states
   what ships, never that it is right. A route that is missing from the table,
   a table entry whose route is gone, and a decided route that starts calling
   the helper all fail. Before the fix the item routes selected
   ``id, title, vault_id`` and authorised against the vault only, so any vault
   writer could rewrite or delete another member's session.

2. IntegrityError discrimination (root cause RC-C). An
   ``except ... IntegrityError`` handler that adopts a winner by re-selecting,
   or that neither raises nor returns (it lets the caller continue as if the
   write succeeded), must bind the exception and read it. Before the fix both
   quality-report handlers re-selected a winner that a foreign-key failure
   cannot have produced and surfaced as HTTP 500, and the durable-turn pre-write
   reported a foreign-key failure as a successful duplicate.

3. Off-loop dispatch shape (root cause RC-D/S1). ``asyncio.to_thread(f(x))``
   runs ``f`` on the event loop before dispatching anything; a sanitizer-bearing
   function must be handed to ``to_thread`` as a function object, and the call
   must be awaited.
"""

import ast
import functools
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"

OWNER_HELPER = "_require_session_owner_or_admin"
SESSION_PREFIX = "/chat/sessions/{session_id}"
_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete"})
_MUTATING_VERBS = frozenset({"post", "put", "patch", "delete"})

ENFORCED = "ENFORCED"
OWN_CHECK = "OWN_CHECK"
DECISION_RECORDED = "DECISION_RECORDED"

# (dotted module, handler) -> its relation to the owner rule.
OWNER_RULE_CLASS = {
    ("app.api.routes.chat", "add_message"): ENFORCED,
    ("app.api.routes.chat", "add_messages_batch"): ENFORCED,
    ("app.api.routes.chat", "truncate_session_messages"): ENFORCED,
    ("app.api.routes.chat", "update_session"): ENFORCED,
    ("app.api.routes.chat", "delete_session"): ENFORCED,
    ("app.api.routes.chat", "set_message_feedback"): OWN_CHECK,
    ("app.api.routes.chat", "fork_session"): DECISION_RECORDED,
    ("app.api.routes.canvas", "create_session_artifact"): DECISION_RECORDED,
}

# Every DECISION_RECORDED route names the maintainer decision that keeps it out of scope.
DECISION_RECORDED_BECAUSE = {
    ("app.api.routes.chat", "fork_session"): "#1201 ASK-2 (fork scope)",
    ("app.api.routes.canvas", "create_session_artifact"): "#1201 ASK-5 (canvas matrix)",
}


@functools.lru_cache(maxsize=None)
def _trees():
    """(dotted module, parsed tree) for every module under ``backend/app``."""
    parsed = []
    for source in sorted(_APP.rglob("*.py")):
        module = ".".join(source.relative_to(_BACKEND).with_suffix("").parts)
        parsed.append((module, ast.parse(source.read_text(encoding="utf-8"))))
    return tuple(parsed)


def _module_text(module):
    return (
        (_BACKEND / Path(*module.split(".")))
        .with_suffix(".py")
        .read_text(encoding="utf-8")
    )


def _own_nodes(func):
    """Every descendant of ``func`` except the interior of a nested def/lambda."""
    nested = set()
    for child in ast.walk(func):
        if child is not func and isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            for desc in ast.walk(child):
                nested.add(id(desc))
    return [node for node in ast.walk(func) if id(node) not in nested]


def _callee_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(func, name):
    return any(
        isinstance(node, ast.Call) and _callee_name(node) == name
        for node in _own_nodes(func)
    )


def _router_routes():
    """(module, verb, url path, handler node) for every router-decorated handler."""
    for module, tree in _trees():
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in _HTTP_VERBS
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    yield module, dec.func.attr, dec.args[0].value, node


def _is_session_item_route(verb, url):
    return verb in _MUTATING_VERBS and (
        url == SESSION_PREFIX or url.startswith(SESSION_PREFIX + "/")
    )


def _module_function(module, name):
    """The top-level ``def``/``async def`` called ``name`` in ``module``, or None."""
    for tree_module, tree in _trees():
        if tree_module == module:
            for node in tree.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name
                ):
                    return node
    return None


def _is_select_statement(node):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return True  # non-literal SQL: assume a lookup
    return node.value.lstrip().upper().startswith("SELECT")


def _selects_after_failure(handler):
    """The handler body runs a SELECT through ``execute`` (called directly or
    handed to ``asyncio.to_thread``)."""
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr == "execute" and node.args:
                if _is_select_statement(node.args[0]):
                    return True
            elif (
                node.func.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Attribute)
                and node.args[0].attr == "execute"
            ):
                if len(node.args) < 2 or _is_select_statement(node.args[1]):
                    return True
    return False


def _binds_and_reads(handler):
    """The exception is bound (``except ... as exc``) and that name is used."""
    if handler.name is None:
        return False
    return any(
        isinstance(node, ast.Name)
        and node.id == handler.name
        and isinstance(node.ctx, ast.Load)
        for stmt in handler.body
        for node in ast.walk(stmt)
    )


def _continues_as_success(handler):
    """The handler neither raises nor returns: control falls through as if the
    guarded write had succeeded."""
    return not any(
        isinstance(node, (ast.Raise, ast.Return))
        for stmt in handler.body
        for node in ast.walk(stmt)
    )


def _integrity_handlers():
    """(module, enclosing function, handler line, handler node) for every
    ``except ... IntegrityError`` handler under ``backend/app``."""

    def names_integrity(exc_type):
        if isinstance(exc_type, ast.Name):
            return exc_type.id == "IntegrityError"
        if isinstance(exc_type, ast.Attribute):
            return exc_type.attr == "IntegrityError"
        if isinstance(exc_type, ast.Tuple):
            return any(names_integrity(elt) for elt in exc_type.elts)
        return False

    for module, tree in _trees():
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ExceptHandler) and names_integrity(node.type)):
                continue
            parent = parents.get(id(node))
            while parent is not None and not isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                parent = parents.get(id(parent))
            yield module, parent.name if parent else "<module>", node.lineno, node


def _sanitizer_bearing_sync_helpers(tree):
    """(sanitizer names imported by the module, sync functions that reach a
    sanitizer call directly or through other sync functions of the module)."""
    names = {"sanitize_assistant_content"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.endswith("assistant_sanitizer")
        ):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    sync_calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            sync_calls.setdefault(node.name, set()).update(
                _callee_name(c) for c in _own_nodes(node) if isinstance(c, ast.Call)
            )
    helpers = set()
    changed = True
    while changed:
        changed = False
        for fn, callees in sync_calls.items():
            if fn not in helpers and (callees & names or callees & helpers):
                helpers.add(fn)
                changed = True
    return names, helpers


class SessionMutationOwnerRuleTest(unittest.TestCase):
    def test_every_mutating_session_route_is_classified(self):
        found = {
            (module, handler.name)
            for module, verb, url, handler in _router_routes()
            if _is_session_item_route(verb, url)
        }
        self.assertEqual(
            sorted(set(OWNER_RULE_CLASS) - found),
            [],
            "OWNER_RULE_CLASS names a missing route",
        )
        self.assertEqual(
            sorted(found - set(OWNER_RULE_CLASS)),
            [],
            "a mutating chat-session route is not classified in OWNER_RULE_CLASS",
        )

    def test_enforced_routes_call_the_owner_helper(self):
        missing = [
            f"{module}:{handler.lineno} {handler.name}"
            for module, verb, url, handler in _router_routes()
            if _is_session_item_route(verb, url)
            and OWNER_RULE_CLASS.get((module, handler.name)) == ENFORCED
            and not _calls(handler, OWNER_HELPER)
        ]
        boundary = _module_function("app.api.routes.chat", "get_stream_auth")
        self.assertIsNotNone(boundary, "get_stream_auth was removed or renamed")
        if not _calls(boundary, OWNER_HELPER):
            missing.append("get_stream_auth")
        self.assertEqual(missing, [], "owner-or-admin check is not wired")

    def test_own_check_route_keeps_its_own_check(self):
        for (module, name), kind in OWNER_RULE_CLASS.items():
            if kind != OWN_CHECK:
                continue
            func = _module_function(module, name)
            self.assertIsNotNone(func, f"{module}.{name} was removed or renamed")
            text = ast.get_source_segment(_module_text(module), func) or ""
            self.assertIn(
                'user["id"]', text, f"{name} no longer compares the session owner"
            )
            self.assertIn("superadmin", text, f"{name} no longer exempts admin roles")

    def test_decided_routes_name_their_decision(self):
        for (module, name), kind in OWNER_RULE_CLASS.items():
            if kind != DECISION_RECORDED:
                continue
            self.assertIn(
                (module, name),
                DECISION_RECORDED_BECAUSE,
                f"{name} is DECISION_RECORDED without naming the recorded decision",
            )
            func = _module_function(module, name)
            self.assertFalse(
                func is not None and _calls(func, OWNER_HELPER),
                f"{name} now enforces the owner rule: reclassify it as ENFORCED",
            )


class IntegrityErrorDiscriminationTest(unittest.TestCase):
    def test_handlers_do_not_assume_the_unique_race(self):
        violating = []
        for module, function, lineno, handler in _integrity_handlers():
            if _binds_and_reads(handler):
                continue
            if _selects_after_failure(handler):
                violating.append(
                    f"{module}:{lineno} in {function}: re-selects a winner unbound"
                )
            elif _continues_as_success(handler):
                violating.append(
                    f"{module}:{lineno} in {function}: continues as success unbound"
                )
        self.assertEqual(
            violating,
            [],
            "IntegrityError handler assumes the unique-index race without discriminating",
        )

    def test_census_sees_the_report_handlers(self):
        reseeding = {
            (module, function)
            for module, function, _, handler in _integrity_handlers()
            if _selects_after_failure(handler)
        }
        for known in (
            ("app.api.routes.quality_reports", "submit_quality_report"),
            ("app.api.routes.quality_reports", "convert_report_to_eval_case"),
        ):
            self.assertIn(known, reseeding)


class OffLoopDispatchShapeTest(unittest.TestCase):
    def test_sanitizer_is_handed_to_to_thread_not_its_result(self):
        tree = dict(_trees())["app.api.routes.chat"]
        names, helpers = _sanitizer_bearing_sync_helpers(tree)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        eager, unawaited = [], []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee_name(node) == "to_thread"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Call) and _callee_name(first) in names | helpers:
                eager.append(
                    f"line {node.lineno}: to_thread({_callee_name(first)}(...))"
                )
            elif isinstance(first, ast.Name) and first.id in names | helpers:
                # ``await asyncio.to_thread(...)`` and ``x = await asyncio.to_thread(...)``
                # both put the Await node directly around the call.
                parent = parents.get(id(node))
                if not isinstance(parent, ast.Await):
                    unawaited.append(
                        f"line {node.lineno}: to_thread({first.id}, ...) is not awaited"
                    )
        self.assertEqual(
            eager, [], "sanitizer runs on the loop before to_thread dispatches"
        )
        self.assertEqual(unawaited, [], "off-loop sanitizer dispatch is not awaited")


if __name__ == "__main__":
    unittest.main()
