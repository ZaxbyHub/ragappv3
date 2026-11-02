"""Non-frozen logging guardrails for the #730/#731 trace.

Runtime guards the frozen set does not carry:

1. Behavioral filter-registration proof through the REAL production startup
   (app.main.app's lifespan via TestClient), run in a SUBPROCESS so the
   lifespan's process-global writes (pool cache, event buses, auth executor,
   root logging config) stay contained. Fresh-handler arm: startup creates
   the root handler and must attach RequestIdFilter + SensitiveFieldFilter
   with the JsonFormatter. Existing-handler arm (the uvicorn/gunicorn-with-
   log-config deployment shape): startup must attach BOTH filters to a
   pre-existing handler; startup deliberately keeps that handler's own
   formatter, so the formatter assertion is fresh-arm-only.

2. The child is HERMETIC (review PRR-001): it runs with a per-run temp
   DATA_DIR (never the shared backend/data tree), the in-memory limiter is
   forced, the test secrets are pinned explicitly (SET, not setdefault — an
   ambient shell value cannot leak into the child), the model-endpoint URLs
   are pinned to loopback-on-a-closed-port (literal IP: no DNS, instant
   connection refusal — no packet leaves the machine), and the
   assert_url_safe bindings (embeddings, llm_client, and app.lifespan's own
   module-level import) are patched in-child so the strict embedding-model
   startup check fails closed without contacting anything.

3. Filter identity is asserted by isinstance in-child (review PRR-009), not
   by class-name string.

4. Query redaction name-only case scoping (#730): a mixed-case credential
   NAME redacts; a benign VALUE containing the credential-looking word in
   mixed case stays intact; the lower-case exact pin keeps its byte shape.

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
from app.middleware.logging import SensitiveFieldFilter
from app.utils.request_context import RequestIdFilter

with (
    patch("app.services.embeddings.assert_url_safe"),
    patch("app.services.llm_client.assert_url_safe"),
    patch("app.lifespan.assert_url_safe"),
    TestClient(app),
):
    pass

filters = [f for h in logger.handlers for f in h.filters]
formatters = [h.formatter for h in logger.handlers if h.formatter is not None]
print(json.dumps({{
    "arm": ARM,
    "request_id_filter": any(isinstance(f, RequestIdFilter) for f in filters),
    "sensitive_field_filter": any(isinstance(f, SensitiveFieldFilter) for f in filters),
    "formatter_names": [type(fmt).__name__ for fmt in formatters],
}}))
"""


def _run_lifespan_arm(arm: str, data_dir: Path) -> dict:
    """Run the real lifespan startup+shutdown in a subprocess child and
    return its JSON report (filter identities + formatter names).

    Hermeticity (review PRR-001): the child never touches the shared
    backend/data tree — a per-run temp DATA_DIR is pinned (pydantic-settings
    maps the DATA_DIR env var onto Settings.data_dir), REDIS_URL is forced
    to the in-memory limiter, and the test secrets are SET explicitly
    (setdefault would let an ambient shell value win). Network hermeticity:
    the model-endpoint URLs are pinned to a loopback URL on a closed port —
    a literal IP needs no DNS and the connection refuses instantly, so no
    packet can leave the machine even with ambient model-endpoint vars in
    the parent environment (the delta reviewer proved getaddrinfo fires on
    the default embedding host through SSRFSafeTransport, which resolves
    independently of the patched assert_url_safe bindings); lifespan's
    startup swallows the resulting URLBlocked/verification failures by
    design (documented fail-open-on-validation path).
    """
    backend_root = str(Path(__file__).resolve().parents[1])
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DATA_DIR"] = str(data_dir)
    env["REDIS_URL"] = ""
    env["ADMIN_SECRET_TOKEN"] = "test-admin-key"
    env["USERS_ENABLED"] = "false"
    env["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    # Loopback pins: literal IP = no DNS; port 1 = instant refusal. These
    # SETs override any ambient model-endpoint vars, so NO ambient value can
    # cause the child to create an LLM client or resolve a host (the probe
    # classes the delta reviewers demonstrated on OLLAMA_EMBEDDING_URL and
    # INSTANT_CHAT_URL; the startup-reachable editorial pair is pinned too).
    env["OLLAMA_EMBEDDING_URL"] = "http://127.0.0.1:1/v1/embeddings"
    env["OLLAMA_CHAT_URL"] = "http://127.0.0.1:1/v1"
    env["RERANKER_URL"] = "http://127.0.0.1:1"
    env["INSTANT_CHAT_URL"] = "http://127.0.0.1:1/v1"
    env["INSTANT_CHAT_MODEL"] = "f03-hermetic-pin"
    env["DRAFT_EDITORIAL_CHAT_URL"] = "http://127.0.0.1:1/v1"
    env["DRAFT_EDITORIAL_CHAT_MODEL"] = "f03-hermetic-pin"
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
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_request_id_filter_attached_fresh_handler_arm(tmp_path):
    """Fresh-handler arm: startup creates the root handler and attaches
    RequestIdFilter + SensitiveFieldFilter with the JsonFormatter."""
    report = _run_lifespan_arm("fresh", tmp_path / "f03-fresh-data")
    assert report["request_id_filter"], report
    assert report["sensitive_field_filter"], report
    assert report["formatter_names"] == ["JsonFormatter"], report


def test_request_id_filter_attached_existing_handler_arm(tmp_path):
    """Existing-handler arm: startup attaches BOTH filters (RequestIdFilter
    and SensitiveFieldFilter) to a handler that already exists — the
    uvicorn/gunicorn-with-log-config deployment shape. Startup deliberately
    keeps the existing handler's own formatter, so the formatter check is
    fresh-arm-only."""
    report = _run_lifespan_arm("existing", tmp_path / "f03-existing-data")
    assert report["request_id_filter"], report
    assert report["sensitive_field_filter"], report


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
