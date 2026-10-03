"""Structural reachability checks for the #773 stacked-PR workflow contract.

These checks intentionally read the workflow source with a small indentation
aware parser instead of importing a YAML package.  They pin the event/ref
surface and the shell data flow that makes closure verification trustworthy:
the stack base must trigger, the review-event job must admit a ``codex/`` base,
and verify-test must resolve base/head from the PR API before calling
``git merge-base``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CLOSURE_WORKFLOW = REPO / ".github" / "workflows" / "closure-evidence.yml"
CLOSURE_CHECKER = REPO / "scripts" / "check_closure_evidence.py"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _workflow_events(text: str) -> list[str]:
    """Return event keys beneath the top-level ``on:`` mapping."""

    lines = text.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if _indent(line) == 0 and line.strip() in {"on:", '"on":', "'on':"}
    )
    events: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent(line) == 0:
            break
        match = re.fullmatch(r" {2}([A-Za-z_][A-Za-z0-9_-]*):(?:\s*.*)?", line)
        if match:
            events.append(match.group(1))
    return events


def _mapping_block(text: str, key: str, parent_indent: int = 2) -> list[str]:
    """Return one indentation-scoped mapping block from workflow YAML."""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _indent(line) != parent_indent or line.strip().split(":", 1)[0] != key:
            continue
        block: list[str] = []
        for child in lines[index + 1 :]:
            if child.strip() and _indent(child) <= parent_indent:
                break
            block.append(child)
        return block
    raise AssertionError(f"workflow mapping {key!r} is missing")


def _list_values(block: list[str], key: str) -> list[str]:
    """Read either ``key: [a, b]`` or an indented YAML list."""

    for index, line in enumerate(block):
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        key_indent = _indent(line)
        if value.startswith("[") and value.endswith("]"):
            return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        values: list[str] = []
        for child in block[index + 1 :]:
            if child.strip() and _indent(child) <= key_indent:
                break
            match = re.match(r"^\s*-\s*(.+?)\s*$", child)
            if match:
                values.append(match.group(1).strip().strip("'\""))
        return values
    raise AssertionError(f"workflow list {key!r} is missing")


def _closure_gate_if(text: str) -> str:
    """Return the literal multiline ``if: |`` expression for the gate job."""

    lines = text.splitlines()
    job_index = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == "closure-evidence-gate:" and _indent(line) == 2
    )
    for index in range(job_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and _indent(line) <= 2:
            break
        if _indent(line) == 4 and line.strip() == "if: |":
            expression: list[str] = []
            for child in lines[index + 1 :]:
                if child.strip() and _indent(child) <= 4:
                    break
                expression.append(child.strip())
            return " ".join(expression)
    raise AssertionError("closure evidence gate if expression is missing")


def _verify_test_block(text: str) -> str:
    marker = "- name: Re-execute the named evidence test (verify-test)"
    start = text.index(marker)
    remainder = text[start + len(marker) :]
    next_step = remainder.find("\n      - name:")
    return remainder if next_step < 0 else remainder[:next_step]


def _closure_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("l02_closure_checker", CLOSURE_CHECKER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {CLOSURE_CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IF_TOKEN = re.compile(
    r"\s*(?:(?P<op>==|!=|&&|\|\||[(),])|(?P<string>'[^']*'|\"[^\"]*\")|(?P<name>[A-Za-z_][A-Za-z0-9_.]*))"
)


def _if_tokens(expression: str) -> list[tuple[str, str]]:
    """Tokenize the small, known GitHub ``if`` grammar used by this job."""

    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _IF_TOKEN.match(expression, position)
        if match is None:
            raise AssertionError(f"unsupported closure guard syntax at offset {position}")
        kind = "op" if match.group("op") else "string" if match.group("string") else "name"
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


class _IfParser:
    """Fail-closed parser/evaluator for this workflow's boolean guard grammar."""

    def __init__(self, expression: str) -> None:
        self.tokens = _if_tokens(expression)
        self.position = 0

    def _peek(self, value: str | None = None) -> tuple[str, str] | None:
        if self.position >= len(self.tokens):
            return None
        token = self.tokens[self.position]
        return token if value is None or token[1] == value else None

    def _take(self, value: str | None = None) -> tuple[str, str]:
        token = self._peek(value)
        if token is None:
            raise AssertionError(f"expected {value!r} in closure guard")
        self.position += 1
        return token

    def parse(self) -> Any:
        tree = self._or()
        if self._peek() is not None:
            raise AssertionError("unsupported trailing closure guard syntax")
        return tree

    def _or(self) -> Any:
        tree = self._and()
        while self._peek("||"):
            self._take("||")
            tree = ("or", tree, self._and())
        return tree

    def _and(self) -> Any:
        tree = self._comparison()
        while self._peek("&&"):
            self._take("&&")
            tree = ("and", tree, self._comparison())
        return tree

    def _comparison(self) -> Any:
        tree = self._primary()
        token = self._peek()
        if token is not None and token[1] in {"==", "!="}:
            self._take()
            tree = (token[1], tree, self._primary())
        return tree

    def _primary(self) -> Any:
        if self._peek("("):
            self._take("(")
            tree = self._or()
            self._take(")")
            return tree
        kind, value = self._take()
        if kind == "string":
            return ("literal", value[1:-1])
        if kind != "name":
            raise AssertionError(f"unsupported closure guard token {value!r}")
        if self._peek("("):
            self._take("(")
            argument = self._or()
            self._take(",")
            second_argument = self._or()
            self._take(")")
            return ("call", value, argument, second_argument)
        return ("name", value)


