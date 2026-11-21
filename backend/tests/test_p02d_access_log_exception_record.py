"""Companion coverage for the #1179 exception-path access record.

Full-record assertions on the 500 line (status, method, path, duration,
request_id through the production filter chain, sanitized query), a
CancelledError leg (no false 500), and a normal-route no-double-log
negative. NOT a frozen check file.
"""
import asyncio
import logging
import os
from unittest.mock import MagicMock

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.middleware.logging import LoggingMiddleware  # noqa: E402


def _capture_logger():
    return logging.getLogger("http")


def _records_for(logger):
    out = []
    class _Cap(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "http_request":
                out.append(record)
    h = _Cap()
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    return h, out


class TestExceptionAccessRecord:
    def test_uncaught_exception_emits_full_500_record(self):
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        @app.get("/boom")
        async def boom():
            raise RuntimeError("probe")

        logger = _capture_logger()
        handler, out = _records_for(logger)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/boom?secret=abc123&q=x")
            assert resp.status_code == 500
        finally:
            logger.removeHandler(handler)
        assert len(out) == 1, "exactly one http_request record"
        rec = out[0]
        assert rec.status_code == 500
        assert rec.method == "GET"
        assert rec.path == "/boom"
        assert rec.duration_ms >= 0
        assert rec.request_id, "request_id present through the contextvar"
        # query sanitized: the sensitive param is masked, not raw
        assert "abc123" not in str(rec.query)

    def test_cancelled_route_not_recorded_as_500(self):
        # Unit-level contract: TestClient converts cancellations into
        # responses (portal semantics), which cannot faithfully model a
        # disconnected client. Drive the middleware directly with a
        # call_next that raises CancelledError (BaseException on 3.11) and
        # assert it is NOT logged as a 500 and DOES propagate.
        from app.middleware.logging import LoggingMiddleware as LM

        async def _run():
            mw = LM(MagicMock())
            logger = _capture_logger()
            handler, out = _records_for(logger)

            async def call_next(request):
                raise asyncio.CancelledError()

            try:
                await mw.dispatch(MagicMock(method="GET", url=MagicMock(path="/x", query="")), call_next)
            except asyncio.CancelledError:
                propagated = True
            else:
                propagated = False
            finally:
                logger.removeHandler(handler)
            return propagated, out

        propagated, out = asyncio.run(_run())
        assert propagated, "CancelledError must propagate, not be swallowed"
        assert out == [], (
            "CancelledError (BaseException) must not be logged as a 500"
        )

    def test_normal_route_single_record_no_duplication(self):
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        @app.get("/ok")
        async def ok():
            return {"fine": True}

        logger = _capture_logger()
        handler, out = _records_for(logger)
        try:
            client = TestClient(app)
            resp = client.get("/ok")
            assert resp.status_code == 200
        finally:
            logger.removeHandler(handler)
        assert len(out) == 1
        assert out[0].status_code == 200
