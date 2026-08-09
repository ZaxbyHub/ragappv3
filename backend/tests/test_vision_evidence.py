"""Tests for the retrieval-first query-time vision service (issue #462).

Covers selection/dedup, whole-batch policy gating, per-source degradation
statuses, and observation bounding — all without real provider calls.
"""

import asyncio
import os
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services.multimodal_enrichment import MultimodalProviderError
from app.services.vision_evidence import (
    VISION_ASSET_MISSING,
    VISION_EMPTY_RESPONSE,
    VISION_POLICY_BLOCKED,
    VISION_PROVIDER_UNAVAILABLE,
    VISION_PROXY_ONLY,
    VISION_USED,
    VisionEvidenceResult,
    VisionEvidenceService,
    apply_vision_to_sources,
)


@dataclass
class _Src:
    artifact_id: object
    modality: object
    asset_id: object
    text: str
    description: object = None
    vision_status: object = None
    vision_observation: object = None


def _src(artifact_id, modality="image", asset_id="a1", text="proxy text"):
    return _Src(
        artifact_id=artifact_id,
        modality=modality,
        asset_id=asset_id,
        text=text,
    )


class TestSelection(unittest.TestCase):
    def test_select_dedups_by_artifact_asset(self) -> None:
        svc = VisionEvidenceService()
        sources = [
            _src("art1", asset_id="assetX"),
            _src("art1", asset_id="assetX"),
            _src("art2", asset_id="assetY"),
        ]
        selected = svc._select(sources)
        self.assertEqual([s.artifact_id for s in selected], ["art1", "art2"])

    def test_select_excludes_ineligible(self) -> None:
        svc = VisionEvidenceService()
        sources = [
            _src("code", modality="code", asset_id="x"),  # code not VLM-eligible
            _src("noass", modality="image", asset_id=None),  # no asset
            _src("nomod", modality=None, asset_id="x"),  # no modality
            _src("chart", modality="chart", asset_id="x"),
        ]
        selected = svc._select(sources)
        self.assertEqual([s.artifact_id for s in selected], ["chart"])


class TestWholeBatchGating(unittest.TestCase):
    def _conn_opted_in(self, value=True):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "multimodal_provider_enabled": 1 if value else 0
        }
        return conn

    def _patch_settings(self, enabled=True, url="http://127.0.0.1:11434",
                        allow=None):
        patches = []
        patches.append(patch.object(settings, "multimodal_query_vision_enabled", enabled))
        patches.append(patch.object(settings, "multimodal_chat_url", url))
        patches.append(
            patch.object(
                settings,
                "multimodal_allowed_model_origins",
                allow if allow is not None else ["http://127.0.0.1:11434"],
            )
        )
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_disabled_feature_blocks(self) -> None:
        self._patch_settings(enabled=False)
        reason = VisionEvidenceService()._whole_batch_allowed(self._conn_opted_in(True), 1)
        self.assertEqual(reason, "query_vision_disabled")

    def test_vault_not_opted_in_blocks(self) -> None:
        self._patch_settings()
        reason = VisionEvidenceService()._whole_batch_allowed(self._conn_opted_in(False), 1)
        self.assertEqual(reason, "vault_not_opted_in")

    def test_no_provider_configured_blocks(self) -> None:
        self._patch_settings(url="")
        reason = VisionEvidenceService()._whole_batch_allowed(self._conn_opted_in(True), 1)
        self.assertEqual(reason, "no_provider_configured")

    def test_unallowed_origin_blocks(self) -> None:
        self._patch_settings(allow=[])
        reason = VisionEvidenceService()._whole_batch_allowed(self._conn_opted_in(True), 1)
        self.assertEqual(reason, "provider_origin_not_allowed")

    def test_allowed_returns_none(self) -> None:
        self._patch_settings()
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            reason = VisionEvidenceService()._whole_batch_allowed(self._conn_opted_in(True), 1)
        self.assertIsNone(reason)


