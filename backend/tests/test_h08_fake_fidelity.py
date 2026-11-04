"""Acceptance checks for issue #745 (Workstream H PR 8): test doubles must match the real service/library contracts. RED at base where the audited defect is live, GREEN after the fix; pin-class checks stay GREEN at both ends.

Arm's-length checks, one per #745 acceptance criterion (AC1-AC12; AC13 is the
preserving run of the suites these checks inspect and lives in the issue's own
argv). Each check mechanically counts the defect shape the issue describes:

* AC1 (T1-06-K2-02) — named ``get_pool`` failure-injecting doubles must carry
  the real ``(sqlite_path, max_size=5)`` signature, never ``*args, **kwargs``.
  DRIFT PIN: at current master both raisers are already signature-faithful
  (fixed upstream by Workstream C PR 2), so the count is 0 and this check is
  a regression pin that stays GREEN at both ends.
* AC2 (T1-32-K-07) — fake ``{"type": "fallback", ...}`` chunks in backend
  tests may only use keys the real engine's fallback chunk carries. DRIFT
  PIN: #715 added a ``content`` key to the real engine chunk, so the one fake
  that used it is now a subset of the union; count is 0 at base.
* AC3 (T1-35-K-02) — AMENDED (plan-critic r1): the audit premise was
  invalidated by the #716 fix (identity ``activation_fn``; the call-site
  contract IS raw logits, so the raw-logit mocks are faithful). The check
  now PINS that every reranking.py ``.predict`` call passes the identity
  activation (single-sigmoid discipline). GREEN at base.
* AC4 (TQ-differential-b-01) — retrieval record fakes must not key relevance
  under ``score`` (LanceDB 0.40 FTS rows carry ``_score`` — locked
  requirements-lock.txt; wheel-verified). RED at base: 12.
* AC5 (T1-01-K-13) — the eval answer-similarity suite must cover a realistic
  768-d normalized vector, not only the exact basis vector. RED at base: 0.
* AC6 (T1-01-K-14) — the IMAP fake needs an ``aioimaplib.Abort`` logout
  failure path. RED at base: 0.
* AC7/AC8 (T1-27-K-12) — mutation-kill harness: the LLM-health suites must
  fail when ``check_chat``'s client timeout is multiplied by 60 (AC7) or when
  ``_probe_client`` sends ``max_tokens=4096`` instead of ``1`` (AC8). RED at
  base: no suite exercises ``check_chat``/``_probe_client``, so the mutant
  survives with 0 failures.
* AC9 (T1-25-KR-08) — the AC13 contextual-prefix budget check's oracle must
  be derived from a qwen-family prefix path. RED at base: 0.
* AC10 (T1-25-KR-08) — at least one backend test must feed real
  ``unstructured.documents.elements`` classes to ``parse_elements_to_atoms``.
  RED at base: 0.
* AC11 (T1-02-K-08) — the trace-redaction test must seed every
  ``str``/``List[str]`` ``RAGTrace`` field with a sentinel. RED at base: 17
  of 20 fields never seeded.
* AC12 (T1-02-K-08) — ``MemoryPage.test.tsx`` must assert the payload passed
  to ``mockUpdateMemory``. RED at base: 0 payload assertions.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
TESTS = BACKEND / "tests"

# Suites driven under mutation for AC7/AC8 (issue #745 ACs name exactly these).
_LLM_HEALTH_SUITES = (
    "tests/test_issue494_llm_health.py",
    "tests/test_llm_health_dual.py",
    "tests/test_llm_reasoning_fallback.py",
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _constant_str_keys(node: ast.Dict) -> set:
    """The dict literal's constant string keys (spread/starred keys skipped)."""
    keys = set()
    for key in node.keys:
        if key is not None and isinstance(key, ast.Constant):
            if isinstance(key.value, str):
                keys.add(key.value)
    return keys


def _is_fallback_dict(node: ast.Dict) -> bool:
    """True for a dict literal carrying ``"type": "fallback"`` on constant keys."""
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "type"
            and isinstance(value, ast.Constant)
            and value.value == "fallback"
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# AC1 — get_pool raisers must mirror the real signature (drift pin at master)
# ---------------------------------------------------------------------------


