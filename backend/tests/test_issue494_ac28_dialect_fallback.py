"""Issue #494 acceptance check — AC28 / FU-001 (PRESERVING).

Pins the dialect-fallback chain of the REAL ``ModelChecker``
(``app/services/model_checker.py``, landed via PR #493) so it cannot regress.
Only the HTTP layer is mocked: every probe is served by ``httpx.MockTransport``
and recorded at the wire level (exact URL sequence + the per-request timeout
from ``request.extensions["timeout"]``), so ordering, counts, and the two-tier
timeout contract are asserted as observable behavior, not response shapes.

Pinned behavior (per the FU-001 acceptance criteria):

(a) HTTP 404 on the primary ollama dialect (``/api/tags``) falls through to the
    openai dialect (``/v1/models``), which lists the model -> ``available=True``
    with the exact probe sequence [base/api/tags, base/v1/models] (no /info).
(b) HTTP 405 on the primary dialect -> identical fall-through.
(c) HTTP 200 with a non-JSON body on the primary dialect -> identical
    fall-through.
(d) Per-base_url dialect cache: after a successful fallback the winning dialect
    is cached under the derived base URL, and a second availability check for
    the same base URL is a SINGLE probe of the winning dialect (the known-bad
    ollama dialect is never re-explored). The cached winner occupies the
    primary slot, so it is probed with the long ``timeout``.
(e) A transport timeout on the PRIMARY dialect is authoritative: the chain
    stops (no ``/v1/models`` and no ``/info`` probe follows) and the result is
    the mapped timeout error; a failed chain never populates the dialect cache.

Wire-level timeout contract: the first-tried dialect probes with
``timeout`` (10.0 s) while fallback probes use the shorter
``fallback_timeout`` (3.0 s) — asserted from the timeout extension captured
with every recorded request.

Must be GREEN at the pre-fix base a543361 (behavior already landed in #493).
"""

import os
import sys
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from app.services.model_checker import ModelChecker

# Distinct fake hosts per sub-case so dialect caches never bleed between them.
BASE_A = "http://ac28-a.test:11434"  # port 11434 -> heuristic ollama primary
BASE_B = "http://ac28-b.test:11434"
BASE_C = "http://ac28-c.test:11434"
BASE_E = "http://ac28-e.test:11434"

PRIMARY_TIMEOUT = 10.0
FALLBACK_TIMEOUT = 3.0
MODEL = "ac28-model"

OPENAI_MODELS_BODY = {
    "object": "list",
    "data": [{"id": MODEL}, {"id": "other-model"}],
}


