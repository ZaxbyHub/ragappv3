"""P03 lifecycle behavioral checks (issue #799: AC1/AC3 shutdown + degrade
class, plus pins). RED at base, GREEN after the fix, except the C15
preserving leg which must pass before AND after.

- C13 (AC1/AC2 behavioral): a residency task that DIES with a natural
  exception (editorial client construction failure) must not abort the
  shutdown sequence — EVERY later finalizer stage (processor stop,
  admission shutdown, vector-store close, db-pool close) must still run;
  the failure message enumerates which stages actually ran.
- C14 (AC3 sibling): a URLBlocked from the INSTANT client constructor must
  degrade the instant client to None instead of failing startup; the
  thinking client is returned untouched.
- C15 (preserving): a clean lifespan shutdown runs the recorded finalizer
  stages in the PRODUCTION order (email stop before processor stop before
  admission before store/pool close) — stable at base and after the fix.
- C16 (AC3 class pin for settings.py): every create_thinking_client() /
  create_instant_client() call in the settings late-activation path must sit
  inside a try whose handler set includes URLBlocked (ast check — kept
  structural to avoid route-fixture weight).

The real-lifespan tests mirror the proven recipe from
tests/test_health_readiness_lifespan.py (pinned settings on a temp data dir,
singleton resets, sacrificial auth executor, root-logging snapshot).
"""

import ast
import asyncio
import atexit
import logging
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
SETTINGS_ROUTE_PATH = BACKEND / "app" / "api" / "routes" / "settings.py"


# ── shared ast helpers ────────────────────────────────────────────────────


