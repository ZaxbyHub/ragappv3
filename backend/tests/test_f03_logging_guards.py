"""Non-frozen logging guardrails for the #730/#731 trace (phase 4.2).

Two runtime guards the frozen set does not carry:

1. Behavioral RequestIdFilter-registration proof, ISOLATED IN A SUBPROCESS.
   Frozen check C10 (test_518_telemetry.py) is a SOURCE pin — it greps
   lifespan.py for the filter wiring and would stay green even if the
   runtime path stopped attaching the filter. These guards drive the REAL
   production startup (app.main.app's lifespan, via TestClient) for BOTH
   arms documented in lifespan.py — no root handlers (fresh-handler arm)
   and a pre-existing handler (existing-handler arm) — and assert a
   RequestIdFilter instance (plus SensitiveFieldFilter, the #981 chain) is
   attached to the live root handler. The lifespan entry runs in a child
   process: entering the real lifespan in-process is the repo's documented
   xdist worker-killer class (closed-pool cache, loop-bound buses, parked
   auth executor, root-level leaks), and process isolation contains every
   one of those globals by construction.

2. Query redaction name-only case scoping (#730 plan-critic r1): a
   mixed-case credential NAME redacts while a benign VALUE merely
   containing the credential-looking word in mixed case stays intact, and
   the existing lower-case exact pin keeps its byte shape.

Issue-tracer trace 730-731-access-log-truthfulness; this file is NOT part of
the frozen checkpoint manifest.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_CHILD_DRIVER = r"""
import json
import logging
import sys

sys.path.insert(0, r"{backend_root}")

from unittest.mock import patch

ARM = sys.argv[1]

logger = logging.getLogger()
if ARM == "fresh":
    logger.handlers = []
else:
    logger.handlers = [logging.StreamHandler()]

from fastapi.testclient import TestClient

from app.main import app
from app.utils.request_context import RequestIdFilter

with (
    patch("app.services.embeddings.assert_url_safe"),
    patch("app.services.llm_client.assert_url_safe"),
    TestClient(app),
):
    pass

filters_attached = [
    type(f).__name__ for h in logger.handlers for f in h.filters
]
print(json.dumps({{
    "arm": ARM,
    "request_id_filter": any(n == "RequestIdFilter" for n in filters_attached),
    "sensitive_field_filter": any(
        n == "SensitiveFieldFilter" for n in filters_attached
    ),
    "filters": filters_attached,
}}))
"""


def _run_lifespan_arm(arm: str) -> dict:
    """Run the real lifespan startup+shutdown in a child process and return
    its JSON report on which filters ended up on the live root handler."""
    backend_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    # The child does not run under pytest/conftest, so provision the same
    # Settings validation env conftest.py sets (backend/tests/conftest.py).
    env.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
    env.setdefault("USERS_ENABLED", "false")
    env.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    env.setdefault("REDIS_URL", "")
    env["PYTHONPATH"] = backend_root
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_DRIVER.format(backend_root=backend_root), arm],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=backend_root,
    )
    assert result.returncode == 0, (
        f"lifespan driver ({arm}) failed rc={result.returncode}: "
        f"{result.stderr[-2000:]}"
    )
    line = result.stdout.strip().splitlines()[-1]
    return json.loads(line)


def test_request_id_filter_attached_fresh_handler_arm():
    """Fresh-handler arm: startup installs RequestIdFilter (+ redaction) on
    the handler it creates for an empty root logger."""
    report = _run_lifespan_arm("fresh")
    assert report["request_id_filter"], report
    assert report["sensitive_field_filter"], report


def test_request_id_filter_attached_existing_handler_arm():
    """Existing-handler arm: startup attaches RequestIdFilter to a handler
    that already exists (the uvicorn/custom-config case in lifespan.py)."""
    report = _run_lifespan_arm("existing")
    assert report["request_id_filter"], report


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