def test_metadata_filter_get_pool_raisers_signature_faithful():
    """Named ``get_pool`` doubles patched in test_metadata_filter.py must not
    be signature-agnostic ``*args, **kw`` raisers: the real ``get_pool`` takes
    ``(sqlite_path, max_size=5)``, and a ``**kw`` raiser absorbs any arity
    drift at the call site (#707 defect class).

    DRIFT NOTE (pin): at the audit base these raisers were ``_raise(*a, **kw)``
    (RED=2); at current master both carry ``(sqlite_path, max_size=5)`` and the
    L82 lambda fixture was replaced by a signature-faithful def (Workstream C
    PR 2). AMENDED (CHECK_WRONG, plan-critic round 1): strengthened from
    rejecting only ``*args/**kwargs`` shapes to exact-signature comparison —
    a zero-argument or otherwise wrong-shape double is equally blind to
    caller arity drift. Expected offenders at master: 0."""
    tree = _parse(TESTS / "test_metadata_filter.py")
    patched_names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "setattr" or len(node.args) < 2:
            continue
        target, double = node.args[0], node.args[1]
        if (
            isinstance(target, ast.Constant)
            and target.value == "app.models.database.get_pool"
            and isinstance(double, ast.Name)
        ):
            patched_names.add(double.id)
    assert patched_names, (
        "H08-AC1 HARNESS DRIFT: test_metadata_filter.py no longer replaces "
        "app.models.database.get_pool with any named function double — the "
        "check has nothing to inspect"
    )

    def _exact_real_signature(fn: ast.FunctionDef) -> bool:
        """True when the def mirrors the real get_pool signature exactly:
        (sqlite_path, max_size=5) — two positional-or-keyword params, the
        second defaulted to the constant 5, and nothing else."""
        args = fn.args
        if args.vararg is not None or args.kwarg is not None:
            return False
        if args.kwonlyargs or args.posonlyargs:
            return False
        if [a.arg for a in args.args] != ["sqlite_path", "max_size"]:
            return False
        defaults = args.defaults
        if len(defaults) != 1:
            return False
        return isinstance(defaults[0], ast.Constant) and defaults[0].value == 5

    offenders = []
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in patched_names:
            if (node.name, node.lineno) in seen:
                continue
            seen.add((node.name, node.lineno))
            if not _exact_real_signature(node):
                offenders.append(f"{node.name} (test_metadata_filter.py:{node.lineno})")
    assert len(offenders) == 0, (
        "H08-AC1 get_pool RAISERS DO NOT MIRROR THE REAL SIGNATURE: "
        f"{offenders} must declare exactly (sqlite_path, max_size=5) like the "
        "real app.models.database.get_pool, so any caller arity mismatch "
        "fails the raiser instead of hiding behind it (or use "
        "mock.create_autospec)"
    )


# ---------------------------------------------------------------------------
# AC2 — fallback chunk fakes must be a subset of the engine's key union (pin)
# ---------------------------------------------------------------------------


def test_fallback_chunk_fakes_match_engine_shape():
    """Every fake ``{"type": "fallback", ...}`` chunk in the route/engine
    tests may only use keys the real engine's fallback chunk carries, so the
    two sides cannot silently diverge (one side checks a key the other never
    emits).

    The union is computed from backend/app/services/rag_engine.py dict
    literals at check time. DRIFT NOTE (pin): #715 added ``content`` to the
    real chunk, so the previously-RED fake at test_chat_stage_events.py:622
    is now a subset; expected count at master: 0."""
    engine_union = set()
    for node in ast.walk(_parse(BACKEND / "app" / "services" / "rag_engine.py")):
        if isinstance(node, ast.Dict) and _is_fallback_dict(node):
            engine_union |= _constant_str_keys(node)
    assert engine_union, (
        "H08-AC2 HARNESS DRIFT: rag_engine.py no longer emits any "
        '"type": "fallback" dict literal — the union is empty and the check '
        "cannot discriminate"
    )

    offenders = []
    for name in (
        "test_chat_stage_events.py",
        "test_integration.py",
        "test_rag_pipeline.py",
        "test_issue494_rag_engine.py",
    ):
        path = TESTS / name
        if not path.is_file():
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Dict) and _is_fallback_dict(node):
                fake_keys = _constant_str_keys(node)
                if not fake_keys <= engine_union:
                    offenders.append(
                        f"{name}:{node.lineno} keys={sorted(fake_keys)} "
                        f"(engine union: {sorted(engine_union)})"
                    )
    assert len(offenders) == 0, (
        "H08-AC2 FALLBACK FAKES DIVERGE FROM ENGINE CHUNK: "
        f"{offenders} — fake fallback chunks must only use keys the real "
        "engine emits (build them from the engine or mirror its key set)"
    )


