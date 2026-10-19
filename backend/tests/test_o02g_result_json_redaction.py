"""Issue #958 acceptance checks: the wiki compile ``result_json`` payload
must never carry raw exception message text from the curator/compiler sinks.

Three sibling sinks interpolate the exception MESSAGE into strings that
flow through ``CuratorResult.errors`` / the curator summary dict into
``WikiStore.complete_job(result_json=...)`` and out of the verbatim jobs
readers (GET /wiki/jobs, GET /wiki/jobs/{id}):

- ``wiki_curator.py``    ``f"transport_error: {type(e).__name__}: {e}"``
- ``wiki_compiler.py``   ``f"orchestration_error: {type(e).__name__}: {e}"``
- ``wiki_compiler.py``   ``f"persist_error: {type(e).__name__}: {e}"``

The adjacent lint path already redacts to class-only with an explicit
"reaches vault readers" comment — these three never got the same
discipline (PR #951's scope was the ``jobs.error`` column only).

Checks:

- AC13: ``WikiCurator.curate`` with a client whose ``propose`` raises a
  sentinel-bearing RuntimeError returns a ``to_summary()`` whose errors
  carry at most the exception class name and a stable code — sentinel
  absent, ``transport_error`` present (``RuntimeError`` class name
  allowed).
- AC14: ``WikiCompiler._maybe_run_curator`` with the module offload
  executor stubbed to raise (orchestration arm) returns summary errors
  containing ``orchestration_error`` but NOT the sentinel; and a compiler
  whose store ``attach_source`` raises (persist arm, driven through the
  real persist loop with a stubbed curator returning one accepted
  candidate) returns summary errors containing ``persist_error`` but NOT
  the sentinel.
- AC15 (NEW-SURFACE census guardrail): no ``{type(e).__name__}: {e}``
  interpolation may appear in ``wiki_curator.py`` / ``wiki_compiler.py``
  error-string constructions — count pinned at ZERO per file so a fourth
  sibling cannot be reintroduced.

RED at base for every check (all three sinks interpolate the raw message
verbatim; the census counts three sites); GREEN after the fix (class-name
+ stable code only).

Harness: temp ``run_migrations`` DBs for the compiler arms, direct
curator/compiler calls with the settings flags patched exactly like the
Phase-2 probes (P11/P12), a stubbed executor / stubbed curator class, and a
source census modeled on ``test_o02_guardrail_census.py``.
"""

import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest import mock

os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN", "o02g-token-0123456789abcdef0123456789abcdef"
)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_CURATOR = os.path.join(BACKEND, "app", "services", "wiki_curator.py")
WIKI_COMPILER = os.path.join(BACKEND, "app", "services", "wiki_compiler.py")

CURATOR_SENTINEL = "O02G-SENTINEL-CURATOR-TRANSPORT-RAW host=10.0.0.9"
ORCH_SENTINEL = "O02G-SENTINEL-ORCHESTRATION-RAW key=SECRET"
PERSIST_SENTINEL = "O02G-SENTINEL-PERSIST-RAW pw=hunter2"

# AC15 census pattern: the sibling raw-message interpolation shape
# (exception CLASS NAME followed by the raw exception MESSAGE inside an
# f-string) — the exact defect shape of all three #958 sinks.
_RAW_MSG_INTERP = re.compile(r"\{type\((e|exc)\)\.__name__\}\s*:\s*\{\1\}")