def _evaluate_if(tree: Any, context: dict[str, str]) -> bool | str:
    kind = tree[0]
    if kind == "literal":
        return tree[1]
    if kind == "name":
        if tree[1] not in context:
            raise AssertionError(f"unsupported closure guard identifier {tree[1]!r}")
        return context[tree[1]]
    if kind == "call":
        if tree[1] != "startsWith":
            raise AssertionError(f"unsupported closure guard function {tree[1]!r}")
        value = _evaluate_if(tree[2], context)
        prefix = _evaluate_if(tree[3], context)
        if not isinstance(value, str) or not isinstance(prefix, str):
            raise AssertionError("startsWith arguments must resolve to strings")
        return value.casefold().startswith(prefix.casefold())
    if kind in {"==", "!="}:
        left = _evaluate_if(tree[1], context)
        right = _evaluate_if(tree[2], context)
        if isinstance(left, str) and isinstance(right, str):
            left = left.casefold()
            right = right.casefold()
        return left == right if kind == "==" else left != right
    if kind in {"and", "or"}:
        left = _evaluate_if(tree[1], context)
        right = _evaluate_if(tree[2], context)
        if not isinstance(left, bool) or not isinstance(right, bool):
            raise AssertionError(f"{kind} operands must resolve to booleans")
        return left and right if kind == "and" else left or right
    raise AssertionError(f"unsupported closure guard AST node {kind!r}")


def _guard_truth_table(expression: str) -> dict[str, bool]:
    tree = _IfParser(expression).parse()
    common = {
        "github.repository": "ZaxbyHub/ragappv3",
    }
    contexts = {
        "workflow_dispatch": {
            **common,
            "github.event_name": "workflow_dispatch",
            "github.event.pull_request.head.repo.full_name": "fork/example",
            "github.event.pull_request.base.ref": "other",
        },
        "pull_request_master_same_repo": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "master",
        },
        "pull_request_codex_same_repo": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "codex/773-stack",
        },
        "pull_request_codex_mixed_case_same_repo": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "zaxbyhub/RAGAPPV3",
            "github.event.pull_request.base.ref": "CoDeX/773-STACK",
        },
        "pull_request_other_same_repo": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "feature/other",
        },
        "pull_request_codex_fork": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "fork/example",
            "github.event.pull_request.base.ref": "codex/773-stack",
        },
        "pull_request_master_fork": {
            **common,
            "github.event_name": "pull_request",
            "github.event.pull_request.head.repo.full_name": "fork/example",
            "github.event.pull_request.base.ref": "master",
        },
        "pull_request_review_codex_same_repo": {
            **common,
            "github.event_name": "pull_request_review",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "codex/773-stack",
        },
        "pull_request_review_master_same_repo": {
            **common,
            "github.event_name": "pull_request_review",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "master",
        },
        "pull_request_review_master_fork": {
            **common,
            "github.event_name": "pull_request_review",
            "github.event.pull_request.head.repo.full_name": "fork/example",
            "github.event.pull_request.base.ref": "master",
        },
        "pull_request_review_other_same_repo": {
            **common,
            "github.event_name": "pull_request_review",
            "github.event.pull_request.head.repo.full_name": "ZaxbyHub/ragappv3",
            "github.event.pull_request.base.ref": "feature/other",
        },
        "pull_request_review_codex_fork": {
            **common,
            "github.event_name": "pull_request_review",
            "github.event.pull_request.head.repo.full_name": "fork/example",
            "github.event.pull_request.base.ref": "codex/773-stack",
        },
    }
    return {name: bool(_evaluate_if(tree, context)) for name, context in contexts.items()}


