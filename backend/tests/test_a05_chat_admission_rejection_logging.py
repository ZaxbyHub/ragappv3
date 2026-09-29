"""Issue #687 acceptance check AC9 (frozen) — a non-stream admission
rejection must be a clean 503, never an "UNHANDLED EXCEPTION" log
(T1-28-K-07).

In ``chat`` (the non-stream route handler, chat.py ~L1729), the
``except AdmissionRejected`` handler raises ``HTTPException(status_code=503,
"chat admission rejected")`` at chat.py ~L1840 — from INSIDE the outer
``try:`` whose broad ``except Exception:`` (chat.py ~L1843) logs
``[chat] UNHANDLED EXCEPTION during chat processing`` with a full traceback
before re-raising. Every expected saturation 503 is therefore logged as an
unhandled error.

Driven path: the non-stream chat ROUTE handler (``chat``) is called
route-function-direct via its ``__wrapped__`` body — the slowapi rate-limit
decorator needs a live request context that is not under test here; the
scaffolding supplies minimal valid arguments so the handler reaches its
admission gate.
"""

import logging
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.routes import chat as chat_module
from app.api.routes.chat import ChatRequest
from app.services.admission import AdmissionRejected


class _RejectingController:
    """Stands in for get_admission_controller(): admission is saturated."""

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        raise AdmissionRejected("queue_full")
        yield  # pragma: no cover — unreachable; generator shape only


async def test_nonstream_admission_rejection_is_not_logged_as_unhandled(
    monkeypatch, caplog
):
    """AC9: when a non-streaming chat request is rejected by admission, the
    route must return 503 and must NOT log "[chat] UNHANDLED EXCEPTION".

    At base the 503 raised at chat.py ~L1840 is caught by the outer
    ``except Exception`` at chat.py ~L1843, which logs one
    "UNHANDLED EXCEPTION" record — so the caplog count assertion fails as
    ``assert 1 == 0``.
    """
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        chat_module, "get_admission_controller", lambda: _RejectingController()
    )

    async def allow_evaluate(*args, **kwargs):
        return True

    body = ChatRequest(message="hello")

    with pytest.raises(HTTPException) as exc_info:
        await chat_module.chat.__wrapped__(
            request=MagicMock(),
            body=body,
            rag_engine=MagicMock(),
            user={"id": 1, "username": "u", "role": "superadmin"},
            evaluate=allow_evaluate,
            _csrf_token="test-csrf",
            _=True,
        )

    assert exc_info.value.status_code == 503

    unhandled = [
        record
        for record in caplog.records
        if "UNHANDLED EXCEPTION" in record.getMessage()
    ]
    assert len(unhandled) == 0