def _mkdb(prefix: str) -> str:
    from app.models.database import run_migrations

    tmp_dir = mkdtemp(prefix=prefix)
    db_path = str(Path(tmp_dir) / "o02g.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class TestCuratorTransportErrorClassOnly(unittest.IsolatedAsyncioTestCase):
    """AC13 (#958): the curator transport-error sink is class-name-only."""

    async def test_curator_transport_error_omits_message_text(self):
        # check: AC13 — RED at base, GREEN after the fix.
        from app.services.wiki_curator import CuratorChunk, WikiCurator

        class _Client:
            async def propose(self, _messages):
                raise RuntimeError(
                    f"conn refused {CURATOR_SENTINEL}"
                )

        class _Store:
            def find_similar_claims(self, *args, **kwargs):
                return []

        chunk = CuratorChunk(
            chunk_id="o02g-c1", source_text="probe text about a topic"
        )
        curator = WikiCurator(client=_Client(), store=_Store())
        result = await curator.curate(
            vault_id=1,
            file_id=1,
            chunks=[chunk],
            deterministic_summary=None,
            existing_entities_brief=[],
            deterministic_dedupe_keys=set(),
        )
        summary = result.to_summary()
        errors = summary.get("errors") or []
        self.assertTrue(
            errors, "the transport failure must still be recorded as an error"
        )
        self.assertTrue(
            any("transport_error" in e for e in errors),
            "the stable transport_error code must be present in the summary"
            f" errors (got {errors!r})",
        )
        self.assertNotIn(
            CURATOR_SENTINEL,
            json.dumps(summary),
            "the curator summary (the result_json payload) must never carry"
            " the raw exception message text (#958)",
        )


class TestCompilerSinksClassOnly(unittest.TestCase):
    """AC14 (#958): the orchestration and persist sinks are class-name-only."""

    def _compiler(self, db_path: str, store):
        import app.services.wiki_compiler as wc

        conn = _conn(db_path)
        compiler = wc.WikiCompiler(db=conn, store=store)
        return wc, conn, compiler

    def test_orchestration_error_omits_message_text(self):
        # check: AC14 (orchestration arm) — RED at base, GREEN after the
        # fix.
        db_path = _mkdb("o02g-orch-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)

        class _BoomExecutor:
            def submit(self, _func, *rest):
                # The production code discards the curator coroutine when
                # submit raises; close it here so the #565 never-awaited
                # gate stays quiet (the double's behavior, not the
                # assertion, is what changed).
                coro = rest[-1] if rest else None
                if coro is not None and hasattr(coro, "close"):
                    coro.close()
                raise RuntimeError(f"offload died {ORCH_SENTINEL}")

        wc, conn, compiler = self._compiler(db_path, None)
        try:
            store = wc.WikiStore(conn)
            compiler = wc.WikiCompiler(db=conn, store=store)

            class _Extraction:
                role_claims: list = []
                acronyms: list = []

            with mock.patch.object(
                wc, "_CURATOR_OFFLOAD_EXECUTOR", _BoomExecutor()
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_enabled", True
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_run_on_manual", True
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_url",
                "http://127.0.0.1:1/v1",
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_model", "o02g-model"
            ):
                summary = compiler._maybe_run_curator(
                    vault_id=1,
                    file_id=1,
                    text="probe text about a topic",
                    trigger="manual",
                    deterministic_extraction=_Extraction(),
                    page_id=None,
                    existing_entities=[],
                )
        finally:
            conn.close()

        self.assertIsNotNone(summary, "the orchestration arm returns a"
                            " summary, never raises")
        errors = summary.get("errors") or []
        self.assertTrue(
            any("orchestration_error" in e for e in errors),
            "the stable orchestration_error code must be present in the"
            f" summary errors (got {errors!r})",
        )
        self.assertNotIn(
            ORCH_SENTINEL,
            json.dumps(summary),
            "the compiler summary errors must never carry the raw exception"
            " message text (#958)",
        )

    def test_persist_error_omits_message_text(self):
        # check: AC14 (persist arm) — RED at base, GREEN after the fix.
        # Drives the real persist loop: a stubbed curator returns one
        # accepted candidate, the store's attach_source raises, and the
        # persist_error string lands in the returned summary.
        import app.services.wiki_compiler as wc
        from app.services.wiki_curator import CuratorAcceptedClaim, CuratorResult
        from app.services.wiki_store import WikiStore

        class _StubCurator:
            """Stands in for WikiCurator inside the compiler module."""

            def __init__(self, client, store=None):
                pass

            async def curate(self, **kwargs):
                result = CuratorResult()
                result.calls = 1
                result.input_chars = 32
                result.accepted.append(
                    CuratorAcceptedClaim(
                        claim_text="O02G probe claim text",
                        claim_type="fact",
                        subject=None,
                        predicate=None,
                        object=None,
                        source_quote="probe text",
                        chunk_id="o02g-c1",
                        file_id=1,
                        source_label=None,
                        confidence=0.9,
                        status="needs_review",
                    )
                )
                return result

        class _AttachBoomStore(WikiStore):
            def attach_source(self, *args, **kwargs):
                raise RuntimeError(f"attach boom {PERSIST_SENTINEL}")

        db_path = _mkdb("o02g-persist-")
        self.addCleanup(shutil.rmtree, os.path.dirname(db_path), True)
        conn = _conn(db_path)
        try:
            store = _AttachBoomStore(conn)
            compiler = wc.WikiCompiler(db=conn, store=store)

            class _Extraction:
                role_claims: list = []
                acronyms: list = []

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            self.addCleanup(executor.shutdown)

            with mock.patch.object(
                wc, "_CURATOR_OFFLOAD_EXECUTOR", executor
            ), mock.patch.object(
                wc, "WikiCurator", _StubCurator
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_enabled", True
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_run_on_manual", True
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_url",
                "http://127.0.0.1:1/v1",
            ), mock.patch.object(
                wc._app_settings, "wiki_llm_curator_model", "o02g-model"
            ):
                summary = compiler._maybe_run_curator(
                    vault_id=1,
                    file_id=1,
                    text="probe text about a topic",
                    trigger="manual",
                    deterministic_extraction=_Extraction(),
                    page_id=None,
                    existing_entities=[],
                )
        finally:
            conn.close()

        self.assertIsNotNone(summary, "the persist arm returns a summary")
        errors = summary.get("errors") or []
        self.assertTrue(
            any("persist_error" in e for e in errors),
            "the stable persist_error code must be present in the summary"
            f" errors (got {errors!r})",
        )
        self.assertNotIn(
            PERSIST_SENTINEL,
            json.dumps(summary),
            "the persist_error string must never carry the raw exception"
            " message text (#958)",
        )


class TestResultJsonRawInterpolationCensus(unittest.TestCase):
    """AC15 (NEW-SURFACE guardrail): the o02-style census extends to a
    result_json-shape predicate.

    No ``{type(e).__name__}: {e}`` interpolation may remain in
    ``wiki_curator.py`` / ``wiki_compiler.py`` — the two files whose
    error-string constructions flow into ``jobs.result_json``. Count pinned
    at ZERO per file; the census is RED at base (three sites) and proves
    discriminating on the original defect, so a reintroduced fourth sibling
    fails it too.
    """

    def test_no_raw_exception_message_interpolation_in_curator_or_compiler(
        self,
    ):
        offending = {}
        for path in (WIKI_CURATOR, WIKI_COMPILER):
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
            offending[os.path.basename(path)] = [
                lineno
                for lineno, line in enumerate(source.splitlines(), 1)
                if _RAW_MSG_INTERP.search(line)
            ]
        self.assertEqual(
            offending,
            {"wiki_curator.py": [], "wiki_compiler.py": []},
            "raw exception-message interpolation"
            " {type(e).__name__}: {e} found at the listed lines — error"
            " strings that flow into jobs.result_json must carry at most the"
            " exception class name and a stable code (#958)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
