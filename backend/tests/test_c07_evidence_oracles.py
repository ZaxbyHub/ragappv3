"""Issue #941: evidence-strength oracles for the #712 memory/telemetry/
evaluator checks.

All five checks are NEW-SURFACE: the official base leg for each is this
file not existing (pytest "file or directory not found" in the frozen
replay). Authored to be GREEN at the tree that already holds the pinned
properties; each one goes RED under the mutation it exists to catch (the
silenced embed_query WARNING, the deleted fail-open call sites, a
reintroduced event-loop bridge under any import spelling, a two-term
underscore regression, an inlined stdlib json.dumps call in telemetry).
"""

import ast
import logging
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

if "lancedb.index" not in sys.modules:
    _ldb_idx = types.ModuleType("lancedb.index")
    _ldb_idx.FTS = type("FTS", (), {})
    _ldb_idx.IvfPq = type("IvfPq", (), {})
    sys.modules["lancedb.index"] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ("partition", "partition.auto", "chunking", "chunking.title",
                 "documents", "documents.elements"):
        sys.modules.setdefault(f"unstructured.{_sub}", types.ModuleType(f"unstructured.{_sub}"))
    sys.modules.setdefault("unstructured", types.ModuleType("unstructured"))

from app.models.database import SQLiteConnectionPool, init_db
from app.services import telemetry as telemetry_module
from app.services.memory_store import MemoryStore

_SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"


class _RaisingEmbeddingService:
    """Embedding-service double whose query embed always fails."""

    async def embed_single(self, text):
        raise RuntimeError("embed down")


class _RecordHandler(logging.Handler):
    """Collect every record emitted by a logger while attached."""

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def test_dense_failure_warning_is_credited_to_embed_query(tmp_path):
    """AC1 (#941): the dense-degradation oracle must credit the
    ``embed_query`` MECHANISM — a WARNING whose message names the query
    embedding ("Memory query embedding ..."), never merely "any WARNING on
    the memory_store logger". The block below also calls search_memories
    without the query_embedding kwarg, which fires the never-attempted
    tripwire ("Memory dense search skipped: ..."); the source-agnostic
    any-WARNING oracle survives the silence-embed_query mutation precisely
    because that tripwire alone satisfies it, while this check requires the
    mechanism message itself and goes red under the same mutation."""
    db_path = Path(str(tmp_path)) / "oracle.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool, embedding_service=_RaisingEmbeddingService())
    store.add_memory(content="quarterly roadmap planning notes", category="task")

    handler = _RecordHandler()
    log = logging.getLogger("app.services.memory_store")
    log.addHandler(handler)
    try:
        query_embedding = await store.embed_query("quarterly roadmap")
        result = store.search_memories("quarterly roadmap", limit=5)
    finally:
        log.removeHandler(handler)
        pool.close_all()

    warnings_seen = [
        r.getMessage()
        for r in handler.records
        if r.levelno >= logging.WARNING
    ]
    assert any("Memory query embedding" in m for m in warnings_seen), (
        "the dense-degradation WARNING must be credited to the embed_query "
        "mechanism (message fragment 'Memory query embedding'); the "
        "never-attempted tripwire 'Memory dense search skipped: ...' names "
        "no mechanism and must not be able to satisfy this oracle on its "
        f"own; WARNINGs captured: {warnings_seen}"
    )
    assert query_embedding is None, (
        "the failed precompute must return None (the attempted-and-failed "
        "encoding), not a value"
    )
    assert len(result) == 1, (
        f"FTS must still serve the row after the dense failure; got {result}"
    )


