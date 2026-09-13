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


def _assignment_signature(node):
    if isinstance(node, ast.AugAssign):
        return (
            ast.unparse(node.target),
            type(node.op).__name__,
            ast.unparse(node.value),
        )
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return (
            ast.unparse(node.targets[0]),
            "Assign",
            ast.unparse(node.value),
        )
    return None


def _assignment_lines(function, *, target, operation, value):
    expected = (target, operation, value)
    return [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AugAssign))
        and _assignment_signature(node) == expected
    ]


def _call_is(node, *, owner, attribute):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner
    )


@pytest.mark.parametrize("processor_type", PROCESSORS)
def test_startup_lifecycle_source_contract(processor_type):
    """Source inspection pins lifecycle ordering that is hard to isolate behaviorally."""
    source_file = inspect.getsourcefile(processor_type)
    assert source_file is not None
    source_path = Path(source_file)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    start = _ast_function(tree, "start")
    stop = _ast_function(tree, "stop")

    running_guard = next(
        node
        for node in ast.walk(start)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "self._running"
    )
    assert any(isinstance(node, ast.Return) for node in ast.walk(running_guard))

    generation_lines = _assignment_lines(
        start,
        target="self._generation",
        operation="Add",
        value="1",
    )
    captured_generation_lines = _assignment_lines(
        start,
        target="generation",
        operation="Assign",
        value="self._generation",
    )
    running_true_lines = _assignment_lines(
        start,
        target="self._running",
        operation="Assign",
        value="True",
    )
    assert generation_lines and captured_generation_lines and running_true_lines
    assert generation_lines[0] < captured_generation_lines[0] < running_true_lines[0]

    shield_awaits = [
        node
        for node in ast.walk(start)
        if isinstance(node, ast.Await)
        and _call_is(node.value, owner="asyncio", attribute="shield")
    ]
    assert len(shield_awaits) == 1

    if_tests = [
        ast.unparse(node.test)
        for node in ast.walk(start)
        if isinstance(node, ast.If)
    ]
    assert sum(
        "generation != self._generation" in test and "not self._running" in test
        for test in if_tests
    ) == 1
    rollback_tests = [
        handler_test
        for node in ast.walk(start)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        for handler_test in ast.walk(handler)
        if isinstance(handler_test, ast.If)
    ]
    rollback = next(
        test
        for test in rollback_tests
        if "generation == self._generation" in ast.unparse(test.test)
    )
    rollback_assignments = {
        _assignment_signature(node)
        for node in ast.walk(rollback)
        if isinstance(node, (ast.Assign, ast.AugAssign))
    }
    assert (
        ("self._running", "Assign", "False") in rollback_assignments
        and ("self._task", "Assign", "None") in rollback_assignments
    )

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
        if _call_is(call, owner="asyncio", attribute="create_task")
    )
    assert len(create_lines) == 2
    assert create_lines[0] < shield_awaits[0].lineno < create_lines[1]

    stop_generation_lines = _assignment_lines(
        stop,
        target="self._generation",
        operation="Add",
        value="1",
    )
    stop_running_false_lines = _assignment_lines(
        stop,
        target="self._running",
        operation="Assign",
        value="False",
    )
    stop_capture_lines = _assignment_lines(
        stop,
        target="task",
        operation="Assign",
        value="self._task",
    )
    stop_clear_lines = _assignment_lines(
        stop,
        target="self._task",
        operation="Assign",
        value="None",
    )
    assert (
        stop_generation_lines
        and stop_running_false_lines
        and stop_capture_lines
        and stop_clear_lines
    )
    assert stop_generation_lines[0] < stop_running_false_lines[0]
    assert stop_running_false_lines[0] < stop_capture_lines[0] < stop_clear_lines[0]
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