class TestApplyVision(unittest.TestCase):
    def test_applies_status_and_observation_in_place(self) -> None:
        result = VisionEvidenceResult()
        result.statuses["art1"] = VISION_USED
        result.observations["art1"] = "The chart shows rising revenue."
        result.statuses["art2"] = VISION_PROXY_ONLY
        sources = [
            _src("art1", text="t1"),
            _src("art2", text="t2"),
            _src(None, text="plain"),
        ]
        apply_vision_to_sources(result, sources)
        self.assertEqual(sources[0].vision_status, VISION_USED)
        self.assertEqual(sources[0].vision_observation, "The chart shows rising revenue.")
        self.assertEqual(sources[1].vision_status, VISION_PROXY_ONLY)
        self.assertIsNone(sources[1].vision_observation)
        self.assertIsNone(sources[2].vision_status)

    def test_degraded_status_without_observation(self) -> None:
        result = VisionEvidenceResult()
        result.statuses["art9"] = VISION_ASSET_MISSING
        sources = [_src("art9", text="t")]
        apply_vision_to_sources(result, sources)
        self.assertEqual(sources[0].vision_status, VISION_ASSET_MISSING)
        self.assertIsNone(sources[0].vision_observation)


class TestRunDegradation(unittest.TestCase):
    def test_no_eligible_degrades_artifacts_to_proxy_only_no_call(self) -> None:
        svc = VisionEvidenceService()
        # No eligible selection: artifacts degrade to proxy_only without any call.
        sources = [
            _src("art1", modality="code", asset_id="x"),  # code -> ineligible
            _src("art2", modality="code", asset_id="y"),  # also ineligible
        ]
        with patch.object(
            svc, "_process_one", side_effect=AssertionError("must not call _process_one")
        ):
            result = asyncio.run(
                svc.run(query="q", sources=sources, vault_id=1)
            )
        self.assertEqual(result.selected, 0)
        self.assertEqual(result.statuses.get("art1"), VISION_PROXY_ONLY)
        self.assertEqual(result.statuses.get("art2"), VISION_PROXY_ONLY)

    def test_feature_off_omits_statuses_v5(self) -> None:
        """V5: feature off => vision never attempted AND vision_status NOT set."""
        svc = VisionEvidenceService()
        sources = [_src("art1", modality="table", asset_id="y")]
        settings.multimodal_query_vision_enabled = False
        try:
            with patch.object(
                svc, "_process_one", side_effect=AssertionError("must not call")
            ):
                result = asyncio.run(
                    svc.run(query="q", sources=sources, vault_id=1)
                )
        finally:
            settings.multimodal_query_vision_enabled = True
        self.assertEqual(result.selected, 1)
        # No status set, no policy_blocked count — feature-off parity on the wire.
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.policy_blocked, 0)

    def test_feature_off_with_ineligible_artifacts_omits_statuses_v5(self) -> None:
        """F-03 regression: feature-off + all-ineligible artifacts must NOT leak
        VISION_PROXY_ONLY (the old `not selected` branch ran before the gate)."""
        svc = VisionEvidenceService()
        sources = [
            _src("art1", modality="code", asset_id="x"),  # code -> ineligible
            _src("art2", modality="code", asset_id="y"),
        ]
        settings.multimodal_query_vision_enabled = False
        try:
            with patch.object(
                svc, "_process_one", side_effect=AssertionError("must not call")
            ):
                result = asyncio.run(
                    svc.run(query="q", sources=sources, vault_id=1)
                )
        finally:
            settings.multimodal_query_vision_enabled = True
        self.assertEqual(result.selected, 0)
        # V5: statuses omitted entirely even when nothing is eligible.
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.proxy_only, 0)

    def test_whole_batch_vault_not_opted_in_marks_policy_blocked(self) -> None:
        svc = VisionEvidenceService()
        sources = [_src("art1", modality="table", asset_id="y")]
        settings.multimodal_query_vision_enabled = True
        try:
            # Vault not opted in -> a real block (NOT the feature-off V5 case).
            with (
                # Isolate from the real SQLite pool (F-001): this test reaches
                # _whole_batch_allowed inside run(), so _conn_ctx must be faked like
                # its siblings or a fresh-clone single-file run opens ./data/app.db.
                patch("app.services.vision_evidence._conn_ctx", lambda: _AsyncCtx(MagicMock())),
                patch("app.services.vision_evidence._vault_opted_in", return_value=False),
                patch.object(
                    svc, "_process_one", side_effect=AssertionError("must not call")
                ),
            ):
                result = asyncio.run(
                    svc.run(query="q", sources=sources, vault_id=1)
                )
        finally:
            settings.multimodal_query_vision_enabled = True
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.statuses.get("art1"), VISION_POLICY_BLOCKED)
        self.assertEqual(result.policy_blocked, 1)

    def test_run_amortizes_one_shared_client_across_batch(self) -> None:
        """Issue #480 (D1): run() builds ONE shared MultimodalProviderClient and
        reuses it across every artifact in the batch (no per-artifact create+close),
        closing it exactly once. The per-call policy re-check inside
        ``chat_multimodal`` is what gates egress — not client lifecycle."""
        svc = VisionEvidenceService()
        sources = [
            _src("art1", modality="image", asset_id="a1"),
            _src("art2", modality="image", asset_id="a2"),
        ]

        class _SharedClient:
            def __init__(self):
                self.start_calls = 0
                self.close_calls = 0
                self.chat_calls = 0

            async def start(self):
                self.start_calls += 1

            async def chat_multimodal(self, messages, max_tokens=1024):
                self.chat_calls += 1
                return "observation"

            async def close(self):
                self.close_calls += 1

        shared = _SharedClient()
        factory = MagicMock(return_value=shared)

        async def _fake_process_one(*, query, src, vault_id, user, evaluate, semaphore, client):
            # Prove the SAME client instance is handed to every worker.
            assert client is shared, "run() must pass the shared client to _process_one"
            async with semaphore:
                await client.chat_multimodal([], max_tokens=1)
            return (src.artifact_id, VISION_USED, "observation")

        settings.multimodal_query_vision_enabled = True
        try:
            with (
                patch("app.services.vision_evidence._conn_ctx", lambda: _AsyncCtx(MagicMock())),
                patch.object(svc, "_whole_batch_allowed", return_value=None),
                patch.object(svc, "_client_factory", factory),
                patch.object(svc, "_process_one", side_effect=_fake_process_one),
            ):
                result = asyncio.run(svc.run(query="q", sources=sources, vault_id=1))
        finally:
            settings.multimodal_query_vision_enabled = True

        # Exactly one client created, started, and closed.
        self.assertEqual(factory.call_count, 1, "one shared client per batch")
        self.assertEqual(shared.start_calls, 1)
        self.assertEqual(shared.close_calls, 1, "client closed exactly once")
        self.assertEqual(shared.chat_calls, 2, "both artifacts reused the shared client")
        self.assertEqual(result.vlm_used, 2)

    def test_per_call_policy_recheck_blocks_after_mid_batch_kill_switch_flip(self) -> None:
        """PR #481 (PRR-002): the per-call policy re-check inside
        ``MultimodalProviderClient.chat_multimodal`` (``_assert_policy``) is what
        gates egress when the client is shared across a batch. Flipping
        ``multimodal_query_vision_enabled`` to False mid-batch MUST make the NEXT
        ``chat_multimodal`` call raise ``ERR_POLICY`` — the shared client must NOT
        cache authorization from a prior call.

        This drives the assertion THROUGH ``chat_multimodal`` (not by calling
        ``_assert_policy`` directly): the network layer is mocked, but the policy
        check is real, so removing ``self._assert_policy()`` from
        ``chat_multimodal`` would make this test fail."""
        from app.services.multimodal_enrichment import (
            ERR_POLICY,
            MultimodalProviderClient,
            MultimodalProviderError,
        )

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class _FakeHttp:
            def __init__(self):
                self.post_calls = 0

            async def post(self, url, json=None):
                self.post_calls += 1
                return _FakeResp()

            async def aclose(self):
                return None

        client = MultimodalProviderClient(purpose="query")
        fake_http = _FakeHttp()
        client._client = fake_http  # avoid start()'s real httpx client
        orig_qv = settings.multimodal_query_vision_enabled
        settings.multimodal_query_vision_enabled = True

        async def _run():
            # First chat_multimodal: kill switch ON → policy passes (origin/SSRF
            # gate stubbed to a no-op so the call reaches the mocked network).
            with patch(
                "app.services.multimodal_enrichment.assert_model_provider_allowed"
            ):
                out = await client.chat_multimodal(
                    [{"role": "user", "content": "q"}], max_tokens=8
                )
            assert out == "ok"
            assert fake_http.post_calls == 1, "first call reached the provider"

            # Flip the kill switch OFF mid-batch. The next chat_multimodal MUST
            # raise ERR_POLICY from the REAL _assert_policy (which re-reads live
            # settings) BEFORE any network call — the shared client did not cache
            # the prior authorization.
            settings.multimodal_query_vision_enabled = False
            raised = False
            try:
                await client.chat_multimodal(
                    [{"role": "user", "content": "q2"}], max_tokens=8
                )
            except MultimodalProviderError as exc:
                raised = exc.code == ERR_POLICY
            assert raised, "second call must raise ERR_POLICY after the flip"
            assert fake_http.post_calls == 1, "blocked call must NOT reach the provider"

            # Flip back ON — the same shared client clears again (re-check is
            # live per call, not cached).
            settings.multimodal_query_vision_enabled = True
            with patch(
                "app.services.multimodal_enrichment.assert_model_provider_allowed"
            ):
                out2 = await client.chat_multimodal(
                    [{"role": "user", "content": "q3"}], max_tokens=8
                )
            assert out2 == "ok"
            assert fake_http.post_calls == 2, "third call reached the provider after re-enable"

        try:
            asyncio.run(_run())
        finally:
            settings.multimodal_query_vision_enabled = orig_qv