# ---------------------------------------------------------------------------
# AC3 — reranking.py .predict calls must keep the identity activation (pin)
# ---------------------------------------------------------------------------




def test_reranker_predict_mocks_return_probabilities():
    """AMENDED (CHECK_WRONG, plan-critic round 1): the audit's premise — that
    ``CrossEncoder.predict`` returns post-sigmoid probabilities at this call
    site — was invalidated by the #716 fix (T1-35-K-01, merged 450d4362):
    production passes ``activation_fn=_identity_activation`` so the CALL-SITE
    contract is raw logits and the existing raw-logit mocks are faithful.
    The faithful pin is therefore the caller's activation contract: every
    ``.predict`` call in reranking.py must pass an identity
    ``activation_fn`` (single-sigmoid discipline — the caller-side
    ``_safe_sigmoid`` stays the only sigmoid), and the identity helper must
    return its argument unchanged. GREEN at base; RED if a call site drops
    the identity activation or the helper stops being the identity."""
    rerank_src = (BACKEND / "app" / "services" / "reranking.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(rerank_src)

    # The identity helper: a module-level def whose body is `return <name>`
    # where <name> is its single parameter.
    identity_names = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        body = node.body
        # Skip a leading docstring expression before the single return.
        if (
            len(body) == 2
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        value = body[0].value
        args = node.args.args
        if (
            isinstance(value, ast.Name)
            and len(args) == 1
            and value.id == args[0].arg
            and node.args.vararg is None
            and node.args.kwarg is None
        ):
            identity_names.add(node.name)
    assert identity_names, (
        "H08-AC3 HARNESS DRIFT: reranking.py no longer defines a module-level "
        "identity helper (expected `_identity_activation` per the #716 fix); "
        "functions defined: "
        f"{[n.name for n in tree.body if isinstance(n, ast.FunctionDef)]}"
    )

    predict_calls = 0
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "predict":
            continue
        predict_calls += 1
        activation = None
        for kw in node.keywords:
            if kw.arg == "activation_fn":
                activation = kw.value
                break
        ok = (
            activation is not None
            and isinstance(activation, ast.Name)
            and activation.id in identity_names
        )
        if not ok:
            offenders.append(f"reranking.py:{node.lineno}: {ast.unparse(node)}")
    assert predict_calls >= 1, (
        "H08-AC3 HARNESS DRIFT: reranking.py no longer calls .predict( — the "
        "activation-contract pin has nothing to inspect"
    )
    assert not offenders, (
        "H08-AC3 RERANKER PREDICT CALL WITHOUT IDENTITY ACTIVATION: "
        f"{offenders} — without activation_fn=<identity>, sentence-"
        "transformers >= 5.6 applies Sigmoid inside predict for single-label "
        "CrossEncoders and the caller-side _safe_sigmoid becomes a second "
        "sigmoid (the #716 double-sigmoid defect class); every .predict call "
        "must pass the identity activation (raw-logit call-site contract, "
        "which the raw-logit mocks in test_reranking_contract.py mirror)"
    )


# ---------------------------------------------------------------------------
# AC4 — retrieval record fakes must not use the wrong relevance key
# ---------------------------------------------------------------------------


def test_fts_record_fakes_use_real_score_key():
    """Dict literals shaped like retrieval records (``text``/``file_id``/``id``)
    that carry a ``score`` key — with neither ``_distance`` nor ``_score`` —
    model a row LanceDB FTS never returns (locked 0.40.0 carries
    ``_score``; wheel-verified), so fixtures keyed ``score`` can never
    observe the product's ``record.get('score')`` default-1.0 path on a
    real FTS hit."""
    offenders = []
    for name in (
        "test_hybrid_logging.py",
        "test_fts_status_tracking.py",
        "test_rag_engine_filtering_adversarial.py",
        "test_relevance_cutoff_calibration.py",
        "test_rag_engine_none_threshold_adversarial.py",
    ):
        for node in ast.walk(_parse(TESTS / name)):
            if not isinstance(node, ast.Dict):
                continue
            keys = _constant_str_keys(node)
            if (
                "score" in keys
                and "_distance" not in keys
                and "_score" not in keys
                and ({"text", "file_id", "id"} & keys)
            ):
                offenders.append(f"{name}:{node.lineno} keys={sorted(keys)}")
    assert len(offenders) == 0, (
        "H08-AC4 FTS RECORD FAKES KEY RELEVANCE UNDER 'score': "
        f"{offenders} — real LanceDB FTS rows carry '_score'; rebuild "
        "the fixtures from the real row shape (the product-side _score floor "
        "gap is filed separately per the issue's Constraints)"
    )


# ---------------------------------------------------------------------------
# AC5/AC6 — literal-presence checks for realistic-vector and Abort coverage
# ---------------------------------------------------------------------------


def test_eval_similarity_has_realistic_vector_case():
    """The eval answer-similarity suite must at least mention a 768-d
    embedding (the served models' dimensionality): the exact basis vector
    ``[1.0, 0.0, 0.0]`` has cosine self-similarity exactly 1.0 in binary
    floating point, so it can never catch the rounding-overshoot defect real
    normalized vectors produce."""
    src = (TESTS / "test_eval_endpoint.py").read_text(encoding="utf-8")
    count = len(re.findall(r"\b768\b", src))
    assert count >= 1, (
        "H08-AC5 NO REALISTIC 768-d VECTOR CASE: tests/test_eval_endpoint.py "
        "never mentions 768 — the only identical-embedding case is the exact "
        "basis vector [1.0, 0.0, 0.0], whose cosine self-similarity can never "
        "overshoot 1.0 the way real normalized vectors do; add a random "
        "normalized 768-d self-pair case asserting <= 1.0"
    )


def test_imap_fake_has_abort_logout_path():
    """The IMAP unseen-count contract suite must reference ``Abort``: real
    aioimaplib (locked 2.0.3; wheel-verified Abort on logout in STARTED
    state) raises ``Abort("command LOGOUT illegal in state ...")``
    when the connection never reached NONAUTH, and a fake whose ``logout()``
    always succeeds cannot test the documented "-1 if connection fails"
    contract."""
    src = (TESTS / "test_issue494_imap_search_contract.py").read_text(
        encoding="utf-8"
    )
    count = len(re.findall(r"Abort", src))
    assert count >= 1, (
        "H08-AC6 IMAP FAKE HAS NO Abort LOGOUT PATH: the fake client's "
        "logout() always succeeds, so the documented '-1 if connection "
        "fails' contract of _get_unseen_count has no failure-path test; add "
        "a fake whose logout() raises aioimaplib.Abort (the product finally-"
        "catch gap this reveals is filed separately per Constraints)"
    )


# ---------------------------------------------------------------------------
# AC7/AC8 — mutation-kill harness for the LLM-health suites
# ---------------------------------------------------------------------------


def _parse_failed(stdout: str) -> int:
    match = re.search(r"(\d+) failed", stdout)
    return int(match.group(1)) if match else 0


def _run_llm_health_suites_under_mutation(
    tmp_path: Path, anchor: str, replacement: str
) -> "int | None":
    """Copy the repo's backend/ and scripts/ into ``tmp_path`` (ignoring
    data/, __pycache__, .pytest_cache, .venv, .swarm and .tmp-*), run the
    three LLM-health suites UNMUTATED as a control, assert ``anchor`` is
    present in the copied app/services/llm_health.py, apply the mutation, run
    the suites again, and return the number of FAILED tests in the mutant
    run — or ``None`` when the harness itself is broken (the control run is
    not clean, or the mutant run errored without a parsable failure
    summary), so callers never mistake an environmental red for a mutation
    kill (PRR-006, feedback round 1)."""
    copy_root = tmp_path / "mutation-copy"
    ignore = shutil.ignore_patterns(
        "data",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "*.pyc",
        # Gitignored local bulk artifacts (~1 GB / thousands of files on dev
        # boxes): copying them into tmp_path per run wastes minutes and can
        # exceed the Windows MAX_PATH limit (PRR-005, feedback round 1; the
        # sibling h06/h07 harnesses exclude the same set).
        ".swarm",
        ".tmp-*",
    )
    started = time.monotonic()
    shutil.copytree(REPO / "backend", copy_root / "backend", ignore=ignore)
    shutil.copytree(REPO / "scripts", copy_root / "scripts")
    copy_seconds = time.monotonic() - started

    def _run_suites() -> "subprocess.CompletedProcess[str]":
        argv = [
            sys.executable,
            "-m",
            "pytest",
            *_LLM_HEALTH_SUITES,
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'bt'}",
        ]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            argv,
            cwd=str(copy_root / "backend"),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    control = _run_suites()
    control_failed = _parse_failed(control.stdout)
    print(
        f"control run (unmutated): exit={control.returncode} "
        f"failed={control_failed}"
    )
    if control.returncode != 0 or control_failed != 0:
        print(control.stdout[-2000:])
        print(control.stderr[-2000:])
        return None

    target = copy_root / "backend" / "app" / "services" / "llm_health.py"
    source = target.read_text(encoding="utf-8")
    assert anchor in source, (
        f"H08 MUTATION HARNESS DRIFT: anchor {anchor!r} no longer present in "
        "app/services/llm_health.py — the mutation target moved; amend the "
        "check with the new anchor"
    )
    target.write_text(source.replace(anchor, replacement), encoding="utf-8")

    result = _run_suites()
    print(result.stdout[-2000:])
    print(result.stderr[-2000:])
    print(f"mutation copy took {copy_seconds:.1f}s; exit={result.returncode}")
    n_failed = _parse_failed(result.stdout)
    if result.returncode != 0 and n_failed == 0:
        # Non-zero exit without a "N failed" summary is a collection/runtime
        # error in the copy, not a clean mutation kill.
        print("mutant run errored without a parsable failure summary")
        return None
    return n_failed


def test_check_chat_timeout_mutant_is_killed(tmp_path):
    """Mutating ``check_chat`` to build its ``LLMClient`` with 60x the checker
    timeout must fail at least one LLM-health suite test — otherwise a
    candidate that ignores the timeout for chat ships green. RED at base: no
    suite in the three files calls ``check_chat``/``_probe_client``, so the
    mutant survives."""
    n_failed = _run_llm_health_suites_under_mutation(
        tmp_path,
        anchor="client = LLMClient(timeout=self.timeout)",
        replacement="client = LLMClient(timeout=self.timeout * 60)",
    )
    assert n_failed is not None, (
        "H08-AC7 HARNESS BROKEN: the copied LLM-health suites fail (or error) "
        "WITHOUT the mutation, so the run cannot attribute a failure to the "
        "mutant — fix the harness/copy environment before trusting the kill"
    )
    assert n_failed >= 1, (
        "H08-AC7 MUTANT SURVIVES: inflating check_chat's LLMClient timeout "
        "60x left every LLM-health suite green — no test exercises the "
        "check_chat/_probe_client timeout contract; add a slow-socket test "
        "mirroring the check_embeddings AC3 harness"
    )


def test_probe_client_max_tokens_mutant_is_killed(tmp_path):
    """Mutating ``_probe_client`` to send ``max_tokens=4096`` instead of ``1``
    must fail at least one LLM-health suite test. RED at base: the suites
    bypass ``LLMClient`` entirely, so the mutant survives."""
    n_failed = _run_llm_health_suites_under_mutation(
        tmp_path,
        anchor="                max_tokens=1,",
        replacement="                max_tokens=4096,",
    )
    assert n_failed is not None, (
        "H08-AC8 HARNESS BROKEN: the copied LLM-health suites fail (or error) "
        "WITHOUT the mutation, so the run cannot attribute a failure to the "
        "mutant — fix the harness/copy environment before trusting the kill"
    )
    assert n_failed >= 1, (
        "H08-AC8 MUTANT SURVIVES: changing _probe_client's max_tokens from 1 "
        "to 4096 left every LLM-health suite green — the minimal-ping probe "
        "contract is untested; pin max_tokens=1 in a probe test"
    )


# ---------------------------------------------------------------------------
# AC9/AC10 — vocabulary/oracle fidelity for the atom adapter and AC13 check
# ---------------------------------------------------------------------------


def test_c13_oracle_uses_qwen_prefix_path():
    """The frozen AC13 contextual-prefix budget check must derive its oracle
    from a qwen-family model name through
    ``EmbeddingService.embedding_doc_prefix`` — reading the product's own
    ``settings.embedding_doc_prefix`` cannot detect a Qwen auto-prefix gap."""
    path = TESTS / "issue513_checks" / "test_c13_contextual_prefix_budget.py"
    src = path.read_text(encoding="utf-8")
    count = len(re.findall(r"qwen", src, re.IGNORECASE))
    assert count >= 1, (
        "H08-AC9 ORACLE READS THE PRODUCT'S OWN SETTING: the AC13 check "
        "never mentions a qwen-family model, so its oracle shares the "
        "product's embedding_doc_prefix assumption and cannot detect a Qwen "
        "auto-prefix gap; compute the oracle from "
        "EmbeddingService.embedding_doc_prefix with a qwen model name"
    )


def test_atom_adapter_has_real_unstructured_vocabulary_test():
    """At least one backend test file must bind the atom-kind adapter to the
    real ``unstructured`` category vocabulary (import real
    ``unstructured.documents.elements`` classes and feed them to
    ``parse_elements_to_atoms``); hand-built ``_Element`` fakes with
    author-chosen category strings cannot catch a vocabulary mismatch."""
    self_path = Path(__file__).resolve()
    matches = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.resolve() == self_path:
            # This check's own file names both markers; it must not satisfy
            # its own predicate.
            continue
        src = path.read_text(encoding="utf-8")
        # PRR-008 (feedback round 1): require an actual import edge, not a
        # text occurrence — the dotted marker previously appeared only in a
        # docstring, so replacing the real import with a fake kept the guard
        # green.
        if (
            "parse_elements_to_atoms" in src
            and (
                "from unstructured.documents import" in src
                or "import unstructured.documents" in src
            )
        ):
            matches.append(str(path.relative_to(REPO)))
    assert len(matches) >= 1, (
        "H08-AC10 NO REAL unstructured VOCABULARY TEST: no backend test "
        "imports real unstructured.documents.elements classes and feeds them "
        "to parse_elements_to_atoms — the adapter is only tested against "
        "hand-built fakes; add a real-vocabulary test (nightly full-deps "
        "tier, importorskip in standard CI)"
    )


# ---------------------------------------------------------------------------
# AC11 — the redaction test must sentinel every str/List[str] RAGTrace field
# ---------------------------------------------------------------------------


def test_ragtrace_redaction_test_sentinels_every_str_field():
    """Every ``str``/``List[str]``/``Optional[...]``-thereof field annotated
    on ``RAGTrace`` must be assigned at least once in
    test_issue_296_logging_ssrf.py — a leak through any unseeded field
    cannot fail a test that never populates it."""
    tree = _parse(BACKEND / "app" / "services" / "rag_trace.py")
    annotation_re = re.compile(
        r"^(?:Optional\[(?:str|List\[str\]|list\[str\])\]"
        r"|str|List\[str\]|list\[str\])$"
    )
    fields = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RAGTrace":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                ):
                    if annotation_re.match(ast.unparse(stmt.annotation)):
                        fields.append(stmt.target.id)
    assert fields, (
        "H08-AC11 HARNESS DRIFT: RAGTrace in app/services/rag_trace.py no "
        "longer declares any str/List[str] annotated fields"
    )

    test_src = (TESTS / "test_issue_296_logging_ssrf.py").read_text(
        encoding="utf-8"
    )
    never_seeded = [
        name
        for name in fields
        if not re.search(rf"\b{name}\s*=", test_src)
    ]
    assert len(never_seeded) == 0, (
        f"H08-AC11 REDACTION TEST LEAVES {len(never_seeded)} TEXT FIELDS "
        f"UNSEEDED: {sorted(never_seeded)} of {len(fields)} str/List[str] "
        "RAGTrace fields are never assigned in "
        "test_issue_296_logging_ssrf.py, so a leak through any of them "
        "cannot fail the redaction test — sentinel every text field"
    )


# ---------------------------------------------------------------------------
# AC12 — MemoryPage.test.tsx must assert the update payload
# ---------------------------------------------------------------------------


def test_memory_page_asserts_update_payload():
    """``MemoryPage.test.tsx`` resets ``mockUpdateMemory`` but must also
    assert the payload it receives when an edit is exercised — a store write
    with the wrong shape passes every test that never inspects it. Static
    check on the TSX source; no vitest run needed."""
    src = (REPO / "frontend" / "src" / "pages" / "MemoryPage.test.tsx").read_text(
        encoding="utf-8"
    )
    count = len(
        re.findall(
            r"expect\(\s*mockUpdateMemory\s*\)\s*\.\s*"
            r"(?:toHaveBeenCalledWith|toHaveBeenLastCalledWith|"
            r"toHaveBeenNthCalledWith)",
            src,
        )
    )
    assert count >= 1, (
        "H08-AC12 mockUpdateMemory PAYLOAD NEVER ASSERTED: "
        "MemoryPage.test.tsx references mockUpdateMemory only as setup and "
        "mockReset — assert the payload passed on an edit "
        "(toHaveBeenCalledWith et al.)"
    )
