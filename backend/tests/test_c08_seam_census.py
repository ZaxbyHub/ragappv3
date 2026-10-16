"""Issue #713 structural census: the mandatory Phase-4.2 AST guardrail.

The frozen behavioral checks prove each seam individually; this census
proves the WIRING structurally, so a future edit that reverts one seam (or
builds embed coroutines outside the admission context again) fails here
even if no behavioral test exercises that exact path:

- P1: inside ``RAGEngine.query``, the standard-route ``_execute_retrieval``
  call, the ``_orchestrate_sub_query_retrieval`` call, every ``.evaluate()``
  call and every ``.distill()`` call must pass ``retrieval_query`` (never
  the raw ``user_input``) as the query-text argument (T1-32-K-10).
- P2: every ``filter_relevant(...)`` call site in RAGEngine (outside the
  ``_filter_relevant`` backward-compat shim) must thread the actual rerank
  status instead of the literal ``reranked=False`` (T1-32-K-02 / RAG-006).
- P3: every ``embed_tasks`` coroutine-list construction and the
  ``_fallback_embedding`` provider embed must sit inside an
  ``admit(AdmissionClass.EMBEDDING)`` context (T1-32-S2-02 / T1-32-S2-03).

RED at the base commit (465f76d2), GREEN after the fix.
"""

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "app" / "services" / "rag_engine.py"


def _load() -> ast.ClassDef:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "RAGEngine":
            return node
    raise AssertionError("RAGEngine class not found")


def _method(cls: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found")


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _parents(root: ast.AST) -> dict:
    parent = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _inside_admit_embedding(node: ast.AST, root: ast.AST) -> bool:
    parent = _parents(root)
    current = parent.get(node)
    while current is not None:
        if isinstance(current, ast.AsyncWith):
            for item in current.items:
                expr = item.context_expr
                if (
                    isinstance(expr, ast.Call)
                    and _call_name(expr) == "admit"
                    and expr.args
                    and isinstance(expr.args[0], ast.Attribute)
                    and expr.args[0].attr == "EMBEDDING"
                ):
                    return True
        current = parent.get(current)
    return False


def test_p1_query_seams_pass_retrieval_query():
    cls = _load()
    query = _method(cls, "query")
    problems = []
    for node in ast.walk(query):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        arg = None
        if name == "_execute_retrieval" and len(node.args) >= 2:
            arg = node.args[1]
        elif name in {"evaluate", "distill"} and node.args:
            arg = node.args[0]
        elif name == "_orchestrate_sub_query_retrieval":
            for kw in node.keywords:
                if kw.arg == "user_input":
                    arg = kw.value
        if arg is None:
            continue
        if not (isinstance(arg, ast.Name) and arg.id == "retrieval_query"):
            problems.append(f"{name} at line {node.lineno} does not pass retrieval_query")
    assert problems == [], "rewrite-seam census violations: " + "; ".join(problems)


def test_p2_filter_relevant_never_hardcodes_reranked_false():
    cls = _load()
    problems = []
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name == "_filter_relevant":
            # Documented backward-compat shim (frozen callers in
            # tests/test_rag_engine.py); disposition RETAIN.
            continue
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or _call_name(node) != "filter_relevant":
                continue
            reranked = None
            for kw in node.keywords:
                if kw.arg == "reranked":
                    reranked = kw.value
            if reranked is None:
                problems.append(
                    f"filter_relevant at line {node.lineno} in {method.name} "
                    "passes no reranked status"
                )
            elif isinstance(reranked, ast.Constant) and reranked.value is False:
                problems.append(
                    f"filter_relevant at line {node.lineno} in {method.name} "
                    "hardcodes reranked=False"
                )
    assert problems == [], "rerank-status census violations: " + "; ".join(problems)


def test_p3_embed_coroutines_built_inside_embedding_admission():
    cls = _load()
    problems = []
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.Assign) and isinstance(
                node.value, ast.ListComp
            ):
                target_names = {
                    t.id for t in node.targets if isinstance(t, ast.Name)
                }
                if target_names != {"embed_tasks"}:
                    continue
                if not _inside_admit_embedding(node, method):
                    problems.append(
                        f"embed_tasks built outside admit(EMBEDDING) at "
                        f"line {node.lineno} in {method.name}"
                    )
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "embed_single"
                and method.name == "_fallback_embedding"
                and not _inside_admit_embedding(node, method)
            ):
                problems.append(
                    f"_fallback_embedding embed_single outside admit at "
                    f"line {node.lineno}"
                )
    assert problems == [], "admission census violations: " + "; ".join(problems)
