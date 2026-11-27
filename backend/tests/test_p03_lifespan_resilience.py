"""P03 lifecycle-resilience acceptance checks (issue #799 AC1/AC2/AC3/AC7/AC8).

Five independent checks pinning the lifespan/startup resilience criteria from
the #799 lifecycle-drain trace (repo base 50a85b70):

- AC1: ``_prime_editorial`` must construct the editorial client INSIDE its
  ``try`` so a construction failure is contained by the task's own handling
  instead of raising out of the shutdown await (ast check).
- AC2: the shutdown loop awaiting each residency task (``await rt``) must
  handle any ``Exception``, not only ``asyncio.CancelledError`` — a task that
  died naturally re-raises its exception at the await, which currently skips
  every later shutdown stage (ast check).
- AC3: ``_create_llm_clients()`` must degrade the thinking client to ``None``
  when its constructor raises ``URLBlocked`` (live-DNS failure) instead of
  aborting startup (behavioral check with patched factories).
- AC7: lifespan.py must not point operators at a remediation method that does
  not exist on ``VectorStore`` (``_ensure_fts_index``) (source check).
- AC8: main.py must not carry the three stale unreachable startup-warning
  texts left over from before ``Settings.reject_insecure_defaults`` (source
  check).

All five are RED at base, GREEN after the fix.
"""

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
LIFESPAN_PATH = BACKEND / "app" / "lifespan.py"
MAIN_PATH = BACKEND / "app" / "main.py"


def _lifespan_source() -> str:
    return LIFESPAN_PATH.read_text(encoding="utf-8")


def _lifespan_tree() -> ast.Module:
    return ast.parse(_lifespan_source(), filename=str(LIFESPAN_PATH))


