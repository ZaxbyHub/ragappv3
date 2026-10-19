"""Acceptance checks for C13 live-eval persistence visibility and interop.

RED at base (master), GREEN after the fix:

- AC9 (TestPersistFailureVisible): when ``POST /api/eval/live`` finishes
  scoring but writing the run record raises, the route still returns plain
  200 success — ``LiveEvalAdapter._persist_run`` swallows every exception
  with ``logger.error`` and neither it nor the route surface a persistence
  signal in the response.
- AC10 (TestEvalCompareInterop): the JSONL record persisted by
  ``LiveEvalAdapter._persist_run`` (``RunResult.to_dict()``) is not
  loadable by the offline compare harness
  ``app.services.eval_compare.load_run_report`` — the record carries no
  ``environment`` key, so comparing a live run against stored baselines
  raises ``ValueError``.

These tests intentionally FAIL on the current tree. No production code is
modified here.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key-for-tests")
os.environ.setdefault("USERS_ENABLED", "False")

from fastapi.testclient import TestClient

from app.services import eval_compare
from app.services.eval_adapter import (
    BenchmarkItem,
    LiveEvalAdapter,
    RetrievedRanking,
)


class TestPersistFailureVisible(unittest.TestCase):
    """AC9 — a failed run-record write must not surface as plain success."""

    @contextmanager
    def _app_with_overrides(self):
        """Yield a TestClient with admin auth + a retrieval-capable fake engine.

        Mirrors backend/tests/test_eval_error_sanitization.py's override
        setup (admin user via ``get_current_active_user`` override, engine
        via ``get_rag_engine`` override). Unlike that suite — which patches
        ``run_live`` itself — the fake engine here is spec'd to
        ``query_retrieve_only`` only, so ``LiveEvalAdapter.run_live`` takes
        the legacy awaited path, scoring completes, and the run reaches
        persistence, where the write is made to fail.
        """
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app as main_app

        main_app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
            "is_active": True,
            "must_change_password": 0,
        }
        fake_engine = MagicMock(spec=["query_retrieve_only"])
        fake_engine.query_retrieve_only = AsyncMock(return_value=["doc1"])
        main_app.dependency_overrides[get_rag_engine] = lambda: fake_engine

        client = TestClient(main_app, raise_server_exceptions=False)
        try:
            yield client
        finally:
            main_app.dependency_overrides.clear()

    def test_persist_failure_is_not_silent_success(self):
        """AC9 — RED at base (master), GREEN after the fix.

        WHEN ``POST /api/eval/live`` completes scoring but writing the run
        record raises THE SYSTEM SHALL NOT report plain success: the
        response must be non-200 OR its body must carry
        ``persisted == False``.

        Base failure signature: ``_get_runs_dir`` is patched to a directory
        whose ``runs.jsonl`` is itself a directory, so the append-mode open
        raises OSError (IsADirectoryError on POSIX, PermissionError
        WinError 5 on Windows) — but ``_persist_run`` swallows it with
        ``except Exception: logger.error(...)``, the route returns 200 with
        no ``persisted`` key, and ``ok`` below evaluates to False:
        ``assert False is True``.
        """
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        poisoned_runs_dir = tmp / "eval-runs"
        poisoned_runs_dir.mkdir(parents=True)
        # Make the append target itself a directory so run_file.open("a") raises.
        (poisoned_runs_dir / "runs.jsonl").mkdir()

        with patch("app.config.settings.eval_enabled", True, create=True), patch(
            "app.services.eval_adapter._get_runs_dir",
            return_value=poisoned_runs_dir,
        ):
            with self._app_with_overrides() as client:
                resp = client.post(
                    "/api/eval/live",
                    json={
                        "benchmark": [
                            {
                                "id": "q1",
                                "query": "test query",
                                "relevant_ids": ["doc1"],
                            }
                        ],
                        "top_k": 3,
                    },
                )

        # Guard against a false pass: 501 would mean the eval-enabled patch
        # did not take effect, not that a persist failure was surfaced.
        self.assertNotEqual(resp.status_code, 501, "eval route unexpectedly disabled")

        ok = resp.status_code != 200 or resp.json().get("persisted") is False
        assert ok is True


class TestEvalCompareInterop(unittest.IsolatedAsyncioTestCase):
    """AC10 — a live-persisted run must be loadable by eval_compare."""

    async def test_live_run_loads_in_eval_compare(self):
        """AC10 — RED at base (master), GREEN after the fix.

        WHEN a run persisted by ``LiveEvalAdapter`` is passed to
        ``eval_compare.load_run_report`` THE SYSTEM SHALL load it without
        raising, so live runs can be cross-checked against stored
        baselines.

        Base failure signature: the persisted record
        (``RunResult.to_dict()``) has run_id/timestamp/release_id/top_k/
        query_metrics/means but no ``environment`` key, so the loader raises
        ``ValueError: run report ...: missing key 'environment'`` and the
        self.fail below fires with that text.

        Persistence is driven through the same seam as
        tests/eval/test_live_eval_adapter.py::TestPersistence::test_persist_run_writes_jsonl
        (patch the adapter module's ``settings.data_dir`` to a temp dir,
        then ``await adapter._persist_run(result)``).
        """
        import app.services.eval_adapter as adapter_module

        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        benchmark = [
            BenchmarkItem(id="q1", query="What is Python?", relevant_ids=["f1", "f3"])
        ]
        rankings = [RetrievedRanking(query_id="q1", retrieved_ids=["f2", "f1", "f3"])]

        with patch.object(adapter_module.settings, "data_dir", tmp_path):
            adapter = LiveEvalAdapter(top_k=3)
            result = adapter.score_run(
                benchmark, rankings, release_id="c13-ac10-rel"
            )
            await adapter._persist_run(result)

        runs_jsonl = tmp_path / "eval-runs" / "runs.jsonl"
        self.assertTrue(
            runs_jsonl.exists(), "adapter should have appended runs.jsonl"
        )

        try:
            report = eval_compare.load_run_report(runs_jsonl)
        except Exception as exc:
            self.fail(f"load_run_report raised {type(exc).__name__}: {exc}")

        self.assertIsNotNone(report)
        # RunReport carries run_id; it must round-trip the persisted run.
        if getattr(report, "run_id", None) is not None:
            self.assertEqual(report.run_id, result.run_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
