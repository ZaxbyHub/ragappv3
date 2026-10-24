"""Supporting (non-frozen) tests for the #1009/#1008/#1005 incident fixes.

Part 2 of the support surface (tests/test_incident_resilience_support.py
covers #1010/#1011). Harness mirrors the frozen files:
tests/test_f2_deep_health_embed_capacity.py (httpx.MockTransport as the
embedding service's persistent client, TEI-mode settings namespace),
tests/test_f1_nltk_offline_bake.py (unstructured stub, log capture), and
tests/test_f4_email_metadata_snippet_bounds.py (route-function-direct
ranked search over a seeded FTS DB; census helpers from
tests/test_k7_snippet_guard_census.py).

Coverage:
* #1009 probe dialect validation (TEI / openai / modern-ollama shapes,
  rejects, MagicMock-safe int read, malformed-JSON probe raises);
* #1008 the zero-element NLTK diagnostic fires only for NON-EMPTY files
  (a genuinely empty file parses silently);
* #1005 email metadata snippet bounds: an oversized email_sender stays in
  the search budget, a small email_subject still yields a <mark>-marked
  excerpt, and the census sibling classifications (col 5, out-of-range
  col 99, unknown table, dynamic helper) hold.
"""

import contextlib
import logging
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.services.circuit_breaker import AsyncCircuitBreaker
from app.services.embeddings import EmbeddingError, EmbeddingService

# ---------------------------------------------------------------------------
# #1009 — deep-health probe dialect validation
# ---------------------------------------------------------------------------

TEI_BASE_URL = "http://localhost:8080"


def _tei_settings_namespace() -> SimpleNamespace:
    """The TEI-mode settings object patched over app.services.embeddings."""
    return SimpleNamespace(
        ollama_embedding_url=TEI_BASE_URL,
        embedding_model="BAAI/bge-m3",
        embedding_doc_prefix="",
        embedding_query_prefix="",
        redis_url=None,
        embedding_cache_ttl_seconds=3600,
        embedding_batch_size=512,
        embedding_batch_max_chars=131072,
        embedding_probe_batch_count=4,
        embedding_probe_batch_chars=32,
    )


@contextlib.asynccontextmanager
async def _tei_embed_service(handler):
    """Yield an EmbeddingService whose HTTP client is a MockTransport
    (the frozen f2 harness: settings namespace over the embeddings module,
    assert_url_safe bypassed, a fresh breaker)."""
    with (
        patch(
            "app.services.embeddings.settings", _tei_settings_namespace()
        ),
        patch("app.services.embeddings.assert_url_safe"),
        patch(
            "app.services.embeddings.embeddings_cb",
            AsyncCircuitBreaker(fail_max=50, reset_timeout=30, name="sup2"),
        ),
    ):
        service = EmbeddingService()
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service._client = mock_client
        try:
            yield service
        finally:
            await mock_client.aclose()


def _bare_service() -> EmbeddingService:
    """A no-init instance: _validate_probe_response touches only ``config``."""
    return object.__new__(EmbeddingService)


def test_validate_probe_response_accepts_every_dialect():
    """_validate_probe_response accepts TEI [[..]], openai
    {"data":[{"embedding":[...]}]}, modern ollama {"embeddings": [[..]]} and
    the compatibility {"embedding": [...]} shape."""
    tei = SimpleNamespace(mode="tei")
    service = _bare_service()
    for data in (
        [[0.5, 0.6]],
        [[0.1], [0.2], [0.3], [0.4]],
    ):
        service._validate_probe_response(data, tei)  # noqa: SLF001

    openai = SimpleNamespace(mode="openai")
    service._validate_probe_response(  # noqa: SLF001
        {"data": [{"embedding": [0.5, 0.6]}]}, openai
    )
    # Count parity is NOT required: a single representative embedding for a
    # batch payload is a valid answer.
    service._validate_probe_response(  # noqa: SLF001
        {"data": [{"embedding": [0.5]}]}, openai
    )

    modern = SimpleNamespace(mode="ollama", ollama_style="modern")
    service._validate_probe_response(  # noqa: SLF001
        {"embeddings": [[0.5, 0.6], [0.7, 0.8]]}, modern
    )
    service._validate_probe_response(  # noqa: SLF001
        {"embedding": [0.5, 0.6]}, modern
    )


def test_validate_probe_response_rejects_malformed_shapes():
    """{"bogus": 1} and [] are rejected in every dialect."""
    modern = SimpleNamespace(mode="ollama", ollama_style="modern")
    tei = SimpleNamespace(mode="tei")
    service = _bare_service()
    with pytest.raises(EmbeddingError):
        service._validate_probe_response({"bogus": 1}, modern)  # noqa: SLF001
    with pytest.raises(EmbeddingError):
        service._validate_probe_response([], tei)  # noqa: SLF001


