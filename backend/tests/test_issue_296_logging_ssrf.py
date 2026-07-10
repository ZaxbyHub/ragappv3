"""Regression tests for issue #296 PR-A: logging/observability (#287) + SSRF (#286).

Each test exercises the specific behavior added/fixed and would fail on the
pre-fix code:

- RAGTrace.log() must NOT persist the raw user query (E3-1, HIGH).
- to_log_dict() redacts original_query; to_dict() keeps it for the
  in-response trace path.
- RAGTrace value-coverage for invalid_citations / answer_supported (C2-9).
- SensitiveFieldFilter scrubs record attributes by NAME, not message text
  (E3-3).
- RequestIdFilter + JsonFormatter emit request_id and extras as JSON (E3-2,
  E3-4).
- SSRFSafeTransport rejects a request whose freshly-resolved IP is private
  (B2-1), closing the DNS-rebinding TOCTOU gap while keeping SNI intact.
"""

import json
import logging
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.middleware.logging import SensitiveFieldFilter
from app.services.rag_trace import RAGTrace
from app.services.ssrf import URLBlocked
from app.utils.request_context import JsonFormatter, RequestIdFilter, request_id_var


class TestRAGTraceLogRedaction(unittest.TestCase):
    """E3-1: the logged form must never carry the raw user query."""

    def test_to_log_dict_redacts_original_query(self):
        t = RAGTrace(original_query="my secret salary is 12345")
        d = t.to_log_dict()
        self.assertNotIn("12345", d["original_query"])
        self.assertIn("redacted", d["original_query"])

    def test_to_dict_keeps_original_query_for_in_response_trace(self):
        # The in-response trace (behind rag_trace_in_response) must still be
        # able to surface the real query.
        t = RAGTrace(original_query="my secret salary is 12345")
        d = t.to_dict()
        self.assertEqual(d["original_query"], "my secret salary is 12345")

    def test_log_does_not_emit_raw_query(self):
        t = RAGTrace(original_query="VERY_UNIQUE_SECRET_TOKEN_xyz")
        with self.assertLogs("app.services.rag_trace", level="INFO") as cm:
            t.log()
        joined = "\n".join(cm.output)
        self.assertNotIn("VERY_UNIQUE_SECRET_TOKEN_xyz", joined)
        # The redacted length/hash form IS present.
        self.assertIn("redacted", joined)

    def test_to_log_dict_preserves_other_fields(self):
        t = RAGTrace(original_query="q", fused_hits=7, rerank_status="applied")
        d = t.to_log_dict()
        self.assertEqual(d["fused_hits"], 7)
        self.assertEqual(d["rerank_status"], "applied")


class TestRAGTraceValueCoverage(unittest.TestCase):
    """C2-9: assert serialized VALUES, not just key presence."""

    def test_invalid_citations_and_answer_supported_round_trip(self):
        t = RAGTrace(original_query="q")
        t.invalid_citations = ["S99", "M3"]
        t.answer_supported = False
        d = t.to_dict()
        self.assertEqual(d["invalid_citations"], ["S99", "M3"])
        self.assertIs(d["answer_supported"], False)

    def test_log_dict_also_carries_invalid_citations_values(self):
        t = RAGTrace(original_query="q")
        t.invalid_citations = ["S99"]
        t.answer_supported = True
        d = t.to_log_dict()
        self.assertEqual(d["invalid_citations"], ["S99"])
        self.assertIs(d["answer_supported"], True)


class TestSensitiveFieldFilter(unittest.TestCase):
    """E3-3: scrub sensitive record attributes by NAME."""

    def setUp(self):
        self.filt = SensitiveFieldFilter()

    def _make_record(self, **extra):
        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="some message", args=None, exc_info=None,
        )
        for k, v in extra.items():
            setattr(rec, k, v)
        return rec

    def test_scrubs_user_input_attribute(self):
        rec = self._make_record(user_input="my raw query")
        self.filt.filter(rec)
        self.assertEqual(rec.user_input, "[redacted]")

    def test_scrubs_api_key_attribute(self):
        rec = self._make_record(api_key="sk-live-12345")
        self.filt.filter(rec)
        self.assertEqual(rec.api_key, "[redacted]")

    def test_does_not_scrub_message_body_containing_keyword(self):
        # A message that merely contains the word "user_input" must NOT be
        # altered (no free-text message matching → no over-redaction).
        rec = self._make_record()
        rec.msg = "the field user_input should be validated"
        self.filt.filter(rec)
        self.assertEqual(rec.getMessage(), "the field user_input should be validated")