def test_fail_open_wiring_requires_a_live_call_path(tmp_path):
    """AC2 (#941): fail-open wiring means a LIVE production call path, not
    token presence. The frozen scan survived the delete-all-three-calls
    mutation because the call sites live in the one file it excludes; this
    check AST-binds the ``record_evaluator_fail_open`` call sites in
    retrieval_evaluator.py (>= 3), the recorder definition in telemetry.py,
    and the non-evaluator reader that binds the counter into the /metrics
    exposition — deleting the call sites unwires the metric and turns this
    red."""
    evaluator_path = _SERVICES_DIR / "retrieval_evaluator.py"
    telemetry_path = _SERVICES_DIR / "telemetry.py"
    evaluator_tree = ast.parse(
        evaluator_path.read_text(encoding="utf-8"), filename=str(evaluator_path)
    )
    call_sites = [
        node.lineno
        for node in ast.walk(evaluator_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record_evaluator_fail_open"
    ]
    assert len(call_sites) >= 3, (
        "the production fail-open call sites live in retrieval_evaluator.py "
        "(exception / empty_response / unexpected_response); a live call "
        f"path requires them — found {len(call_sites)} at lines {call_sites}"
    )

    telemetry_tree = ast.parse(
        telemetry_path.read_text(encoding="utf-8"), filename=str(telemetry_path)
    )
    assert any(
        isinstance(node, ast.FunctionDef)
        and node.name == "record_evaluator_fail_open"
        for node in ast.walk(telemetry_tree)
    ), "telemetry.py must define record_evaluator_fail_open (the recorder)"

    # The non-evaluator reader: telemetry's exposition binds the counter
    # into the /metrics text under the fail-open metric name.
    assert telemetry_module.METRIC_EVALUATOR_FAIL_OPEN == (
        "ragapp_retrieval_evaluator_fail_open_total"
    ), "the exposition metric symbol must exist under its documented name"
    tel = telemetry_module.Telemetry(
        enabled=True, registry_dir=Path(str(tmp_path)) / "telreg"
    )
    tel.record_evaluator_fail_open("exception")
    assert (
        'ragapp_retrieval_evaluator_fail_open_total{reason="exception"} 1'
        in tel.metrics_text()
    ), (
        "a recorded fail-open must reach the metrics exposition — the "
        "recorder-to-exposition read path is the wiring being pinned"
    )


def _event_loop_construction_hits(tree) -> list:
    """Line numbers of Calls that construct or enter a fresh event loop by
    ANY import spelling: ``<asyncio-alias>.run/new_event_loop/Runner``,
    from-imported ``run/new_event_loop/run_until_complete/Runner``, any
    ``....run_until_complete(...)`` (loop method — covers the chained
    ``new_event_loop().run_until_complete`` shape), and
    ``self._run_coro_sync(...)``."""
    asyncio_module_names = set()
    from_asyncio_names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "asyncio":
                    asyncio_module_names.add(
                        alias.asname or alias.name.split(".")[0]
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "asyncio":
                for alias in node.names:
                    from_asyncio_names[alias.asname or alias.name] = alias.name
    banned_attrs = {"run", "new_event_loop", "Runner"}
    banned_from_imports = {"run", "new_event_loop", "run_until_complete", "Runner"}
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "run_until_complete":
                # A loop method by construction: flagged regardless of what
                # the receiver expression is (alias name or chained call).
                hits.append(node.lineno)
            elif func.attr in banned_attrs:
                if isinstance(func.value, ast.Name) and (
                    func.value.id in asyncio_module_names
                ):
                    hits.append(node.lineno)
                elif isinstance(func.value, ast.Call):
                    # Chained construction, e.g. asyncio.Runner(...).run(...)
                    hits.append(node.lineno)
            elif (
                func.attr == "_run_coro_sync"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                hits.append(node.lineno)
        elif isinstance(func, ast.Name):
            if (
                func.id in from_asyncio_names
                and from_asyncio_names[func.id] in banned_from_imports
            ):
                hits.append(node.lineno)
    return hits


def test_memory_store_has_no_event_loop_construction_of_any_spelling():
    """AC3 (#941): memory_store.py must contain ZERO fresh-event-loop
    constructions under ANY spelling. The census matcher resolves the
    module's import aliases to the asyncio module first, so ``import
    asyncio as aio; aio.run(...)`` and ``from asyncio import run; run(...)``
    are caught exactly like the plain ``asyncio.run(...)`` spelling, along
    with ``new_event_loop``/``run_until_complete``/``Runner`` and
    ``self._run_coro_sync``. The matcher's own teeth are proven inline on
    in-memory snippets (a matcher self-test keeps the guard honest); it
    stays one-file on purpose — the five non-memory asyncio.run sites were
    dispositioned out with per-site mechanisms in #712's recurrence sweep.
    """
    # --- matcher self-test: every evasion spelling MUST be flagged -------
    alias_snippet = (
        "import asyncio as aio\n"
        "async def main():\n"
        "    pass\n"
        "aio.run(main())\n"
    )
    assert _event_loop_construction_hits(ast.parse(alias_snippet)) != [], (
        "matcher self-test: an aliased aio.run(...) must be flagged"
    )
    from_import_snippet = (
        "from asyncio import run\n"
        "async def main():\n"
        "    pass\n"
        "run(main())\n"
    )
    assert _event_loop_construction_hits(ast.parse(from_import_snippet)) != [], (
        "matcher self-test: a from-imported run(...) must be flagged"
    )
    chained_snippet = (
        "import asyncio\n"
        "async def main():\n"
        "    pass\n"
        "asyncio.new_event_loop().run_until_complete(main())\n"
    )
    assert _event_loop_construction_hits(ast.parse(chained_snippet)) != [], (
        "matcher self-test: chained new_event_loop().run_until_complete "
        "must be flagged"
    )
    runner_snippet = "from asyncio import Runner\nloop = Runner()\n"
    assert _event_loop_construction_hits(ast.parse(runner_snippet)) != [], (
        "matcher self-test: a from-imported Runner construction must be flagged"
    )
    coro_sync_snippet = (
        "class A:\n"
        "    def g(self):\n"
        "        return self._run_coro_sync(1)\n"
    )
    assert _event_loop_construction_hits(ast.parse(coro_sync_snippet)) != [], (
        "matcher self-test: self._run_coro_sync(...) must be flagged"
    )
    # --- matcher self-test: unrelated calls must NOT be flagged ----------
    benign_snippet = (
        "import asyncio\n"
        "import other\n"
        "async def main():\n"
        "    await asyncio.sleep(0)\n"
        "other.run(main)\n"
    )
    assert _event_loop_construction_hits(ast.parse(benign_snippet)) == [], (
        "matcher self-test: asyncio.sleep and a non-asyncio module's run "
        "must not be flagged"
    )

    source_path = _SERVICES_DIR / "memory_store.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"), filename=str(source_path)
    )
    hits = _event_loop_construction_hits(tree)
    assert hits == [], (
        "memory_store.py must never construct or enter a fresh event loop "
        f"under ANY spelling (alias import, from-import, Runner, "
        f"run_until_complete, _run_coro_sync); found hits at lines {hits}; "
        "await embed_query/embed_and_store on the caller's loop instead "
        "(issue #712 / #941)"
    )


def test_underscore_pin_discriminates_single_token_from_two_terms(tmp_path):
    """AC4 (#941): the underscore-joined query pin must actually
    DISCRIMINATE the single-token semantics from a two-AND-term
    interpretation. The joined row alone satisfies both; the CONTROL row
    ("separate foo and bar tokens here") carries the parts separately, so
    if "foo_bar" ever tokenized as two terms (foo* AND bar*) the control
    row would match too and the equality below would fail. Cross-reference:
    the pinned semantics themselves are #940's to affirm (AC1 there); this
    check tracks that decision."""
    db_path = Path(str(tmp_path)) / "underscore.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool)
    store.add_memory("the foo_bar integration endpoint", category="doc")
    store.add_memory("separate foo and bar tokens here", category="doc")
    try:
        results = store.search_memories("foo_bar", limit=5)
        contents = [r.content for r in results]
        assert contents == ["the foo_bar integration endpoint"], (
            "'foo_bar' is ONE \\w+ token: only the joined-token row may "
            f"match. The control row (separate 'foo' and 'bar' tokens) "
            f"matching here would mean the query was interpreted as two "
            f"AND terms; got {contents}"
        )
    finally:
        pool.close_all()


def _direct_json_dumps_calls(tree) -> list:
    """Line numbers of Call nodes invoking ``json.dumps`` directly."""
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dumps"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
        ):
            hits.append(node.lineno)
    return hits