def test_int_probe_setting_falls_back_on_non_int(monkeypatch):
    """A MagicMock-shaped settings object (non-int attribute) yields the
    documented default, never a synthesized Mock."""
    monkeypatch.setattr(
        "app.services.embeddings.settings",
        SimpleNamespace(embedding_probe_batch_count="not-an-int"),
    )
    assert (
        EmbeddingService._int_probe_setting(  # noqa: SLF001
            "embedding_probe_batch_count", 4
        )
        == 4
    )
    monkeypatch.setattr(
        "app.services.embeddings.settings",
        SimpleNamespace(embedding_probe_batch_count=True),  # bool is not int
    )
    assert (
        EmbeddingService._int_probe_setting(  # noqa: SLF001
            "embedding_probe_batch_count", 7
        )
        == 7
    )


async def test_embed_probe_raises_on_malformed_json_response():
    """A 200 whose body is not JSON raises EmbeddingError (never a raw
    httpx/json error escaping the probe)."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, text="<html>not json</html>", request=request)

    async with _tei_embed_service(handler) as service:
        with pytest.raises(EmbeddingError):
            await service.embed_probe(2.0)


# ---------------------------------------------------------------------------
# #1008 — zero-element diagnostic negative (genuinely empty file)
# ---------------------------------------------------------------------------


def _unstructured_auto_module():
    """Install the sibling-suite stub when unstructured is absent (the f1
    idiom): production's lazy ``from unstructured.partition.auto import
    partition`` resolves to the stub."""
    import sys
    import types

    auto = sys.modules.get("unstructured.partition.auto")
    if auto is not None and hasattr(auto, "partition"):
        return auto
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    unstructured.partition = partition_pkg
    sys.modules["unstructured"] = unstructured
    sys.modules["unstructured.partition"] = partition_pkg
    sys.modules["unstructured.partition.auto"] = auto
    return auto


def test_zero_byte_file_parses_silently_no_nltk_warning(
    tmp_path, monkeypatch, caplog
):
    """A GENUINELY EMPTY file that parses to zero elements must NOT trip the
    NLTK diagnostic — the warning is scoped to non-empty files whose parse
    came back empty (the missing-corpus signature)."""
    auto = _unstructured_auto_module()
    monkeypatch.setattr(auto, "partition", lambda *args, **kwargs: [], raising=False)

    from app.services.document_processor import DocumentParser

    empty = tmp_path / "sup2-empty.pdf"
    empty.write_bytes(b"")  # zero bytes: an honest empty document

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    module_logger = logging.getLogger("app.services.document_processor")
    handler = _Capture()
    module_logger.addHandler(handler)
    try:
        with caplog.at_level(
            logging.DEBUG, logger="app.services.document_processor"
        ):
            result = DocumentParser().parse(str(empty))
    finally:
        module_logger.removeHandler(handler)

    assert result == [], "harness: the stubbed empty parse returns []"
    combined = " ".join(captured + caplog.messages).lower()
    assert "nltk" not in combined and "tokenizer" not in combined, (
        "an empty file must parse silently (no NLTK diagnostic) — the "
        f"zero-element warning is non-empty-only; captured {captured!r}"
    )


# ---------------------------------------------------------------------------
# #1005 — email metadata snippet bounds (sender twin + census siblings)
# ---------------------------------------------------------------------------

_SUBJECT_SIZE = 800_000
_TIME_BUDGET_S = 5.0
_EXCERPT_BOUND = 10_000

_DENSE_TERMS = [
    "opmed",
    "cdp",
    "order",
    "medications",
    "permission",
    "health",
    "system",
    "row",
    "value",
    "status",
    "active",
]
_DENSE_QUERY = "opmed cdp order"


def _build_dense_text(size: int) -> str:
    """~size chars of dense-match text, token-index cycled (k7 builder)."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


async def _allow_policy(user, resource_type, resource_id, action):
    return True


