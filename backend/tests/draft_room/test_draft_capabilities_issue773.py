from __future__ import annotations

import asyncio

from app.api.routes import draft_room
from app.config import settings


def test_capabilities_emit_configured_poll_interval_and_job_budget() -> None:
    original_interval = settings.draft_poll_interval_seconds
    original_budget = settings.draft_job_max_model_calls
    try:
        settings.draft_poll_interval_seconds = 3.5
        settings.draft_job_max_model_calls = 27

        capabilities = asyncio.run(draft_room.get_capabilities(user={}))

        assert capabilities.limits["poll_interval_seconds"] == 3.5
        assert capabilities.limits["job_max_model_calls"] == 27
    finally:
        settings.draft_poll_interval_seconds = original_interval
        settings.draft_job_max_model_calls = original_budget