def _has_dumps_seam_assignment(tree) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_dumps"
        for node in ast.walk(tree)
    )


def _dumps_seam_call_count(tree) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_dumps"
    )


def test_telemetry_serialization_seam_is_module_local():
    """AC5 (#941): telemetry must route every shard-serialization ``dumps``
    call through the module-local ``_dumps`` seam — the alias poisoned-
    serialization tests patch — with ZERO direct ``json.dumps`` calls in the
    module. Nothing pins the seam today: if the alias were dropped, the
    frozen flush test's hasattr fallback would silently degrade to mutating
    the stdlib ``json`` module process-wide (the exact mutation PRR-022
    flagged). The matcher's teeth are proven inline: a direct-call snippet
    is flagged by the same matcher that must stay at zero on the real
    module."""
    # --- matcher self-test: a direct stdlib call MUST be flagged ---------
    direct_snippet = "import json\nx = json.dumps({})\n"
    assert _direct_json_dumps_calls(ast.parse(direct_snippet)) != [], (
        "matcher self-test: a direct json.dumps call must be flagged"
    )
    seam_snippet = "import json\n_dumps = json.dumps\nx = _dumps({})\n"
    seam_tree = ast.parse(seam_snippet)
    assert _direct_json_dumps_calls(seam_tree) == [], (
        "matcher self-test: routing through the _dumps seam must not be "
        "flagged as a direct call"
    )
    assert _has_dumps_seam_assignment(seam_tree)
    assert _dumps_seam_call_count(seam_tree) >= 1

    source_path = _SERVICES_DIR / "telemetry.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8"), filename=str(source_path)
    )
    assert _has_dumps_seam_assignment(tree), (
        "telemetry.py must assign the module-local _dumps seam "
        "(`_dumps = json.dumps`) — it is the surface the poisoned-"
        "serialization tests patch instead of mutating the stdlib json module"
    )
    direct_hits = _direct_json_dumps_calls(tree)
    assert direct_hits == [], (
        "telemetry.py must contain ZERO direct json.dumps calls; every "
        "serialization must go through the module-local _dumps seam so no "
        f"test ever needs to mutate the stdlib json module; found direct "
        f"calls at lines {direct_hits}"
    )
    assert _dumps_seam_call_count(tree) >= 1, (
        "the _dumps seam must actually be used — an assigned-but-unused "
        "alias pins nothing"
    )
