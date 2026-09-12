import ast
import inspect
from pathlib import Path

import pytest

from app.services.draft_job_processor import DraftJobProcessor
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.wiki_compile_processor import WikiCompileProcessor

PROCESSORS = (
    pytest.param(KMSCompileProcessor, id="KMS"),
    pytest.param(WikiCompileProcessor, id="Wiki"),
    pytest.param(DraftJobProcessor, id="Draft"),
)


def _ast_function(tree, name):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    )


def _assignment(node, expected):
    assert isinstance(node, (ast.Assign, ast.AugAssign))
    assert ast.unparse(node) == expected


@pytest.mark.parametrize("processor_type", PROCESSORS)
def test_startup_lifecycle_source_contract(processor_type):
    """Source inspection pins lifecycle ordering that is hard to isolate behaviorally."""
    source_path = Path(inspect.getsourcefile(processor_type))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    start = _ast_function(tree, "start")
    stop = _ast_function(tree, "stop")
    assert isinstance(start.body[0], ast.If)
    assert ast.unparse(start.body[0].test) == "self._running"
    assert isinstance(start.body[0].body[0], ast.Return)
    _assignment(start.body[1], "self._generation += 1")
    _assignment(start.body[2], "generation = self._generation")
    _assignment(start.body[3], "self._running = True")
    awaits = [node for node in ast.walk(start) if isinstance(node, ast.Await)]
    assert len(awaits) == 1
    assert ast.unparse(awaits[0].value) == "asyncio.shield(reset_task)"

    if_tests = [node.test for node in ast.walk(start) if isinstance(node, ast.If)]
    assert sum(ast.unparse(test) == "generation != self._generation or not self._running" for test in if_tests) == 1
    rollback_tests = [
        handler_test
        for node in ast.walk(start)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        for handler_test in ast.walk(handler)
        if isinstance(handler_test, ast.If)
    ]
    rollback = next(test for test in rollback_tests if ast.unparse(test.test) == "generation == self._generation")
    rollback_assignments = {ast.unparse(node) for node in rollback.body if isinstance(node, ast.Assign)}
    assert {"self._running = False", "self._task = None"} <= rollback_assignments

    publication_closures = set()
    for node in ast.walk(start):
        if not isinstance(node, ast.Try):
            continue
        created = {
            call.args[0].id
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "create_task"
            and call.args
            and isinstance(call.args[0], ast.Name)
        }
        for handler in node.handlers:
            if not isinstance(handler.type, ast.Name) or handler.type.id != "BaseException":
                continue
            closed = {
                call.func.value.id
                for call in ast.walk(handler)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "close"
                and isinstance(call.func.value, ast.Name)
            }
            publication_closures.update(created & closed)
    assert {"reset_coro", "poll_coro"} <= publication_closures

    create_lines = sorted(
        call.lineno
        for call in ast.walk(start)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "create_task"
    )
    assert len(create_lines) == 2
    assert create_lines[0] < awaits[0].lineno < create_lines[1]

    _assignment(stop.body[0], "self._generation += 1")
    _assignment(stop.body[1], "self._running = False")
    _assignment(stop.body[2], "task = self._task")
    _assignment(stop.body[3], "self._task = None")
    stop_await_lines = [node.lineno for node in ast.walk(stop) if isinstance(node, ast.Await)]
    assert stop_await_lines
    cancel_lines = [
        node.lineno
        for node in ast.walk(stop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cancel"
    ]
    assert cancel_lines and min(cancel_lines) < min(stop_await_lines)