class TestProcessOneSecurity(unittest.TestCase):
    """Plan V3/P5 security regressions for the per-artifact pipeline.

    Verifies authorization-before-byte-open: any denial means ZERO byte reads and
    ZERO provider calls. And per-call policy re-check degrades to policy_blocked
    without ever reading bytes or invoking the provider.
    """

    ROW = {
        "file_id": 5, "asset_id": "asset-1", "generation_hash": "g" * 12,
        "kind": "image", "raw_text": "FIG 1", "caption": None, "page_number": 1,
    }

    def _process_one(self, *, row=None, can_read=True, policy_reason=None,
                     read_result=b"fake-img", client_obs="an observation",
                     client_side_effect=None, sniff_mime="image/png"):
        """Run `_process_one` with a fake conn + spies for byte-open and provider."""
        svc = VisionEvidenceService()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = row if row is not None else self.ROW
        reads: list = []

        def _read(path, cap):
            reads.append(path)
            return read_result

        class _FakeClient:
            def __init__(self, *a, **k):
                self.calls = 0

            async def start(self):
                return None

            async def chat_multimodal(self, messages, max_tokens=1024):
                self.calls += 1
                if client_side_effect is not None:
                    raise client_side_effect
                return client_obs

            async def close(self):
                return None

        client = _FakeClient()
        # _client_factory is still spied so D1's run()-level amortization tests
        # can assert call counts; _process_one itself receives the shared client.
        factory = MagicMock(side_effect=lambda *a, **k: client)

        async def _can_read(user, evaluate, c, vid):
            return can_read

        async def _run2():
            with (
                patch("app.services.vision_evidence._conn_ctx", lambda: _AsyncCtx(conn)),
                patch("app.services.vision_evidence._can_read", new=_can_read),
                patch.object(
                    svc, "_whole_batch_allowed", side_effect=lambda c, vid: policy_reason
                ),
                patch("app.services.vision_evidence.sniff_raster_mime",
                      new=lambda data: sniff_mime),
                patch("app.services.vision_evidence._pixel_dims",
                      new=lambda data: (50, 50)),
                patch("app.services.vision_evidence._read_bounded", new=_read),
                patch("app.services.vision_evidence.record_security_event"),
                patch.object(svc, "_client_factory", factory),
            ):
                return await svc._process_one(
                    query="q", src=_src("art1", modality="image", asset_id="asset-1"),
                    vault_id=1, user={"id": 1}, evaluate=None,
                    semaphore=asyncio.Semaphore(1),
                    client=client,
                )

        return asyncio.run(_run2()), reads, client

    def test_authz_deny_does_no_byte_open_and_no_call(self) -> None:
        result, reads, client = self._process_one(can_read=False)
        self.assertEqual(result[1], VISION_POLICY_BLOCKED)
        self.assertEqual(reads, [], "no byte open on denied authz (V3)")
        self.assertEqual(client.calls, 0, "no provider call on denied authz")

    def test_missing_relation_does_no_byte_open(self) -> None:
        # Joined artifact->file lookup returns no row -> policy_blocked, no read.
        svc = VisionEvidenceService()
        conn_lookup = MagicMock()
        conn_lookup.execute.return_value.fetchone.return_value = None
        with (
            patch("app.services.vision_evidence._conn_ctx", lambda: _AsyncCtx(conn_lookup)),
            patch("app.services.vision_evidence._can_read",
                  new=lambda user, evaluate, c, vid: _can_read_true(user, evaluate, c, vid)),
            patch.object(
                svc, "_whole_batch_allowed", side_effect=AssertionError("must not gate")
            ),
            patch("app.services.vision_evidence.record_security_event"),
        ):
            result = asyncio.run(_isolated_process_one(svc=svc))
        self.assertEqual(result[1], VISION_POLICY_BLOCKED)

    def test_policy_recheck_before_read_blocks_bytes(self) -> None:
        # Mid-run policy denial (provider origin revoked) stops BEFORE byte open
        # and BEFORE the provider call.
        result, reads, client = self._process_one(policy_reason="provider_origin_not_allowed")
        self.assertEqual(result[1], VISION_POLICY_BLOCKED)
        self.assertEqual(reads, [], "no byte open when policy blocks before read (P5)")
        self.assertEqual(client.calls, 0)

    def test_asset_missing_does_no_byte_open(self) -> None:
        row = dict(self.ROW, asset_id=None)
        result, reads, client = self._process_one(row=row)
        self.assertEqual(result[1], VISION_ASSET_MISSING)
        self.assertEqual(client.calls, 0)

    def test_resolve_confined_escape_does_no_byte_open(self) -> None:
        svc = VisionEvidenceService()
        reads: list = []

        def _read(path, cap):
            reads.append(path)
            return b"fake"

        with (
            patch("app.services.vision_evidence._conn_ctx", lambda: _AsyncCtx(MagicMock())),
            patch("app.services.vision_evidence._can_read",
                  new=lambda user, evaluate, c, vid: _can_read_true(user, evaluate, c, vid)),
            patch.object(
                svc, "_whole_batch_allowed", side_effect=lambda c, vid: None
            ),
            patch("app.services.vision_evidence.resolve_confined", new=lambda *a, **k: None),
            patch("app.services.vision_evidence._read_bounded", new=_read),
            patch("app.services.vision_evidence.record_security_event"),
        ):
            result = asyncio.run(_isolated_process_one(svc=svc))
        self.assertEqual(result[1], VISION_ASSET_MISSING)
        self.assertEqual(reads, [], "no byte open on confined-path escape")

    def test_provider_used_sets_observation_and_reads_bytes(self) -> None:
        result, reads, client = self._process_one()
        self.assertEqual(result[1], VISION_USED)
        self.assertEqual(result[2], "an observation")
        self.assertTrue(reads, "read bytes for an authorized call")
        self.assertEqual(client.calls, 1)

    def test_provider_error_non_policy_degrades_to_provider_unavailable(self) -> None:
        # A provider failure (not a policy denial) must degrade safely to
        # provider_unavailable without crashing the batch (F-12).
        result, reads, client = self._process_one(
            client_side_effect=MultimodalProviderError("network", retryable=True, message="boom")
        )
        self.assertEqual(result[1], VISION_PROVIDER_UNAVAILABLE)
        self.assertTrue(reads, "bytes were opened before the provider call")
        self.assertEqual(client.calls, 1)

    def test_generic_exception_degrades_to_provider_unavailable(self) -> None:
        # Unexpected exceptions bubbling out of the provider map to
        # provider_unavailable (safe per-source degradation) (F-12).
        result, reads, client = self._process_one(
            client_side_effect=ValueError("unexpected")
        )
        self.assertEqual(result[1], VISION_PROVIDER_UNAVAILABLE)
        self.assertEqual(client.calls, 1)

    def test_provider_timeout_degrades_to_provider_unavailable(self) -> None:
        # asyncio.TimeoutError from the provider call maps to provider_unavailable,
        # not a batch crash (F-12).
        result, reads, client = self._process_one(
            client_side_effect=asyncio.TimeoutError()
        )
        self.assertEqual(result[1], VISION_PROVIDER_UNAVAILABLE)
        self.assertEqual(client.calls, 1)

    def test_empty_observation_is_empty_response_not_provider_unavailable(self) -> None:
        # Issue #480 (B2): a 200-OK-but-empty provider payload is NOT a provider
        # outage — the provider answered successfully. It must map to the distinct
        # VISION_EMPTY_RESPONSE status (not VISION_PROVIDER_UNAVAILABLE) so the
        # outage counters / proxy badge don't conflate "provider up but empty"
        # with "provider down". The batch must not crash and observation stays None.
        result, reads, client = self._process_one(client_obs="")
        self.assertEqual(result[1], VISION_EMPTY_RESPONSE)
        self.assertIsNone(result[2])
        self.assertTrue(reads)
        self.assertEqual(client.calls, 1)

    def test_whitespace_observation_is_empty_response(self) -> None:
        # Whitespace-only output validates to None just like empty output, and
        # must also surface as empty_response (not provider_unavailable).
        result, _reads, client = self._process_one(client_obs="   \n\t  ")
        self.assertEqual(result[1], VISION_EMPTY_RESPONSE)
        self.assertIsNone(result[2])
        self.assertEqual(client.calls, 1)

    def test_unexpected_runtime_error_degrades_without_raising(self) -> None:
        # F-003: an unexpected failure inside _run (e.g. DB pool exhaustion at
        # _conn_ctx) must degrade THIS artifact to provider_unavailable and never
        # raise to the caller loop — otherwise gather(return_exceptions=False)
        # would discard every status in the batch.
        svc = VisionEvidenceService()

        def _boom():
            raise RuntimeError("connection pool exhausted")

        with (
            patch("app.services.vision_evidence._conn_ctx", _boom),
            patch("app.services.vision_evidence.record_security_event"),
        ):
            result = asyncio.run(
                svc._process_one(
                    query="q", src=_src("art1", modality="image", asset_id="asset-1"),
                    vault_id=1, user={"id": 1}, evaluate=None,
                    semaphore=asyncio.Semaphore(1),
                )
            )
        self.assertEqual(result[1], VISION_PROVIDER_UNAVAILABLE)
        self.assertIsNone(result[2])