class TestJsonFormatterAndRequestId(unittest.TestCase):
    """E3-2 / E3-4: JSON output carries request_id and extras."""

    def test_json_formatter_emits_request_id_and_extras(self):
        token = request_id_var.set("req-abc")
        try:
            filt = RequestIdFilter()
            rec = logging.LogRecord(
                name="http", level=logging.INFO, pathname=__file__, lineno=1,
                msg="http_request", args=None, exc_info=None,
            )
            rec.method = "GET"
            rec.status_code = 200
            rec.duration_ms = 12.34
            filt.filter(rec)
            line = JsonFormatter().format(rec)
            payload = json.loads(line)
            self.assertEqual(payload["request_id"], "req-abc")
            self.assertEqual(payload["method"], "GET")
            self.assertEqual(payload["status_code"], 200)
            self.assertEqual(payload["duration_ms"], 12.34)
            self.assertEqual(payload["message"], "http_request")
        finally:
            request_id_var.reset(token)


class TestSSRFSafeTransport(unittest.TestCase):
    """B2-1: request-time re-validation blocks DNS rebinding to a private IP."""

    def test_rejects_request_resolving_to_private_ip(self):
        import httpx

        from app.services.ssrf_transport import SSRFSafeTransport

        transport = SSRFSafeTransport()
        request = httpx.Request("GET", "https://evil-rebind.example.com/v1/embeddings")

        # Simulate DNS rebinding: the guard saw a public IP earlier, but at
        # request time the hostname resolves to a private (RFC1918) address.
        with patch(
            "app.services.ssrf_transport._resolve_host_ips",
            return_value=["10.0.0.5"],
        ), patch(
            "app.services.ssrf_transport._local_services_opt_in_enabled",
            return_value=False,
        ):
            with self.assertRaises(URLBlocked):
                self._run_async(transport.handle_async_request(request))

    def test_allows_request_resolving_to_public_ip(self):
        import asyncio

        import httpx

        from app.services.ssrf_transport import SSRFSafeTransport

        transport = SSRFSafeTransport()
        request = httpx.Request("GET", "https://legit.example.com/v1/embeddings")
        inner = MagicMock()

        async def _fake_handle(req):
            inner(req)
            return httpx.Response(200, text="ok")

        transport._transport.handle_async_request = _fake_handle

        with patch(
            "app.services.ssrf_transport._resolve_host_ips",
            return_value=["93.184.216.34"],
        ), patch(
            "app.services.ssrf_transport._local_services_opt_in_enabled",
            return_value=False,
        ):
            resp = asyncio.new_event_loop().run_until_complete(
                transport.handle_async_request(request)
            )
        self.assertEqual(resp.status_code, 200)
        inner.assert_called_once()
        # SNI safety: the inner transport must receive the ORIGINAL request
        # with the hostname intact (not rewritten to a literal IP, which
        # would corrupt TLS SNI/cert validation for https endpoints).
        passed_request = inner.call_args[0][0]
        self.assertEqual(passed_request.url.host, "legit.example.com")

    def test_rejects_when_resolution_returns_empty(self):
        # Fail-closed: a transient DNS failure at re-validation must block the
        # request (matching the startup guard's posture), not slip past.
        import httpx

        from app.services.ssrf_transport import SSRFSafeTransport

        transport = SSRFSafeTransport()
        request = httpx.Request("GET", "https://flaky.example.com/v1/embeddings")

        with patch(
            "app.services.ssrf_transport._resolve_host_ips",
            return_value=[],
        ), patch(
            "app.services.ssrf_transport._local_services_opt_in_enabled",
            return_value=False,
        ):
            with self.assertRaises(URLBlocked):
                self._run_async(transport.handle_async_request(request))

    def test_local_services_opt_in_allows_private_ip(self):
        import asyncio

        import httpx

        from app.services.ssrf_transport import SSRFSafeTransport

        transport = SSRFSafeTransport()
        request = httpx.Request("GET", "http://local.model/v1/embeddings")

        async def _fake_handle(req):
            return httpx.Response(200, text="ok")

        transport._transport.handle_async_request = _fake_handle

        with patch(
            "app.services.ssrf_transport._resolve_host_ips",
            return_value=["127.0.0.1"],
        ), patch(
            "app.services.ssrf_transport._local_services_opt_in_enabled",
            return_value=True,
        ):
            resp = asyncio.new_event_loop().run_until_complete(
                transport.handle_async_request(request)
            )
        self.assertEqual(resp.status_code, 200)

    @staticmethod
    def _run_async(coro):
        import asyncio

        return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
