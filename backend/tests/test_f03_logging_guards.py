"""Non-frozen logging guardrails for the #730/#731 trace (phase 4.2).

Two runtime guards the frozen set does not carry:

1. Behavioral RequestIdFilter-registration proof. Frozen check C10
   (test_518_telemetry.py) is a SOURCE pin — it greps lifespan.py for the
   filter wiring and would stay green even if the runtime path stopped
   attaching the filter. These guards drive the REAL production startup
   (app.main.app's lifespan, via TestClient) for BOTH arms documented in
   lifespan.py — no root handlers (fresh-handler arm) and a pre-existing
   handler (existing-handler arm) — and assert a RequestIdFilter instance is
   attached to the live root handler afterwards, plus SensitiveFieldFilter
   per the documented #981 chain.

2. Query redaction name-only scoping (#730 plan-critic r1): a mixed-case
   credential NAME redacts while a benign VALUE merely containing the
   credential-looking word in mixed case stays intact, and the existing
   lower-case exact pin keeps its byte shape.

Issue-tracer trace 730-731-access-log-truthfulness; this file is NOT part of
the frozen checkpoint manifest.
"""

import logging
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@contextmanager
def _root_logger_snapshot():
    """Snapshot the root logger's handlers/level and restore on exit."""
    logger = logging.getLogger()
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)


def _reset_lifespan_singletons():
    """Reset the process-global state a real lifespan run leaves behind.

    Known hazard set (the repo's real-lifespan lesson): the DB pool cache
    holds CLOSED pools after shutdown (second startup dies with
    "Connection pool has been closed"), the event buses bind to the first
    event loop, the background processor is a module singleton, and lifespan
    shutdown parks the auth CPU executor with nothing recreating it.
    """
    from app.models import database

    with database._pool_cache_lock:
        for pool in database._pool_cache.values():
            try:
                pool.close_all()
            except Exception:
                pass
        database._pool_cache.clear()

    from concurrent.futures import (
        ThreadPoolExecutor,  # noqa: E402 (function-local by design)
    )

    import app.services.auth_service as auth_service
    import app.services.background_tasks as background_tasks
    import app.services.draft_events as draft_events
    import app.services.wiki_events as wiki_events

    background_tasks._processor_instance = None
    wiki_events._bus = None
    draft_events._bus = None
    auth_service._auth_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="auth-cpu-guard"
    )


def _drive_production_startup():
    """Run app.main.app's real lifespan startup+shutdown via TestClient.

    The outbound-model SSRF validators are patched (the same seam
    test_518_telemetry.py patches) so startup does not depend on a live
    model URL; the logging-setup block under test runs before any of the
    patched services and is untouched by the patches.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.main import app

    _reset_lifespan_singletons()
    try:
        with (
            patch("app.services.embeddings.assert_url_safe"),
            patch("app.services.llm_client.assert_url_safe"),
            TestClient(app),
        ):
            pass
    finally:
        _reset_lifespan_singletons()


def test_request_id_filter_attached_fresh_handler_arm():
    """Fresh-handler arm: startup installs RequestIdFilter (+ redaction) on
    the handler it creates for an empty root logger."""
    from app.utils.request_context import RequestIdFilter

    with _root_logger_snapshot() as logger:
        logger.handlers = []
        _drive_production_startup()
        filters_attached = [f for h in logger.handlers for f in h.filters]
        assert any(
            isinstance(f, RequestIdFilter) for f in filters_attached
        ), (
            "no RequestIdFilter on the root handler after production startup "
            f"(fresh-handler arm): {[h.filters for h in logger.handlers]}"
        )
        assert any(
            type(f).__name__ == "SensitiveFieldFilter" for f in filters_attached
        ), (
            "no SensitiveFieldFilter on the root handler after production "
            f"startup (fresh-handler arm): {[h.filters for h in logger.handlers]}"
        )


def test_request_id_filter_attached_existing_handler_arm():
    """Existing-handler arm: startup attaches RequestIdFilter to a handler
    that already exists (the uvicorn/custom-config case in lifespan.py)."""
    from app.utils.request_context import RequestIdFilter

    with _root_logger_snapshot() as logger:
        pre = logging.StreamHandler()
        logger.handlers = [pre]
        _drive_production_startup()
        attached = [f for h in logger.handlers for f in h.filters]
        assert any(isinstance(f, RequestIdFilter) for f in attached), (
            "RequestIdFilter not attached to the pre-existing root handler "
            f"after production startup: {[h.filters for h in logger.handlers]}"
        )


def test_query_redaction_name_only_case_scoping():
    """#730 name-only scoping: mixed-case NAME redacts; a benign VALUE
    containing the credential-looking word (mixed case) stays intact; the
    lower-case exact pin keeps its byte shape."""
    from app.middleware.logging import _sanitize_query

    out = _sanitize_query("API_KEY=f01-guard-zzz&page=2")
    assert "f01-guard-zzz" not in out, f"mixed-case credential name must redact: {out!r}"
    assert "page=2" in out, f"non-credential parameters must survive: {out!r}"

    benign = _sanitize_query("note=API_KEY")
    assert benign == "note=API_KEY", (
        "a benign VALUE containing a credential-looking word (mixed case) "
        f"must stay intact under name-only redaction: {benign!r}"
    )

    pinned = _sanitize_query("api_key=f01-guard-zzz&mode=list")
    assert pinned == "api_key=[redacted]&mode=list", (
        f"lower-case exact pin drifted: {pinned!r}"
    )