def _recording_transport(log, primary_outcome):
    """MockTransport that records ``(url, connect-timeout)`` per probe.

    ``primary_outcome`` is called for every ``/api/tags`` probe and returns an
    ``httpx.Response`` or raises the exception it returns instead.
    ``/v1/models`` always answers with a listing containing MODEL; any other
    path fails the test (unexpected dialect probe).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout", {})
        log.append((str(request.url), timeout.get("connect")))
        path = request.url.path
        if path == "/api/tags":
            outcome = primary_outcome()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if path == "/v1/models":
            return httpx.Response(200, json=OPENAI_MODELS_BODY)
        raise AssertionError(f"unexpected dialect probe: {request.url}")

    return httpx.MockTransport(handler)


class TestIssue494AC28DialectFallbackChain(unittest.IsolatedAsyncioTestCase):
    """One node, sub-assertions (a)-(e); PRESERVING for PR #493 behavior."""

    async def _check(
        self,
        checker: ModelChecker,
        base_url: str,
        log: list,
        primary_outcome,
    ):
        transport = _recording_transport(log, primary_outcome)
        async with httpx.AsyncClient(transport=transport) as client:
            return await checker._check_model_availability(client, base_url, MODEL)

    async def test_dialect_fallback_chain(self) -> None:
        # ---- (a) 404 on primary ollama dialect -> openai dialect serves ----
        checker = ModelChecker(timeout=PRIMARY_TIMEOUT, fallback_timeout=FALLBACK_TIMEOUT)
        log_a: list = []
        result = await self._check(
            checker, BASE_A, log_a, lambda: httpx.Response(404, text="no such route")
        )
        self.assertEqual(result, {"available": True, "error": None})
        self.assertEqual(
            [entry[0] for entry in log_a],
            [f"{BASE_A}/api/tags", f"{BASE_A}/v1/models"],
            "exact probe sequence: ollama primary then openai fallback (no /info)",
        )
        self.assertEqual(
            [entry[1] for entry in log_a],
            [PRIMARY_TIMEOUT, FALLBACK_TIMEOUT],
            "primary probe uses timeout=10s; fallback probe uses the shorter "
            "fallback_timeout=3s (wire-level, from request.extensions)",
        )
        self.assertEqual(
            checker._dialect_cache,
            {BASE_A: "openai_compatible"},
            "winning dialect cached under the derived base URL",
        )

        # ---- (d) same base_url again: cached winner, single probe ----
        log_d: list = []
        result = await self._check(
            checker, BASE_A, log_d, lambda: httpx.Response(404, text="no such route")
        )
        self.assertEqual(result, {"available": True, "error": None})
        self.assertEqual(
            [entry[0] for entry in log_d],
            [f"{BASE_A}/v1/models"],
            "second check for the same base URL is single-probe: the known-bad "
            "ollama dialect is not re-explored",
        )
        self.assertEqual(
            log_d[0][1],
            PRIMARY_TIMEOUT,
            "cached winner occupies the primary slot (dialect order starts "
            "with the cached winner), so it probes with the long timeout",
        )

        # ---- (b) 405 on primary -> same fall-through (fresh checker) ----
        checker_b = ModelChecker(timeout=PRIMARY_TIMEOUT, fallback_timeout=FALLBACK_TIMEOUT)
        log_b: list = []
        result = await self._check(
            checker_b, BASE_B, log_b, lambda: httpx.Response(405, text="method not allowed")
        )
        self.assertEqual(result, {"available": True, "error": None})
        self.assertEqual(
            [entry[0] for entry in log_b],
            [f"{BASE_B}/api/tags", f"{BASE_B}/v1/models"],
        )
        self.assertEqual([entry[1] for entry in log_b], [PRIMARY_TIMEOUT, FALLBACK_TIMEOUT])
        self.assertEqual(checker_b._dialect_cache, {BASE_B: "openai_compatible"})

        # ---- (c) 200 with non-JSON body on primary -> same fall-through ----
        checker_c = ModelChecker(timeout=PRIMARY_TIMEOUT, fallback_timeout=FALLBACK_TIMEOUT)
        log_c: list = []
        result = await self._check(
            checker_c,
            BASE_C,
            log_c,
            lambda: httpx.Response(200, text="<html>upstream landing page</html>"),
        )
        self.assertEqual(result, {"available": True, "error": None})
        self.assertEqual(
            [entry[0] for entry in log_c],
            [f"{BASE_C}/api/tags", f"{BASE_C}/v1/models"],
        )
        self.assertEqual([entry[1] for entry in log_c], [PRIMARY_TIMEOUT, FALLBACK_TIMEOUT])
        self.assertEqual(checker_c._dialect_cache, {BASE_C: "openai_compatible"})

        # ---- (e) timeout on PRIMARY dialect stops the chain ----
        checker_e = ModelChecker(timeout=PRIMARY_TIMEOUT, fallback_timeout=FALLBACK_TIMEOUT)
        log_e: list = []
        result = await self._check(
            checker_e,
            BASE_E,
            log_e,
            lambda: httpx.ConnectTimeout("simulated connect timeout"),
        )
        self.assertEqual(
            result,
            {"available": False, "error": f"Request timed out after {PRIMARY_TIMEOUT}s"},
            "transport failure is authoritative: mapped timeout error, not a "
            "dialect-mismatch fall-through",
        )
        self.assertEqual(
            [entry[0] for entry in log_e],
            [f"{BASE_E}/api/tags"],
            "no further dialect probes after a transport failure (/v1/models "
            "and /info are never reached)",
        )
        self.assertEqual(
            log_e[0][1],
            PRIMARY_TIMEOUT,
            "the failing probe was the primary dialect (timeout slot 0)",
        )
        self.assertEqual(
            checker_e._dialect_cache,
            {},
            "a chain that never succeeded must not populate the dialect cache",
        )

        print(
            "PRESERVING GREEN: AC28/FU-001 dialect fallback chain — "
            "404/405/non-JSON fall through to /v1/models with exact URL "
            "sequence [api/tags, v1/models]; fallback probes carry "
            "fallback_timeout=3.0 on the wire; cached winner => single "
            "re-probe; primary-dialect timeout stops the chain authoritatively"
        )


if __name__ == "__main__":
    unittest.main()