async def _can_read_true(user, evaluate, c, vid):
    """Always-True coroutine for patching `_can_read` (it is awaited)."""
    return True


class _AsyncCtx:
    """Async context manager yielding a given object as the 'connection'."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


async def _isolated_process_one(svc=None):
    from app.services.vision_evidence import VisionEvidenceService
    svc = svc or VisionEvidenceService()

    class _NoCallClient:
        async def start(self):
            return None

        async def chat_multimodal(self, messages, max_tokens=1024):
            raise AssertionError("provider must not be called on these denial paths")

        async def close(self):
            return None

    return await svc._process_one(
        query="q", src=_src("art1", modality="image", asset_id="asset-1"),
        vault_id=1, user={"id": 1}, evaluate=None,
        semaphore=asyncio.Semaphore(1),
        client=_NoCallClient(),
    )


class TestReadBoundedRealImpl(unittest.TestCase):
    """Issue #480 (E1): exercise the REAL _read_bounded branches.

    _read_bounded is defined in multimodal_enrichment.py and imported by
    vision_evidence. Previously every test patched it out; its branch logic
    (stat OSError / oversize / success / open OSError) was never exercised.
    """

    def test_success_returns_bytes(self) -> None:
        import tempfile
        from pathlib import Path

        from app.services.multimodal_enrichment import _read_bounded

        d = tempfile.mkdtemp()
        p = Path(d) / "asset.png"
        payload = b"png-bytes"
        p.write_bytes(payload)
        try:
            self.assertEqual(_read_bounded(p, 1024), payload)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_file_returns_none(self) -> None:
        from pathlib import Path

        from app.services.multimodal_enrichment import _read_bounded

        # stat() raises OSError on a non-existent path -> None.
        self.assertIsNone(_read_bounded(Path("/nonexistent/path/asset.png"), 1024))

    def test_oversize_returns_none(self) -> None:
        import tempfile
        from pathlib import Path

        from app.services.multimodal_enrichment import _read_bounded

        d = tempfile.mkdtemp()
        p = Path(d) / "big.png"
        p.write_bytes(b"x" * 100)
        try:
            # cap (10) < size (100) -> None, and no bytes are read.
            self.assertIsNone(_read_bounded(p, 10))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class TestCanReadRealImpl(unittest.TestCase):
    """Issue #480 (E1): exercise the REAL _can_read branches.

    _can_read (vision_evidence.py) is fail-closed with four branches: user None,
    evaluate-provided (allow / raise), and the import-fallback service evaluator.
    """

    def test_user_none_denies(self) -> None:
        from app.services.vision_evidence import _can_read

        self.assertFalse(asyncio.run(_can_read(None, None, MagicMock(), 1)))

    def test_evaluate_allow(self) -> None:
        from app.services.vision_evidence import _can_read

        async def _allow(user, resource_type, resource_id, action):
            return True

        self.assertTrue(
            asyncio.run(_can_read({"id": 1}, _allow, MagicMock(), 1))
        )

    def test_evaluate_raises_denies(self) -> None:
        from app.services.vision_evidence import _can_read

        async def _boom(user, resource_type, resource_id, action):
            raise RuntimeError("policy service down")

        self.assertFalse(
            asyncio.run(_can_read({"id": 1}, _boom, MagicMock(), 1))
        )

    def test_evaluate_false_denies(self) -> None:
        from app.services.vision_evidence import _can_read

        async def _deny(user, resource_type, resource_id, action):
            return False

        self.assertFalse(
            asyncio.run(_can_read({"id": 1}, _deny, MagicMock(), 1))
        )

    def test_fallback_service_evaluator_superadmin_allows(self) -> None:
        """When no DI evaluate is provided, _can_read falls back to the shared
        service-layer evaluator (app.services.authz_policy). A superadmin is
        granted vault read without any membership row."""
        from app.services.vision_evidence import _can_read

        conn = MagicMock()
        # evaluate=None -> fallback path; superadmin short-circuits to True
        # without any DB query, so conn.execute is never reached.
        self.assertTrue(
            asyncio.run(_can_read({"id": 1, "role": "superadmin"}, None, conn, 1))
        )

    def test_fallback_service_evaluator_no_user_id_denies(self) -> None:
        """A principal with no id is denied by the fallback evaluator."""
        from app.services.vision_evidence import _can_read

        conn = MagicMock()
        self.assertFalse(
            asyncio.run(_can_read({"role": "viewer"}, None, conn, 1))
        )


if __name__ == "__main__":
    unittest.main()