async def test_oversized_email_sender_search_stays_in_budget():
    """The email_sender twin of the f4 C14 check: an 800K dense sender value
    must not wedge ranked search (the guarded snippet is skipped)."""
    from app.api.routes.documents import search_documents
    from app.models.database import init_db, run_migrations

    tmp = tempfile.mkdtemp(prefix="sup2_sender_")
    conn = None
    try:
        db_path = str(Path(tmp) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        seed = sqlite3.connect(db_path)
        try:
            seed.execute("INSERT INTO vaults (id, name) VALUES (101, 'S2V')")
            seed.execute(
                "INSERT INTO files "
                "(vault_id, file_path, file_name, file_size, file_type, "
                " status, email_sender) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    101,
                    "/sup2/mail.txt",
                    "sup2-mail.txt",
                    _SUBJECT_SIZE,
                    "txt",
                    "indexed",
                    _build_dense_text(_SUBJECT_SIZE),
                ),
            )
            seed.commit()
        finally:
            seed.close()

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        start = time.perf_counter()
        response = await search_documents(
            q=_DENSE_QUERY,
            vault_id=101,
            limit=20,
            offset=0,
            conn=conn,
            user={"id": 1, "username": "sup2-admin", "role": "superadmin"},
            evaluate=_allow_policy,
        )
        elapsed = time.perf_counter() - start

        assert response.total >= 1, (
            "harness: the oversized-sender file must match the metadata-arm "
            f"query, got total={response.total}"
        )
        assert elapsed < _TIME_BUDGET_S, (
            f"search over an {_SUBJECT_SIZE}-char dense email_sender took "
            f"{elapsed:.2f}s (budget {_TIME_BUDGET_S}s) — the sender snippet "
            "must sit behind the length guard like the subject"
        )
        for result in response.results:
            assert (
                result.excerpt is None
                or len(result.excerpt) < _EXCERPT_BOUND
            ), (
                "the metadata excerpt for an oversized email_sender must be "
                f"bounded, got {len(result.excerpt or '')} chars"
            )
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def test_small_email_subject_still_yields_marked_excerpt():
    """A small email_subject keeps the match-marked snippet: the guard only
    skips snippet() for OVER-ceiling columns."""
    from app.api.routes.documents import search_documents
    from app.models.database import init_db, run_migrations

    tmp = tempfile.mkdtemp(prefix="sup2_subject_")
    conn = None
    try:
        db_path = str(Path(tmp) / "app.db")
        init_db(db_path)
        run_migrations(db_path)

        seed = sqlite3.connect(db_path)
        try:
            seed.execute("INSERT INTO vaults (id, name) VALUES (102, 'S2W')")
            seed.execute(
                "INSERT INTO files "
                "(vault_id, file_path, file_name, file_size, file_type, "
                " status, email_subject) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    102,
                    "/sup2/subject-mail.txt",
                    "sup2-subject-mail.txt",
                    40,
                    "txt",
                    "indexed",
                    "opmed cdp order summary for the night shift",
                ),
            )
            seed.commit()
        finally:
            seed.close()

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        response = await search_documents(
            q=_DENSE_QUERY,
            vault_id=102,
            limit=20,
            offset=0,
            conn=conn,
            user={"id": 1, "username": "sup2-admin", "role": "superadmin"},
            evaluate=_allow_policy,
        )
        assert response.results, "harness: the small-subject file must match"
        assert any(
            result.excerpt and "<mark>" in result.excerpt
            for result in response.results
        ), (
            "a small (under-ceiling) email_subject must still produce a "
            "match-marked <mark> excerpt; got "
            f"{[r.excerpt for r in response.results]!r}"
        )
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_census_sibling_classifications():
    """The census tri-state on synthetic sites: unguarded col 5 (sender),
    out-of-range col 99, and an unknown table are all unguarded; the dynamic
    {col} helper form with the table marker in range is bounded-metadata."""
    import test_k7_snippet_guard_census as census

    def classify(snippet_expr: str):
        source = (
            "def _meta(self, conn):\n"
            "    return (\n"
            f"        \"SELECT {snippet_expr} AS meta_snippet \"\n"
            "        \"FROM files_search_fts WHERE files_search_fts "
            "MATCH ?\"\n"
            "    )\n"
        )
        sites = census.classify_source(
            "backend/app/api/routes/documents.py", source
        )
        assert sites, f"harness: the planted site must be found ({snippet_expr})"
        return {site.verdict for site in sites}

    # email_sender (col 5): stored uncapped — the table-name exemption must
    # NOT cover it without its own length guard.
    assert classify(
        "snippet(files_search_fts, 5, '<mark>', '</mark>', '…', 24)"
    ) == {"unguarded"}
    # Out-of-range literal index: unbindable site fails loud.
    assert classify(
        "snippet(files_search_fts, 99, '<mark>', '</mark>', '…', 24)"
    ) == {"unguarded"}
    # Unknown table: no column mapping, fails loud.
    assert classify(
        "snippet(other_fts, 0, '<mark>', '</mark>', '…', 24)"
    ) == {"unguarded"}
    # The dynamic-helper {col} form with the table marker within the
    # 40-char lookahead: the recognized bounded-metadata compatibility case.
    assert classify(
        "snippet(files_search_fts, {col}, '<mark>', '</mark>', '…', 24)"
    ) == {"bounded-metadata"}
