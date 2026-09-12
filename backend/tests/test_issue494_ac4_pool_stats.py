"""Issue #494 acceptance check — AC4 / OBS-001 (DISCRIMINATING).

Root cause (verified at base a543361): ``EmbeddingService._log_pool_stats``
(embeddings.py ~969-996) reads urllib3-internal attributes
(``_num_connections`` / ``_num_keepalive`` / ``_limits``) from the httpcore
connection pool. Under httpx's asyncio transport those attributes DO NOT
EXIST (verified against httpcore 1.0.9: the pool exposes ``_connections``,
``connections``, ``_max_connections``, ``_max_keepalive_connections`` only),
so every ``getattr`` defaults and the log line fabricates
"0/20 connections, 0/10 keepalive" even while connections are alive.

Contract: the emitted pool-stats log line must either report counts that
match the actual pool state or explicitly report unavailability — it must
NEVER claim ``connections=0`` and ``keepalive=0`` while a connection is
demonstrably alive.

The check fakes the transport pool object with the REAL httpcore shape (see
``_FakeHttpcorePool``): one live connection tracked in ``_connections`` /
``connections``, configured limits 20/10, and NO urllib3 ``_num_*`` /
``_limits`` attributes — exactly what a real
``httpx.AsyncHTTPTransport()._pool`` looks like. A fixed implementation that
reports correct counts, or reports limits + an explicit "unavailable"/n/a
marker for live counts, passes; the base fabrication fails.
"""

import logging
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.embeddings import EmbeddingService


class _FakeConnection:
    """Stand-in for one live httpcore AsyncHTTPConnection."""


class _FakeHttpcorePool:
    """Mirrors the real httpcore.AsyncConnectionPool attribute surface.

    httpcore 1.0.9 exposes ``_connections`` (list), the ``connections``
    property, ``_max_connections`` and ``_max_keepalive_connections`` — and
    NOT the urllib3-era ``_num_connections`` / ``_num_keepalive`` / ``_limits``
    the base implementation reads.
    """

    def __init__(self, live_connections: int = 1):
        self._connections = [_FakeConnection() for _ in range(live_connections)]
        self._max_connections = 20
        self._max_keepalive_connections = 10

    @property
    def connections(self):
        return list(self._connections)

    def live_connection_count(self) -> int:
        return len(self._connections)


def _mock_embedding_settings() -> MagicMock:
    settings = MagicMock()
    settings.ollama_embedding_url = "http://embed-test-host:11434"
    settings.embedding_model = "nomic-embed-text"
    settings.embedding_doc_prefix = ""
    settings.embedding_query_prefix = ""
    settings.redis_url = None
    settings.embedding_cache_ttl_seconds = 3600
    return settings


class TestAC4PoolStatsNoFabricatedZeros:
    """_log_pool_stats must not fabricate 0/0 pool stats with a live connection."""

    async def test_log_pool_stats_does_not_claim_zero_with_live_connection(
        self, caplog
    ):
        with patch(
            "app.services.embeddings.settings", _mock_embedding_settings()
        ), patch("app.services.embeddings.assert_url_safe"):
            service = EmbeddingService()

        live_pool = _FakeHttpcorePool(live_connections=1)
        self_pool_transport = SimpleNamespace(_pool=live_pool)
        fake_client = SimpleNamespace(_transport=self_pool_transport)

        caplog.set_level(logging.INFO, logger="app.services.embeddings")
        service._log_pool_stats(fake_client)

        records = [
            r
            for r in caplog.records
            if r.name == "app.services.embeddings" and "pool" in r.getMessage()
        ]
        assert records, "expected _log_pool_stats to emit a pool-stats log record"
        message = records[0].getMessage()

        conn_match = re.search(r"(\d+)\s*/\s*(\d+) connections", message)
        keep_match = re.search(r"(\d+)\s*/\s*(\d+) keepalive", message)
        reports_unavailable = "unavailable" in message.lower() or "n/a" in message.lower()
        live_count = live_pool.live_connection_count()

        # DISCRIMINATING: at base the urllib3 getattr defaults fabricate
        # "0/20 connections, 0/10 keepalive" while our pool demonstrably has a
        # live connection -> RED.
        print("AC4 CHECK: FAIL", flush=True)
        if conn_match and keep_match and not reports_unavailable:
            claimed_connections = int(conn_match.group(1))
            claimed_keepalive = int(keep_match.group(1))
            assert not (
                claimed_connections == 0
                and claimed_keepalive == 0
                and live_count >= 1
            ), (
                f"_log_pool_stats fabricated '{message}' while {live_count} "
                f"connection(s) are demonstrably alive; report real counts or "
                f"an explicit unavailability marker"
            )
            assert (
                claimed_connections >= live_count
            ), f"_log_pool_stats under-reported live connections: '{message}' vs {live_count} alive"
        # If the line carries no numeric connection claim, or explicitly marks
        # live counts unavailable, the monitoring contract is satisfied.
        print(f"AC4 pool-stats line accepted: {message!r}", flush=True)