def _callee_name(call: ast.Call) -> str:
    """Best-effort name of a call target: ``foo()`` / ``mod.foo()``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _try_body_node_ids(scope) -> set[int]:
    """ids of every node sitting inside ANY ``try:`` body within ``scope``.

    ``except``/``else``/``finally`` arms are deliberately NOT collected: the
    criterion is that the call be protected by the try body itself.
    """
    protected: set[int] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for child in ast.walk(stmt):
                    protected.add(id(child))
    return protected


def _exception_type_names(handler_type) -> set[str]:
    """Names an except-clause type expression mentions (``Exception``,
    ``asyncio.CancelledError`` → ``{"CancelledError"}``, tuples recursed)."""
    names: set[str] = set()
    if handler_type is None:
        return names
    if isinstance(handler_type, ast.Name):
        names.add(handler_type.id)
    elif isinstance(handler_type, ast.Attribute):
        names.add(handler_type.attr)
    elif isinstance(handler_type, ast.Tuple):
        for element in handler_type.elts:
            names |= _exception_type_names(element)
    return names


class TestPrimeEditorialConstructionInsideTry(unittest.TestCase):
    """AC1: editorial client construction must be contained by the try."""

    def test_create_editorial_client_call_sits_inside_try(self):
        func = _find_function(_lifespan_tree(), "_prime_editorial")
        self.assertIsNotNone(
            func, "lifespan.py must define _prime_editorial (editorial prime task)"
        )
        protected = _try_body_node_ids(func)
        unprotected: list[int] = []
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and _callee_name(node) == (
                "create_editorial_client"
            ):
                if id(node) not in protected:
                    unprotected.append(node.lineno)
        self.assertEqual(
            unprotected,
            [],
            "AC1: every create_editorial_client() call in _prime_editorial must "
            "sit inside a try body; unprotected call(s) at line(s) "
            f"{unprotected}. A construction failure there raises out of the "
            "residency task and re-raises at the shutdown await, skipping "
            "file_watcher stop, all processor stop()s, admission shutdown, "
            "vector-store close and pool close.",
        )


class TestResidencyAwaitHandlesAnyException(unittest.TestCase):
    """AC2: the shutdown await of each residency task must absorb Exceptions."""

    def test_await_rt_try_handles_exception(self):
        tree = _lifespan_tree()
        candidate_tries: list[ast.Try] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for stmt in node.body:
                has_await_rt = any(
                    isinstance(child, ast.Await)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "rt"
                    for child in ast.walk(stmt)
                )
                if has_await_rt:
                    candidate_tries.append(node)
                    break
        self.assertTrue(
            candidate_tries,
            "could not locate the shutdown try around `await rt` (the loop "
            "awaiting residency_tasks after rt.cancel()) — the AC2 anchor is "
            "missing entirely, which cannot satisfy the criterion",
        )
        handler_names: set[str] = set()
        for try_node in candidate_tries:
            for handler in try_node.handlers:
                handler_names |= _exception_type_names(handler.type)
        self.assertIn(
            "Exception",
            handler_names,
            "AC2: the try around `await rt` handles only "
            f"{sorted(handler_names)}; a residency task that DIED with any "
            "other exception re-raises at this await and aborts the whole "
            "shutdown sequence. The handler set must include Exception.",
        )


class TestCreateLlmClientsDegradesOnURLBlocked(unittest.TestCase):
    """AC3: a URLBlocked thinking-client constructor must degrade, not raise."""

    def test_urlblocked_thinking_client_degrades_to_none(self):
        import app.lifespan as lifespan_module
        from app.services.ssrf import URLBlocked

        instant_sentinel = object()

        # create_instant_client is pinned to a sentinel so the ONLY failure
        # source under test is the thinking factory: the conftest-configured
        # instant endpoint (localhost) could itself raise URLBlocked on some
        # hosts, which would keep the check red even after the thinking-side
        # fix lands.
        raised: BaseException | None = None
        result = None
        try:
            with (
                patch.object(
                    lifespan_module,
                    "create_thinking_client",
                    side_effect=URLBlocked("host did not resolve"),
                ),
                patch.object(
                    lifespan_module,
                    "create_instant_client",
                    return_value=instant_sentinel,
                ),
            ):
                result = lifespan_module._create_llm_clients()
        except BaseException as exc:  # noqa: BLE001 - the point of the check
            raised = exc

        self.assertIsNone(
            raised,
            "AC3: _create_llm_clients() must return (degrade) when the "
            f"thinking-client constructor raises URLBlocked; raised {raised!r}",
        )
        self.assertIsInstance(
            result, tuple, "AC3: _create_llm_clients() must return a tuple"
        )
        self.assertEqual(
            len(result), 2, "AC3: the tuple must be (thinking, instant)"
        )
        thinking, instant = result
        self.assertIsNone(
            thinking,
            "AC3: a URLBlocked thinking client must degrade to None so the "
            "app boots degraded instead of failing startup",
        )
        self.assertIs(
            instant,
            instant_sentinel,
            "AC3: the instant client is not the failing half and must be "
            "returned untouched",
        )


class TestFTSRemediationPointerExists(unittest.TestCase):
    """AC7: lifespan must not direct operators at a nonexistent method."""

    def test_lifespan_does_not_point_at_missing_fts_method(self):
        vector_store_module = pytest.importorskip("app.services.vector_store")
        vector_store = vector_store_module.VectorStore
        source = _lifespan_source()
        ok = ("_ensure_fts_index" not in source) or hasattr(
            vector_store, "_ensure_fts_index"
        )
        self.assertTrue(
            ok,
            "AC7: lifespan.py points operators at "
            "VectorStore._ensure_fts_index(), which does not exist on "
            "VectorStore — dead-end guidance during an FTS incident. Remove "
            "the pointer or restore the method.",
        )


class TestStaleStartupWarningsRemoved(unittest.TestCase):
    """AC8: the three pre-reject_insecure_defaults warning texts must be gone."""

    def test_main_has_no_stale_warning_texts(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        stale_texts = (
            "effectively unauthenticated",
            "jwt_secret_key is using the default value",
            "will be rejected by browsers",
        )
        present = [text for text in stale_texts if text in source]
        self.assertEqual(
            len(present),
            0,
            "AC8: main.py still carries stale/unreachable startup-warning "
            f"text(s) {present}; the warned states now refuse to start via "
            "Settings.reject_insecure_defaults, and the CORS wording is "
            "wrong for Starlette's reflect-Origin behavior",
        )


if __name__ == "__main__":
    unittest.main()
