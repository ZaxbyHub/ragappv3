"""Issue #713 structural census: the mandatory Phase-4.2 AST guardrail.

The frozen behavioral checks prove each seam individually; this census
proves the WIRING structurally, so a future edit that reverts one seam (or
builds embed coroutines outside the admission context again) fails here
even if no behavioral test exercises that exact path:

- P1: inside ``RAGEngine.query`` and ``RAGEngine._orchestrate_sub_query_retrieval``,
  every query-text seam call must pass that method's rewrite-carrying value
  (``retrieval_query`` in query(); the ``user_input`` parameter — which the
  caller binds to the rewrite — inside the orchestration) — positional OR
  keyword form, and an unrecognized call shape FAILS LOUD instead of being
  skipped (T1-32-K-10). Covered seams: the standard-route ``_execute_retrieval``
  call and the ``_orchestrate_sub_query_retrieval`` call (``user_input``
  kwarg) in query(); every ``_execute_retrieval`` and consolidated ``rerank``
  call inside the orchestration; the CRAG evaluators (``evaluate``), the
  context distiller (``distill``), the query planner (``plan``), and the
  wiki/KMS receives handed to ``asyncio.to_thread`` as bare-name first
  arguments.
- P2: every ``filter_relevant(...)`` call site in RAGEngine (outside the
  ``_filter_relevant`` backward-compat shim) must thread the actual rerank
  status instead of the literal ``reranked=False`` or omitting the status
  (T1-32-K-02 / RAG-006).
- P3: every ``embed_tasks`` coroutine-list construction (whatever the
  construction shape) and the ``_fallback_embedding`` provider embed (a
  nested closure — matched by nearest enclosing function, not class-body
  method name) must sit inside an ``admit(AdmissionClass.EMBEDDING)``
  context, with site-count floors so a shape change that evades the
  matchers fails loud (T1-32-S2-02 / T1-32-S2-03).

The self-tests at the bottom feed KNOWN-BAD mutated sources through the
same matchers and assert they are flagged, so the census cannot silently
fail open (PRR-002 class).

RED at the base commit (465f76d2), GREEN after the fix.
"""

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "app" / "services" / "rag_engine.py"

# call-name -> (query-text positional index, query-text keyword name)
_QUERY_TEXT_SEAMS = {
    "_execute_retrieval": (1, "user_input"),
    "_orchestrate_sub_query_retrieval": (2, "user_input"),
    "evaluate": (0, "query"),
    "distill": (0, "query"),
    "plan": (0, "query"),
    "rerank": (0, "query"),
}
_EXPECTED_EMBED_TASKS_SITES = 3


def _parse(source_text: str) -> ast.AST:
    return ast.parse(source_text)


def _load_tree() -> ast.AST:
    return _parse(_SOURCE.read_text(encoding="utf-8"))


def _find_engine_class(tree: ast.AST) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RAGEngine":
            return node
    raise AssertionError("RAGEngine class not found")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
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


def _enclosing_function_name(node: ast.AST, root: ast.AST):
    parent = _parents(root)
    current = parent.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent.get(current)
    return None


def _query_text_arg(call: ast.Call, positional_index: int, keyword_name: str):
    """Query-text argument node, or None when the call matches neither the
    expected positional index nor the expected keyword (callers treat None
    as a fail-loud census violation, never a silent skip)."""
    if len(call.args) > positional_index:
        return call.args[positional_index]
    for kw in call.keywords:
        if kw.arg == keyword_name:
            return kw.value
    return None


def _p1_problems(tree: ast.AST) -> list:
    cls = _find_engine_class(tree)
    problems = []

    def scan(method, expected_name):
        for node in ast.walk(method):
            if isinstance(node, ast.Call) and _call_name(node) == "to_thread":
                # wiki/KMS receives: to_thread(self._wiki_retrieval.retrieve,
                # retrieval_query, vault_id) — the seam function is a bare-name
                # first argument, the query text the second.
                if (
                    node.args
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "retrieve"
                    and node.args[0].value is not None
                    and isinstance(node.args[0].value, ast.Attribute)
                    and node.args[0].value.attr in {"_wiki_retrieval", "_kms_retrieval"}
                ):
                    if len(node.args) < 2:
                        problems.append(
                            f"to_thread retrieve at line {node.lineno} passes no query text"
                        )
                    elif not (
                        isinstance(node.args[1], ast.Name)
                        and node.args[1].id == expected_name
                    ):
                        problems.append(
                            f"to_thread retrieve at line {node.lineno} does not pass {expected_name}"
                        )
                continue
            if not isinstance(node, ast.Call):
                continue
            spec = _QUERY_TEXT_SEAMS.get(_call_name(node))
            if spec is None:
                continue
            positional_index, keyword_name = spec
            arg = _query_text_arg(node, positional_index, keyword_name)
            if arg is None:
                problems.append(
                    f"{_call_name(node)} at line {node.lineno}: unrecognized call "
                    f"shape (no positional index {positional_index} and no "
                    f"'{keyword_name}=' keyword); census cannot verify the query text"
                )
            elif not (isinstance(arg, ast.Name) and arg.id == expected_name):
                problems.append(
                    f"{_call_name(node)} at line {node.lineno} does not pass {expected_name}"
                )

    scan(_method(cls, "query"), "retrieval_query")
    scan(_method(cls, "_orchestrate_sub_query_retrieval"), "user_input")
    return problems


def _p2_problems(tree: ast.AST) -> list:
    cls = _find_engine_class(tree)
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
    return problems