def test_ci_pull_request_trigger_accepts_codex_stack_base() -> None:
    events = _workflow_events(CI_WORKFLOW.read_text(encoding="utf-8"))
    pull_request = _mapping_block(CI_WORKFLOW.read_text(encoding="utf-8"), "pull_request")
    branches = _list_values(pull_request, "branches")

    assert "pull_request" in events
    assert set(branches) == {"master", "codex/**"}


def test_ci_preserves_master_push_and_merge_group_triggers() -> None:
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    events = _workflow_events(source)
    push = _mapping_block(source, "push")

    assert "merge_group" in events
    assert _list_values(push, "branches") == ["master"]


def test_closure_pull_request_trigger_accepts_codex_stack_base_and_types() -> None:
    source = CLOSURE_WORKFLOW.read_text(encoding="utf-8")
    pull_request = _mapping_block(source, "pull_request")

    assert set(_list_values(pull_request, "branches")) == {"master", "codex/**"}
    assert set(_list_values(pull_request, "types")) == {
        "opened",
        "edited",
        "synchronize",
        "reopened",
    }


def test_closure_review_guard_preserves_same_repo_and_accepts_stack_base() -> None:
    source = CLOSURE_WORKFLOW.read_text(encoding="utf-8")
    expression = _closure_gate_if(source)

    assert "github.event_name == 'workflow_dispatch'" in expression
    assert "github.event.pull_request.head.repo.full_name == github.repository" in expression
    assert "github.event.pull_request.base.ref == 'master'" in expression
    assert _guard_truth_table(expression) == {
        "workflow_dispatch": True,
        "pull_request_master_same_repo": True,
        "pull_request_codex_same_repo": True,
        "pull_request_codex_mixed_case_same_repo": True,
        "pull_request_other_same_repo": True,
        "pull_request_codex_fork": False,
        "pull_request_master_fork": False,
        "pull_request_review_codex_same_repo": True,
        "pull_request_review_master_same_repo": True,
        "pull_request_review_master_fork": False,
        "pull_request_review_other_same_repo": False,
        "pull_request_review_codex_fork": False,
    }


def test_closure_review_from_master_fork_is_explicitly_denied() -> None:
    expression = _closure_gate_if(CLOSURE_WORKFLOW.read_text(encoding="utf-8"))
    assert _guard_truth_table(expression)["pull_request_review_master_fork"] is False


def test_closure_policy_preserves_review_modes_and_api_base_resolution() -> None:
    source = CLOSURE_WORKFLOW.read_text(encoding="utf-8")
    review = _mapping_block(source, "pull_request_review")
    verify = _verify_test_block(source)

    assert set(_list_values(review, "types")) == {"submitted", "edited", "dismissed"}
    assert 'MODE: ${{ inputs.mode || \'warn\' }}' in source
    assert 'if [ "$MODE" = "enforce" ]' in source
    assert "closure-evidence: WARN" in source
    assert "closure-evidence: FAIL" in source
    assert "python scripts/check_closure_evidence.py evaluate" in source
    assert '--mode "$MODE"' in source

    assert 'PR_JSON="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER")"' in verify
    assert re.search(r"BASE_REF=.*\['base'\]\['ref'\]", verify, re.DOTALL)
    assert re.search(r"HEAD_SHA=.*\['head'\]\['sha'\]", verify, re.DOTALL)
    assert 'BASE_SHA="$(git merge-base "origin/$BASE_REF" "$HEAD_SHA")"' in verify
    assert '--base "$BASE_SHA" --head "$HEAD_SHA"' in verify

    checker = _closure_checker()
    violations, evidence = checker.evaluate_snapshot(
        {
            "body": "Closes #773\nEvidence: backend/tests/test_l02_stack_workflow_reachability.py::test_ci_pull_request_trigger_accepts_codex_stack_base",
            "changed_files": ["frontend/src/lib/api/canvas.ts"],
            "commits": [
                {
                    "author": "same-family-author",
                    "files": ["frontend/src/lib/api/canvas.ts"],
                }
            ],
            "reviews": [{"state": "APPROVED", "author": "same-family-author"}],
            "issue_labels": {"773": ["high"]},
        },
        "warn",
        REPO,
    )
    assert evidence is not None
    assert any("outside the fix's own file family" in violation for violation in violations)