def _callee_name(call: ast.Call) -> str:
    """Best-effort name of a call target: ``foo()`` / ``mod.foo()``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _exception_type_names(handler_type) -> set[str]:
    """Names an except-clause type expression mentions."""
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


def _try_protections(tree: ast.Module) -> list[tuple[set[int], set[str]]]:
    """(try-body node ids, handler type names) for every Try in the tree."""
    protections: list[tuple[set[int], set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_ids: set[int] = set()
        for stmt in node.body:
            for child in ast.walk(stmt):
                body_ids.add(id(child))
        handler_names: set[str] = set()
        for handler in node.handlers:
            handler_names |= _exception_type_names(handler.type)
        protections.append((body_ids, handler_names))
    return protections


# ── real-lifespan pinning (minimal mirror of the neighbor recipe) ─────────


def _pin_lifespan_settings(tmp_path: Path, *, editorial: bool):
    """Pin every settings path/flag the real lifespan touches to a temp dir.

    Minimal local mirror of the proven recipe in
    tests/test_health_readiness_lifespan.py::_pin_lifespan_settings: stale
    pools closed, wiki/draft event buses and the background-processor
    singleton reset, a SACRIFICIAL auth executor handed to the lifespan
    shutdown, and the settings singleton pinned. The chat endpoint pairs are
    EMPTIED so no thinking/instant client (and no residency task for them)
    is constructed — C13's editorial task must be the ONLY residency task.
    With ``editorial=True`` the editorial pair is set so the lifespan's
    editorial branch (gated on ``settings.editorial_chat_url or
    settings.editorial_chat_model``) is taken.
    """
    from app.config import settings
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for pool in list(_pool_cache.values()):
            try:
                pool.close_all()
            except Exception:  # noqa: BLE001 - an already-closed pool is fine
                pass
        _pool_cache.clear()
    import app.services.background_tasks as _background_tasks
    import app.services.draft_events as _draft_events
    import app.services.wiki_events as _wiki_events

    _wiki_events._bus = None  # noqa: SLF001 - reset singleton for a fresh loop
    _draft_events._bus = None  # noqa: SLF001
    _background_tasks._processor_instance = None  # noqa: SLF001

    from app.services import auth_service as _auth_service

    saved_auth_executor = _auth_service._auth_executor
    _auth_service._auth_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="auth-cpu"
    )

    root_logger = logging.getLogger()
    saved_root_level = root_logger.level

    saved = {
        "data_dir": settings.data_dir,
        "redis_url": settings.redis_url,
        "users_enabled": settings.users_enabled,
        "admin_secret_token": settings.admin_secret_token,
        "jwt_secret_key": settings.jwt_secret_key,
        "ollama_embedding_url": settings.ollama_embedding_url,
        "allow_local_services": settings.allow_local_services,
        "ollama_chat_url": settings.ollama_chat_url,
        "chat_model": settings.chat_model,
        "instant_chat_url": settings.instant_chat_url,
        "instant_chat_model": settings.instant_chat_model,
        "editorial_chat_url": settings.editorial_chat_url,
        "editorial_chat_model": settings.editorial_chat_model,
    }
    settings.data_dir = tmp_path  # type: ignore[assignment]
    settings.redis_url = ""
    settings.users_enabled = False
    settings.admin_secret_token = "c13-pin-token-0123456789abcdef0123456"
    settings.jwt_secret_key = "c13-pin-jwt-0123456789abcdef0123456789abcd"
    settings.ollama_embedding_url = "http://127.0.0.1:9/v1/embeddings"
    settings.allow_local_services = True  # type: ignore[assignment]
    settings.ollama_chat_url = ""
    settings.chat_model = ""
    settings.instant_chat_url = ""
    settings.instant_chat_model = ""
    settings.editorial_chat_url = (
        "http://127.0.0.1:9/v1" if editorial else saved["editorial_chat_url"]
    )
    settings.editorial_chat_model = (
        "c13-editorial-model" if editorial else saved["editorial_chat_model"]
    )

    def restore():
        import contextlib

        with contextlib.suppress(Exception):
            root_logger.setLevel(saved_root_level)
        for key, value in saved.items():
            with contextlib.suppress(Exception):
                setattr(settings, key, value)
        from app.services import auth_service as _auth_service

        try:
            if _auth_service._auth_executor is not saved_auth_executor:
                _auth_service._auth_executor.shutdown(wait=False)
        finally:
            _auth_service._auth_executor = saved_auth_executor

    return restore


def _snapshot_root_handlers():
    root = logging.getLogger()
    return [(h, list(h.filters)) for h in root.handlers]


def _restore_root_handlers(saved_handlers):
    root = logging.getLogger()
    root.handlers = [h for h, _ in saved_handlers]
    for handler, filters in saved_handlers:
        handler.filters = list(filters)


# Production order of the instrumented lifespan shutdown stages
# (backend/app/lifespan.py ~:1201-1308): email stop -> residency/memory
# cancels -> file_watcher stop -> processor stops (background -> wiki ->
# draft -> kms) -> admission shutdown (+ store close) -> client closes ->
# vector-store close -> upload-commit drain -> turn-finalizer drain ->
# db-pool close.
SHUTDOWN_STAGE_ORDER = [
    "email_stop",
    "file_watcher_stop",
    "background_stop",
    "wiki_stop",
    "draft_stop",
    "kms_stop",
    "admission_shutdown",
    "admission_store_close",
    "vector_store_close",
    "upload_commit_drain",
    "db_pool_close",
]


class _SiblingStopDouble:
    """Minimal no-op stand-in for a sibling processor whose app.state slot
    the pinned run left None (failed or disabled start): wiring the double
    keeps that sibling's stop stage RUNNING and recorded, per the
    conditional-when-present contract."""

    async def stop(self, *args, **kwargs):
        return None


def _instrument_lifespan_shutdown_stages(app, stages: list[str]):
    """Wrap the lifespan shutdown stages on the LIVE app.state so a run
    records them in production order. Every wrapper calls through to the
    original, so the app still shuts down for real; the admission
    controller is instrumented in place (the lifespan's shutdown block
    calls the module-singleton get_admission_controller()).

    Returns an undo callable restoring every in-place wrap — load-bearing
    for the wraps that outlive the app object (the admission
    module-singleton controller and the pool-cached db_pool).
    """
    undos: list = []

    def wrap(obj, attr_name: str, stage: str, *, is_async: bool) -> None:
        original = getattr(obj, attr_name)
        if is_async:
            async def recording(*args, **kwargs):
                stages.append(stage)
                return await original(*args, **kwargs)
        else:
            def recording(*args, **kwargs):
                stages.append(stage)
                return original(*args, **kwargs)
        setattr(obj, attr_name, recording)
        undos.append(
            lambda o=obj, a=attr_name, orig=original: setattr(o, a, orig)
        )

    email_service = getattr(app.state, "email_service", None)
    if email_service is not None:
        wrap(email_service, "stop_polling", "email_stop", is_async=True)

    file_watcher = getattr(app.state, "file_watcher", None)
    if file_watcher is not None:
        wrap(file_watcher, "stop", "file_watcher_stop", is_async=True)

    processor = getattr(app.state, "background_processor", None)
    if processor is not None:
        wrap(processor, "stop", "background_stop", is_async=True)

    # Sibling processor stops (lifespan.py ~:1236-1241): background ->
    # wiki -> draft -> kms. A slot left None (failed/disabled start) gets
    # a minimal double so the stage still runs.
    for state_attr, stage in (
        ("wiki_compile_processor", "wiki_stop"),
        ("draft_job_processor", "draft_stop"),
        ("kms_compile_processor", "kms_stop"),
    ):
        sibling = getattr(app.state, state_attr, None)
        if sibling is None:
            setattr(app.state, state_attr, _SiblingStopDouble())
        wrap(getattr(app.state, state_attr), "stop", stage, is_async=True)

    from app.services import admission as admission_module

    controller = admission_module.get_admission_controller()
    wrap(controller, "shutdown", "admission_shutdown", is_async=True)
    store = getattr(controller, "store", None)
    if store is not None and getattr(store, "close", None) is not None:
        wrap(store, "close", "admission_store_close", is_async=True)

    vector_store = getattr(app.state, "vector_store", None)
    if vector_store is not None:
        # Sync: the lifespan calls app.state.vector_store.close() without
        # an await.
        wrap(vector_store, "close", "vector_store_close", is_async=False)

    # Async upload-commit drain gate (lifespan.py ~:1283-1286): the
    # lifespan imports drain_inflight_upload_commits at RUNTIME inside its
    # shutdown try block, so wrapping the MODULE attribute records each
    # call. Module-global wrap — restored by the undo callable.
    from app.api.routes import draft_room as draft_room_module

    wrap(
        draft_room_module,
        "drain_inflight_upload_commits",
        "upload_commit_drain",
        is_async=True,
    )

    db_pool = getattr(app.state, "db_pool", None)
    if db_pool is not None:
        # Sync: close_all() is called un-awaited by
        # _finalize_turns_and_close_pool.
        wrap(db_pool, "close_all", "db_pool_close", is_async=False)

    def undo() -> None:
        while undos:
            undos.pop()()

    return undo


# ── C13: residency task death must not skip later shutdown stages ────────


class TestResidencyTaskDeathDoesNotSkipLaterShutdownStages(unittest.TestCase):
    """A residency task that died with a natural exception must not abort
    shutdown: EVERY later finalizer stage — the four processor stops
    (background, wiki, draft, kms), admission shutdown, vector-store close,
    db-pool close — must still run."""

    def test_residency_task_death_does_not_skip_later_shutdown_stages(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        app = FastAPI()
        later_stages: list[str] = []
        instrumentation_undo: list = []
        raised: BaseException | None = None

        async def scenario():
            async with lifespan(app):
                instrumentation_undo.append(
                    _instrument_lifespan_shutdown_stages(app, later_stages)
                )
                # Give the event loop a tick so the freshly created
                # editorial residency task actually RUNS (and dies) before
                # shutdown begins — without an await here the task is
                # still PENDING at shutdown, rt.cancel() then yields the
                # (already handled) CancelledError instead of the
                # natural-death re-raise this check exists to expose.
                await asyncio.sleep(0.1)

        expected_stages = (
            "background_stop",
            "wiki_stop",
            "draft_stop",
            "kms_stop",
            "admission_shutdown",
            "vector_store_close",
            "upload_commit_drain",
            "db_pool_close",
        )
        saved_handlers = _snapshot_root_handlers()
        # Critic R3-5 hygiene: restore every module-global the test wraps
        # or rebinds — addCleanup for the pre-run wraps (root handlers,
        # pinned settings/sacrificial executor), and the finally below for
        # the mid-scenario instrumentation undo (the admission singleton
        # and pool-cached db_pool wraps cannot be registered before the
        # scenario creates them).
        self.addCleanup(_restore_root_handlers, saved_handlers)
        tmp_dir = Path(tempfile.mkdtemp(prefix="c13-residency-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        restore = _pin_lifespan_settings(tmp_dir, editorial=True)
        self.addCleanup(restore)
        try:
            with patch(
                "app.services.llm_client.create_editorial_client",
                side_effect=ValueError("editorial boom"),
            ):
                asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - the point of the check
            raised = exc
        finally:
            if instrumentation_undo:
                instrumentation_undo[0]()

        ran_all = all(stage in later_stages for stage in expected_stages)
        self.assertTrue(
            ran_all and raised is None,
            f"C13 later-stage-ran={ran_all} raised={raised!r} "
            f"stages-run={later_stages} expected={list(expected_stages)} — "
            "the editorial residency task died with a natural exception "
            "(create_editorial_client patched to raise ValueError) and the "
            "shutdown await of the dead task re-raised it, skipping every "
            "later shutdown stage (the processor stops and beyond: "
            "admission shutdown, vector-store close, pool close). The loop "
            "awaiting residency tasks must absorb any Exception.",
        )


# ── C14: instant-client URLBlocked must degrade ───────────────────────────


def test_instant_client_urlblocked_degrades():
    """A URLBlocked from the instant factory must degrade instant to None."""
    import app.lifespan as lifespan_module
    from app.services.ssrf import URLBlocked

    thinking_sentinel = object()
    raised: BaseException | None = None
    result = None
    try:
        with (
            patch.object(
                lifespan_module,
                "create_thinking_client",
                return_value=thinking_sentinel,
            ),
            patch.object(
                lifespan_module,
                "create_instant_client",
                side_effect=URLBlocked("host did not resolve"),
            ),
        ):
            result = lifespan_module._create_llm_clients()
    except BaseException as exc:  # noqa: BLE001 - the point of the check
        raised = exc

    degraded = (
        raised is None
        and isinstance(result, tuple)
        and len(result) == 2
        and result[0] is thinking_sentinel
        and result[1] is None
    )
    assert degraded, (
        f"C14 instant-degraded=False raised={raised!r} "
        f"result-type={type(result).__name__} — a URLBlocked from the "
        "instant client constructor (live-DNS assert_url_safe failure) "
        "must degrade the instant client to None and return the thinking "
        "client untouched, exactly like the thinking-side guard; startup "
        "must not fail on an environment-shaped error"
    )


# ── C15 (preserving): clean shutdown keeps the production stage order ─────


class TestStopFastDrainPreserving(unittest.TestCase):
    """A clean, unstalled lifespan shutdown (no failures, nothing slow)
    must run the recorded finalizer stages in the PRODUCTION order
    (lifespan.py ~:1201-1308: email stop before the processor stops —
    background, wiki, draft, kms — before admission before store/pool
    close). Passes at base and after the fix — the fix must change
    error/timing behavior, not the stage sequence of a healthy shutdown.
    (The processor-double fast-stop bound lives on as C12-FAST in
    test_lifecycle_drain_budget.py.)"""

    def test_stop_fast_drain_preserving(self):
        from fastapi import FastAPI

        from app.lifespan import lifespan

        app = FastAPI()
        stages: list[str] = []
        instrumentation_undo: list = []

        async def scenario():
            async with lifespan(app):
                instrumentation_undo.append(
                    _instrument_lifespan_shutdown_stages(app, stages)
                )

        saved_handlers = _snapshot_root_handlers()
        # Critic R3-5 hygiene (see the C13 twin for the split).
        self.addCleanup(_restore_root_handlers, saved_handlers)
        tmp_dir = Path(tempfile.mkdtemp(prefix="c15-order-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        restore = _pin_lifespan_settings(tmp_dir, editorial=False)
        self.addCleanup(restore)
        try:
            asyncio.run(scenario())
        finally:
            if instrumentation_undo:
                instrumentation_undo[0]()

        # The unconditional core a clean shutdown must always reach (the
        # sibling stops are guaranteed by the instrumentation's
        # wire-a-double-when-absent fallback); upload_commit_drain is a
        # CORE member so its body provably runs before the conditional
        # db_pool_close. The conditional stages (file_watcher_stop when a
        # watcher is wired, db_pool_close when the turn-finalizer drain
        # opens the gate, admission_store_close when the store exposes
        # close) are order-checked whenever they run.
        core = [
            "email_stop",
            "background_stop",
            "wiki_stop",
            "draft_stop",
            "kms_stop",
            "admission_shutdown",
            "vector_store_close",
            "upload_commit_drain",
        ]
        missing = [stage for stage in core if stage not in stages]
        self.assertEqual(
            missing,
            [],
            f"C15 stages-missing={missing} stages-run={stages} — a clean "
            "lifespan shutdown must run every core finalizer stage",
        )
        recorded_chain = [
            stage for stage in stages if stage in SHUTDOWN_STAGE_ORDER
        ]
        positions = [
            SHUTDOWN_STAGE_ORDER.index(stage) for stage in recorded_chain
        ]
        self.assertTrue(
            all(a < b for a, b in zip(positions, positions[1:])),
            f"C15 stage-order-violated positions={positions} "
            f"stages-run={stages} — the finalizer stages must keep the "
            "production order (email stop before the processor stops "
            "before admission before store/pool close)",
        )


# ── C16: settings late-activation URLBlocked containment (ast pin) ───────


def test_settings_late_activation_urlblocked_is_contained():
    """Every create_*_client() call in the settings late-activation path
    must sit inside a try whose handler set includes URLBlocked."""
    tree = ast.parse(
        SETTINGS_ROUTE_PATH.read_text(encoding="utf-8"),
        filename=str(SETTINGS_ROUTE_PATH),
    )
    protections = _try_protections(tree)
    factory_names = ("create_thinking_client", "create_instant_client")
    calls: list[str] = []
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name not in factory_names:
            continue
        calls.append(f"{name}@L{node.lineno}")
        guarded = any(
            id(node) in body_ids and "URLBlocked" in handler_names
            for body_ids, handler_names in protections
        )
        if not guarded:
            violations.append(f"{name}@L{node.lineno}")
    assert calls, (
        "C16 urlblocked-guarded=False factory-calls=0 — expected the "
        "create_thinking_client()/create_instant_client() late-activation "
        "call sites in backend/app/api/routes/settings.py; found none, so "
        "the anchor of this pin is missing"
    )
    assert not violations, (
        f"C16 urlblocked-guarded=False unguarded={violations} (of {calls}) "
        "— the settings late-activation try blocks catch only "
        "ModelNotConfiguredError, so a URLBlocked from the constructor's "
        "live-DNS assert_url_safe propagates and 500s the settings save; "
        "the handler set must include URLBlocked"
    )


if __name__ == "__main__":
    unittest.main()