def _p3_problems(tree: ast.AST) -> list:
    cls = _find_engine_class(tree)
    problems = []
    embed_tasks_sites = 0
    fallback_embed_calls = 0
    for node in ast.walk(cls):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "embed_tasks" in names:
                embed_tasks_sites += 1
                if not _inside_admit_embedding(node, cls):
                    problems.append(
                        f"embed_tasks built outside admit(EMBEDDING) at "
                        f"line {node.lineno}"
                    )
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "embed_single"
            and _enclosing_function_name(node, cls) == "_fallback_embedding"
        ):
            fallback_embed_calls += 1
            if not _inside_admit_embedding(node, cls):
                problems.append(
                    f"_fallback_embedding embed_single outside admit at "
                    f"line {node.lineno}"
                )
    if embed_tasks_sites < _EXPECTED_EMBED_TASKS_SITES:
        problems.append(
            f"census found only {embed_tasks_sites} embed_tasks construction "
            f"site(s); expected >= {_EXPECTED_EMBED_TASKS_SITES} — the "
            "construction shape changed enough to evade this guard"
        )
    if fallback_embed_calls < 1:
        problems.append(
            "census found no embed_single call inside _fallback_embedding; "
            "the fallback-admission guard may be scanning the wrong shape"
        )
    return problems


def test_p1_query_seams_pass_retrieval_query():
    problems = _p1_problems(_load_tree())
    assert problems == [], "rewrite-seam census violations: " + "; ".join(problems)


def test_p2_filter_relevant_never_hardcodes_reranked_false():
    problems = _p2_problems(_load_tree())
    assert problems == [], "rerank-status census violations: " + "; ".join(problems)


def test_p3_embed_coroutines_built_inside_embedding_admission():
    problems = _p3_problems(_load_tree())
    assert problems == [], "admission census violations: " + "; ".join(problems)


# ------------------------------------------------------------------
# Self-tests: KNOWN-BAD mutated sources must be flagged by the same
# matchers. These close the fail-open class (PRR-002 / OOB L-8): any
# future matcher regression that would let one of these mutants pass
# fails here.
# ------------------------------------------------------------------


def _mutated_problems(mutate, checker):
    source = _SOURCE.read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, "self-test mutation did not apply"
    return checker(_parse(mutated))


def test_self_test_keyword_form_revert_bites_p1():
    """A keyword-form revert of the standard-route seam to the raw
    user_input must be flagged (the pre-hardening census skipped keyword
    shapes silently)."""

    def mutate(source: str) -> str:
        old = (
            "                    vector_results, relevance_hint, eval_result, "
            "rerank_success, score_type, hybrid_status, fts_exceptions, "
            "rerank_status, variants_dropped, exact_match_promoted, "
            "token_pack_stats = await self._execute_retrieval(\n"
            "                        query_embeddings,\n"
            "                        retrieval_query,\n"
            "                        vault_id,"
        )
        new = old.replace(
            "retrieval_query,\n                        vault_id,",
            "vault_id=vault_id,\n                        user_input=user_input,",
        )
        return source.replace(old, new, 1)

    problems = _mutated_problems(mutate, _p1_problems)
    assert any("_execute_retrieval" in p for p in problems), problems


def test_self_test_fallback_admit_removal_bites_p3():
    """Removing the admit around `_fallback_embedding`'s provider embed must
    be flagged (the pre-hardening census never scanned the nested closure)."""

    def mutate(source: str) -> str:
        old = (
            "            async with get_admission_controller().admit(\n"
            "                AdmissionClass.EMBEDDING\n"
            "            ):\n"
            "                return await self.embedding_service.embed_single(user_input)"
        )
        new = "            return await self.embedding_service.embed_single(user_input)"
        return source.replace(old, new, 1)

    problems = _mutated_problems(mutate, _p3_problems)
    assert any("_fallback_embedding" in p for p in problems), problems


def test_self_test_construction_shape_change_bites_p3():
    """Rebuilding an embed gather as a generator-expression tuple outside the
    admit must be flagged (the pre-hardening census matched only
    Assign+ListComp)."""

    def mutate(source: str) -> str:
        old = (
            "        async with get_admission_controller().admit(AdmissionClass.EMBEDDING):\n"
            "            embed_tasks = [_embed_one(vt, t) for vt, t in transformed_queries]\n"
            "            raw_embeddings = await asyncio.gather(\n"
            "                *embed_tasks, return_exceptions=True\n"
            "            )"
        )
        new = (
            "        embed_tasks = tuple(\n"
            "            _embed_one(vt, t) for vt, t in transformed_queries\n"
            "        )\n"
            "        raw_embeddings = await asyncio.gather(\n"
            "            *embed_tasks, return_exceptions=True\n"
            "        )"
        )
        return source.replace(old, new, 1)

    problems = _mutated_problems(mutate, _p3_problems)
    assert any("embed_tasks" in p for p in problems), problems


def test_self_test_positional_revert_bites_p1():
    """Control: the original positional revert must still be flagged."""

    def mutate(source: str) -> str:
        old = (
            "                    vector_results, relevance_hint, eval_result, "
            "rerank_success, score_type, hybrid_status, fts_exceptions, "
            "rerank_status, variants_dropped, exact_match_promoted, "
            "token_pack_stats = await self._execute_retrieval(\n"
            "                        query_embeddings,\n"
            "                        retrieval_query,\n"
            "                        vault_id,"
        )
        new = old.replace("retrieval_query,", "user_input,")
        return source.replace(old, new, 1)

    problems = _mutated_problems(mutate, _p1_problems)
    assert any("_execute_retrieval" in p for p in problems), problems
