"""Issue #516 job-lifecycle acceptance checks (DRAFT-005/006/007/011/013/014).

One regression test per acceptance criterion, each printing an
``AC<n> CHECK: PASS`` / ``AC<n> CHECK: FAIL`` sentinel so a plain
``python -m pytest tests/draft_room/test_issue516_jobs_acceptance.py -q -s``
run shows which required behaviors hold on the current tree.

Harness reuses the established patterns of ``test_draft_job_processor.py``
(temp SQLite DB, thread-safe connection pool, real ``DraftJobProcessor`` with
a fake extraction service, deterministic poll-loop driving via
``start``/``wait_until``/``_recover_on_startup``) and ``test_draft_pipeline.py``
(``PipelinePool``, ``PipelineDeps`` injected with a stage-routed fake model and
a deterministic retriever, loopback provider-allowlist patches, real
``run_compile`` against the temp DB). No network, no lancedb, no unstructured.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; stub it like the other suites
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.config import settings
from app.services import draft_pipeline
from app.services.document_extraction import ExtractedDocument
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_job_processor import DraftJobProcessor
from app.services.draft_pipeline import (
    CODE_JOB_TIMEOUT,
    CODE_MODEL_CALL_BUDGET_EXCEEDED,
    CompileFailure,
    PipelineDeps,
    _CompileRun,
    run_compile,
)
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_store import DraftStore, sha256_text

# ═══════════════════════════════════════════════════════════════════════════
# Shared deterministic doubles (copied from test_draft_pipeline.py so this
# acceptance module stays self-contained; see that module's docstrings)
# ═══════════════════════════════════════════════════════════════════════════

OWNER_ID = 91001
VAULT_ID = 91001

PROVIDER_URL = "http://127.0.0.1:11434"

MANUSCRIPT_TEXT = (
    "The internal review window for charter amendments is thirty days."
)
EVIDENCE_PASSAGE = (
    "Section 4 of the 2019 charter fixes the internal review window at 30 days."
)
EVIDENCE_TITLE = "Charter section 4"
SECTION_MARKDOWN = "The review window is 30 days. [S1]"

BRIEF = {
    "piece_type": "memo",
    "audience": "internal counsel",
    "purpose": "restate the review window",
    "target_words": 60,
    "transformation_strength": "moderate",
}


@dataclass(frozen=True)
class FakeSource:
    kind: str
    title: str
    passage: str
    score: float
    content_sha256: str
    updated_at: Optional[str] = None
    file_id: Optional[int] = None
    chunk_uid: Optional[str] = None
    wiki_page_id: Optional[int] = None
    wiki_claim_id: Optional[int] = None
    kms_entry_id: Optional[int] = None


@dataclass(frozen=True)
class FakeRetrievalResult:
    status: str
    sources: tuple
    requested_kinds: frozenset
    successful_kinds: frozenset
    failed_kinds: frozenset
    source_only: bool


DOC_SOURCE = FakeSource(
    kind="document",
    title=EVIDENCE_TITLE,
    passage=EVIDENCE_PASSAGE,
    score=0.71,
    content_sha256=hashlib.sha256(EVIDENCE_PASSAGE.encode()).hexdigest(),
    updated_at="2019-04-01T00:00:00Z",
    file_id=4242,
    chunk_uid="chunk-4242-1",
)

_ALL_KINDS = frozenset({"document", "wiki", "kms"})


class FakeRetriever:
    """Returns the research evidence for facet queries, nothing for claims."""

    def __init__(self, *, facet_sources=(DOC_SOURCE,)) -> None:
        self.facet_sources = tuple(facet_sources)
        self.queries: list[str] = []

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        self.queries.append(query)
        matched = query.strip().rstrip(".") == MANUSCRIPT_TEXT.strip().rstrip(".")
        sources = self.facet_sources if matched else ()
        return FakeRetrievalResult(
            status="ok",
            sources=sources,
            requested_kinds=_ALL_KINDS,
            successful_kinds=_ALL_KINDS,
            failed_kinds=frozenset(),
            source_only=not sources,
        )


_PROMPT_ID_RE = re.compile(r"PROMPT_ID: draft_room\.([a-z]+)\.v1")


def _stage_of(prompt: str) -> str:
    match = _PROMPT_ID_RE.search(prompt)
    if match is None:  # pragma: no cover - defensive
        raise AssertionError("prompt carries no recognizable PROMPT_ID")
    return match.group(1)


class FakeModel:
    """Stage-routed fake ``complete``; ``responses[stage]`` repeats its last item.

    ``calls`` records the stage of every invocation, so a test can assert
    exactly how many model calls a run made.
    """

    def __init__(self, responses) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def count(self, stage: str) -> int:
        return sum(1 for s in self.calls if s == stage)

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        stage = _stage_of(prompt)
        self.calls.append(stage)
        self.prompts.append(prompt)
        queue = self.responses.get(stage)
        if not queue:  # pragma: no cover - defensive
            raise AssertionError(f"no fake response configured for stage {stage!r}")
        index = min(self.count(stage) - 1, len(queue) - 1)
        item = queue[index]
        if callable(item) and not isinstance(item, BaseException):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """Returns ``start`` for the first ``hold`` calls, then ``start + jump``."""

    def __init__(self, start: datetime, *, hold: int = 10**9, jump=timedelta(0)):
        self.start = start
        self.hold = hold
        self.jump = jump
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.start if self.calls <= self.hold else self.start + self.jump


def _research_json() -> str:
    return json.dumps(
        {"retrieval_status": "ok", "contradictions": [], "gaps": []}
    )


def _outline_json(section_count: int = 1, verdict: str = "approved") -> str:
    return json.dumps(
        {
            "mode": "rewrite",
            "sections": [
                {
                    "section_id": f"sec-{n:02d}",
                    "heading": f"Heading {n}",
                    "purpose": "state the review window",
                    "target_words": 40,
                    "evidence_labels": ["S1"],
                    "must_preserve": [],
                    "acceptance_checks": ["names the window"],
                }
                for n in range(1, section_count + 1)
            ],
            "voice_rules": [],
            "critic": {"verdict": verdict, "findings": []},
        }
    )


def _draft_section_json(markdown: str = SECTION_MARKDOWN) -> str:
    return json.dumps(
        {
            "section_id": "sec-01",
            "markdown": markdown,
            "evidence_labels_used": ["S1"],
            "preserved_span_results": [],
            "model_call_audit": {
                "prompt_id": "",
                "prompt_version": "",
                "prompt_sha256": "",
                "model": "",
                "temperature": 0.0,
                "output_sha256": "",
            },
        }
    )


def _no_edits_json() -> str:
    return json.dumps({"edits": [], "findings": []})


def _fact_json(
    *,
    proposition: str = "The review window is 30 days",
    status: str = "supported",
    labels=("S1",),
) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "claim_type": "factual",
                    "proposition": proposition,
                    "status": status,
                    "evidence_labels": list(labels),
                    "retrieval_audit": None,
                    "single_source_warning": False,
                    "high_stakes": True,
                }
            ],
            "findings": [],
        }
    )


def happy_responses(**overrides) -> dict:
    responses = {
        "research": [_research_json()],
        "outline": [_outline_json()],
        "draft": [_draft_section_json()],
        "copy": [_no_edits_json()],
        "standards": [_no_edits_json()],
        "fact": [_fact_json()],
    }
    responses.update(overrides)
    return responses


# ═══════════════════════════════════════════════════════════════════════════
# Parse-job processor harness (pattern of test_draft_job_processor.py)
# ═══════════════════════════════════════════════════════════════════════════


class _FakeUpload:
    """Minimal stand-in for a FastAPI UploadFile."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._content[self._offset :]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeExtractionService:
    """Controllable stand-in for DocumentExtractionService.extract_text."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[str] = []

    def extract_text(self, path: Path) -> ExtractedDocument:
        key = str(path)
        self.calls.append(key)
        resp = self.responses.get(key)
        if callable(resp):
            resp = resp()
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            return ExtractedDocument(
                text="", character_count=0, media_type="text/plain", warnings=[]
            )
        return resp


class _ThreadConnectionPool:
    """Thread-safe SQLite pool exposing ``with pool.connection() as conn``."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn) -> None:
        if self._closed:
            conn.close()
            return
        self._pool.put_nowait(conn)

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


class ParseJobTestBase(unittest.IsolatedAsyncioTestCase):
    """Real temp SQLite DB + real DraftJobProcessor with faked extraction."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        import sqlite3

        seed = sqlite3.connect(self._db_path)
        seed.execute("PRAGMA foreign_keys = ON")
        seed.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1,'owner','hash','Owner','member',1)"
        )
        seed.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1,'V','')"
        )
        seed.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) "
            "VALUES (1,1,'read')"
        )
        seed.commit()
        seed.close()

        self.pool = _ThreadConnectionPool(self._db_path)
        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)
        self.extraction = _FakeExtractionService()
        self.processor = DraftJobProcessor(
            pool=self.pool,
            storage=self.storage,
            extraction=self.extraction,
            poll_interval=0.02,
        )

    def tearDown(self):
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- helpers --

    def make_draft(self, *, owner_id=1, vault_id=1, title="Draft"):
        with self.pool.connection() as conn:
            return DraftStore(conn).create_draft(
                vault_id=vault_id,
                created_by=owner_id,
                title=title,
                mode="compose",
                tier="standard",
                brief_json="{}",
            )

    async def add_input(self, draft_id, *, owner_id=1, name="a.txt", content=b"hi"):
        upload = _FakeUpload(name, content)
        staged = await self.storage.stage_upload(
            upload, allowed_extensions={".txt"}, max_file_bytes=10_000_000
        )
        with self.pool.connection() as conn:
            record = DraftStore(conn).reserve_input(
                draft_id=draft_id,
                owner_id=owner_id,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
        self.storage.finalize(staged, record.storage_relpath)
        return record

    def enqueue_parse_job(self, draft_id, owner_id, input_id, *, timeout_seconds=60):
        with self.pool.connection() as conn:
            return DraftStore(conn).enqueue_parse_job(
                draft_id=draft_id,
                owner_id=owner_id,
                input_id=input_id,
                timeout_seconds=timeout_seconds,
            )

    def get_input(self, draft_id, owner_id, input_id):
        with self.pool.connection() as conn:
            return DraftStore(conn).get_input(
                draft_id=draft_id, owner_id=owner_id, input_id=input_id
            )

    def get_job(self, draft_id, owner_id, job_id):
        with self.pool.connection() as conn:
            return DraftStore(conn).get_job(
                draft_id=draft_id, owner_id=owner_id, job_id=job_id
            )

    def raw_input_row(self, input_id):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT parsed_text, parse_status FROM draft_inputs WHERE id = ?",
                (input_id,),
            ).fetchone()

    async def wait_until(self, predicate, *, timeout=30.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(interval)
        self.fail("condition not met before timeout")


# ═══════════════════════════════════════════════════════════════════════════
# Compile-pipeline harness (pattern of test_draft_pipeline.py)
# ═══════════════════════════════════════════════════════════════════════════


class _PipelinePool(SimpleConnectionPool):
    """``SimpleConnectionPool`` plus the ``with pool.connection()`` idiom."""

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


class CompilePipelineTestBase(unittest.IsolatedAsyncioTestCase):
    """Real temp SQLite DB, seeded compile fixtures, injected fakes."""

    maxDiff = None

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = _PipelinePool(self._db_path)
        self.conn = self.pool.get_connection()
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, "
            "role, is_active) VALUES (?, 'owner', 'hash', 'Owner', 'member', 1)",
            (OWNER_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, 'V1', '')",
            (VAULT_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vault_members "
            "(vault_id, user_id, permission, granted_by) VALUES (?, ?, 'read', ?)",
            (VAULT_ID, OWNER_ID, OWNER_ID),
        )
        self._seed_source_document()
        self.conn.commit()
        self.store = DraftStore(self.conn)

        self._patches = [
            patch.object(settings, "ollama_chat_url", PROVIDER_URL),
            patch.object(settings, "instant_chat_url", PROVIDER_URL),
            patch.object(settings, "draft_allowed_model_origins", [PROVIDER_URL]),
            patch.object(settings, "draft_room_enabled", True),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
            patch.object(draft_pipeline, "_backoff_seconds", lambda attempt: 0.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self.draft_id, self.input_id = self._make_draft_with_ready_input()

    def tearDown(self):
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- fixtures --

    def _seed_source_document(self):
        info = list(self.conn.execute("PRAGMA table_info(files)"))
        row = {}
        for _cid, name, ctype, notnull, dflt, _pk in info:
            if name == "id":
                row[name] = DOC_SOURCE.file_id
            elif name == "vault_id":
                row[name] = VAULT_ID
            elif name == "file_hash":
                row[name] = DOC_SOURCE.content_sha256
            elif name == "filename":
                row[name] = EVIDENCE_TITLE
            elif notnull and dflt is None:
                row[name] = 0 if "INT" in (ctype or "").upper() else "x"
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.conn.execute(
            f"INSERT OR IGNORE INTO files ({cols}) VALUES ({marks})",  # nosec B608
            tuple(row.values()),
        )

    def _make_draft_with_ready_input(self):
        draft = self.store.create_draft(
            vault_id=VAULT_ID,
            created_by=OWNER_ID,
            title="Charter memo",
            mode="rewrite",
            tier="standard",
            brief_json=json.dumps(BRIEF),
        )
        record = self.store.reserve_input(
            draft_id=draft.id,
            owner_id=OWNER_ID,
            role="manuscript",
            authority="primary",
            as_of_date=None,
            original_name="manuscript.txt",
            stored_name="manuscript.txt",
            extension=".txt",
            media_type="text/plain",
            size_bytes=len(MANUSCRIPT_TEXT),
            content_sha256=sha256_text(MANUSCRIPT_TEXT),
            max_inputs=10,
            max_total_input_bytes=10_000_000,
        )
        self.store.set_input_parse_status(input_id=record.id, target="parsing")
        self.store.set_input_parse_status(
            input_id=record.id,
            target="ready",
            parsed_text=MANUSCRIPT_TEXT,
            parsed_text_sha256=sha256_text(MANUSCRIPT_TEXT),
            parsed_char_count=len(MANUSCRIPT_TEXT),
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()
        return draft.id, record.id

    def _make_compile_job(
        self,
        *,
        status="running",
        max_model_calls=40,
        timeout_seconds=1800,
        model_call_count=0,
    ):
        """Insert one compile job row in the given claimed/pending state."""
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, model_call_count, "
            "prompt_bundle_version, compile_input_sha256) "
            "VALUES (?, ?, ?, 'compile', ?, ?, ?, ?, ?, NULL)",
            (
                self.draft_id,
                VAULT_ID,
                OWNER_ID,
                status,
                max_model_calls,
                timeout_seconds,
                model_call_count,
                PROMPT_BUNDLE_VERSION,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _deps(self, model=None, retriever=None, clock=None) -> PipelineDeps:
        self.model = model or FakeModel(happy_responses())
        self.retriever = retriever or FakeRetriever()
        self.clock = clock or FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
        return PipelineDeps(
            retrieve_sources=self.retriever,
            complete=self.model,
            now=self.clock,
        )

    # -- readers --

    def _job_row(self, job_id):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT status, error_code, output_revision_id, model_call_count "
                "FROM draft_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

    def _draft_status(self):
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT status FROM drafts WHERE id = ?", (self.draft_id,)
            ).fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════════
# AC6 (DRAFT-006): startup recovery must not resurrect a cancelled parse job
# ═══════════════════════════════════════════════════════════════════════════


class TestAC6RecoverySkipsCancelledParseJobs(ParseJobTestBase):
    async def test_ac6_startup_recovery_must_not_resurrect_a_cancelled_parse_job(
        self,
    ):
        try:
            draft = self.make_draft()
            input_record = await self.add_input(draft.id)
            job = self.enqueue_parse_job(draft.id, 1, input_record.id)

            # Cancel the PENDING parse job the way the API does.
            with self.pool.connection() as conn:
                DraftStore(conn).request_job_cancel(
                    draft_id=draft.id, owner_id=1, job_id=job.id
                )
            self.assertEqual(
                self.get_job(draft.id, 1, job.id).status, "cancelled"
            )

            # Run the exact startup-recovery sequence the processor runs:
            # recover_orphaned_parse_jobs() -> list_pending_inputs_without_
            # active_job() -> enqueue_parse_job_for_recovery(...) (plus the
            # compile/storage passes that surround them in _recover_on_startup).
            await asyncio.to_thread(self.processor._recover_on_startup)

            # REQUIRED: no new active parse job exists for the cancelled input.
            with self.pool.connection() as conn:
                active = conn.execute(
                    "SELECT COUNT(*) FROM draft_jobs WHERE input_id = ? "
                    "AND job_type = 'parse_input' "
                    "AND status IN ('pending', 'running')",
                    (input_record.id,),
                ).fetchone()[0]
            self.assertEqual(
                int(active),
                0,
                "startup recovery re-enqueued parse work the user cancelled",
            )

            # REQUIRED: the explicit retry path can still create a job.
            with self.pool.connection() as conn:
                retried = DraftStore(conn).retry_parse_job(
                    draft_id=draft.id,
                    owner_id=1,
                    job_id=job.id,
                    timeout_seconds=60,
                )
            self.assertEqual(retried.status, "pending")
            self.assertEqual(retried.parent_job_id, job.id)
            retried_input = self.get_input(draft.id, 1, input_record.id)
            self.assertEqual(retried_input.parse_status, "pending")
        except Exception:
            print("AC6 CHECK: FAIL")
            raise
        print("AC6 CHECK: PASS")


# ═══════════════════════════════════════════════════════════════════════════
# AC7 (DRAFT-007): a queued compile job must survive an unwired RAG engine
# ═══════════════════════════════════════════════════════════════════════════


class TestAC7UnwiredEngineDefersCompileJobs(CompilePipelineTestBase):
    def setUp(self):
        super().setUp()
        # The compile job is queued exactly as _sync_enqueue_compile leaves it.
        self.job_id = self._make_compile_job(
            status="pending", max_model_calls=40, timeout_seconds=1800
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'queued' WHERE id = ?", (self.draft_id,)
        )
        self.conn.commit()

        # A real DraftJobProcessor with the constructor-default engine=None
        # (the state it is in between its start() and lifespan's set_rag_engine).
        self.processor_pool = _ThreadConnectionPool(self._db_path)
        self.processor = DraftJobProcessor(
            pool=self.processor_pool,
            storage=DraftInputStorage(Path(self._temp_dir) / "draft-room"),
            extraction=_FakeExtractionService(),
            poll_interval=0.02,
            # engine intentionally omitted: constructor default is None
        )

        # Route draft_pipeline.default_deps: while the engine is unwired the
        # REAL production deps run (so the unwired behavior under test is the
        # genuine one); once our stub engine is wired, inject the deterministic
        # model/retrieval doubles so the completion half needs no network.
        self.wired_model = FakeModel(happy_responses())
        self.wired_retriever = FakeRetriever()
        self.wired_clock = FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.engine_stub = types.SimpleNamespace(
            retrieve_sources=self.wired_retriever
        )
        real_default_deps = draft_pipeline.default_deps

        def deps_router(*, engine=None):
            if engine is self.engine_stub:
                return PipelineDeps(
                    retrieve_sources=self.wired_retriever,
                    complete=self.wired_model,
                    now=self.wired_clock,
                )
            return real_default_deps(engine=engine)

        router = patch.object(draft_pipeline, "default_deps", deps_router)
        router.start()
        self.addCleanup(router.stop)
        self.addCleanup(self.processor_pool.close_all)

    def tearDown(self):
        super().tearDown()

    async def _one_claim_dispatch_cycle(self):
        """One poll-loop iteration: claim (parse first, then compile) and run."""
        job = await asyncio.to_thread(self.processor._claim_next_job)
        if job is not None:
            await self.processor._run_job(job)
        return job

    async def test_ac7_compile_job_stays_pending_until_engine_is_wired(self):
        try:
            # Cycle 1 with engine=None: the job must NOT be terminally failed.
            await self._one_claim_dispatch_cycle()

            row = self._job_row(self.job_id)
            self.assertEqual(
                row["status"],
                "pending",
                f"unwired-engine dispatch terminally settled the job "
                f"(status={row['status']!r}, error={row['error_code']!r})",
            )
            self.assertNotEqual(
                self._draft_status(),
                "failed",
                "the draft must not be failed while its compile job is merely "
                "waiting for the engine",
            )

            # Wire the engine, then run another cycle: the same job must now
            # run to completion.
            self.processor.set_rag_engine(self.engine_stub)
            claimed = await self._one_claim_dispatch_cycle()
            self.assertIsNotNone(
                claimed, "the queued compile job was not claimable after wiring"
            )

            row_after = self._job_row(self.job_id)
            self.assertEqual(row_after["status"], "completed")
            self.assertIsNone(row_after["error_code"])
            self.assertIsNotNone(row_after["output_revision_id"])
            self.assertEqual(self._draft_status(), "needs_review")
        except Exception:
            print("AC7 CHECK: FAIL")
            raise
        print("AC7 CHECK: PASS")


# ═══════════════════════════════════════════════════════════════════════════
# AC8 (DRAFT-011): a recovered job at the model-call cap makes zero calls
# ═══════════════════════════════════════════════════════════════════════════


class TestAC8RecoveredJobBudgetSeededFromPersistedCount(CompilePipelineTestBase):
    async def test_ac8_recovered_job_at_cap_makes_zero_model_calls(self):
        try:
            # A worker crashed after persisting model_call_count == the cap; a
            # new worker re-runs the recovered job from its draft_jobs row.
            job_id = self._make_compile_job(
                max_model_calls=3, timeout_seconds=1800, model_call_count=3
            )
            deps = self._deps()

            with self.assertRaises(CompileFailure) as caught:
                await run_compile(job_id=job_id, pool=self.pool, deps=deps)

            # REQUIRED: zero additional model calls.
            self.assertEqual(
                self.model.calls,
                [],
                "the recovered run made model calls beyond the persisted cap",
            )
            # REQUIRED: it settles as budget exhaustion, not success.
            self.assertEqual(
                caught.exception.code, CODE_MODEL_CALL_BUDGET_EXCEEDED
            )
            self.assertFalse(caught.exception.retryable)
            row = self._job_row(job_id)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_code"], CODE_MODEL_CALL_BUDGET_EXCEEDED)
            self.assertEqual(row["model_call_count"], 3)
        except Exception:
            print("AC8 CHECK: FAIL")
            raise
        print("AC8 CHECK: PASS")


# ═══════════════════════════════════════════════════════════════════════════
# AC9 (DRAFT-013): a never-returning model call must not outlive the deadline
# ═══════════════════════════════════════════════════════════════════════════


class TestAC9HangingModelCallBoundedByDeadline(CompilePipelineTestBase):
    async def test_ac9_never_returning_model_call_settles_a_timeout_failure(self):
        try:
            # A 1-second wall-clock budget; the model call never returns.
            job_id = self._make_compile_job(
                max_model_calls=40, timeout_seconds=1
            )
            call_state = {"started": False, "returned": False}

            async def hanging_complete(
                prompt, *, logical_mode, temperature, sensitive
            ):
                call_state["started"] = True
                await asyncio.sleep(3600)
                call_state["returned"] = True
                return "{}"

            deps = PipelineDeps(
                retrieve_sources=FakeRetriever(),
                complete=hanging_complete,
                now=lambda: datetime.now(timezone.utc),
            )

            async def _drive() -> None:
                with self.assertRaises(CompileFailure):
                    await run_compile(job_id=job_id, pool=self.pool, deps=deps)

            # The whole run must settle well inside the test's outer bound.
            try:
                await asyncio.wait_for(_drive(), timeout=4.0)
            except asyncio.TimeoutError:
                raise AssertionError(
                    "run_compile was still awaiting the never-returning model "
                    "call at the 4s test bound: the job outlived its 1s "
                    "wall-clock deadline"
                )

            # REQUIRED: the job settled with a timeout failure, and the hung
            # call was abandoned rather than holding the job open.
            self.assertTrue(
                call_state["started"], "the model call was never attempted"
            )
            self.assertFalse(
                call_state["returned"],
                "the test expected a call that never returns",
            )
            row = self._job_row(job_id)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_code"], CODE_JOB_TIMEOUT)
        except Exception:
            print("AC9 CHECK: FAIL")
            raise
        print("AC9 CHECK: PASS")


# ═══════════════════════════════════════════════════════════════════════════
# AC10 (DRAFT-014): a failed replacement publication keeps the old current
# ═══════════════════════════════════════════════════════════════════════════

_PRIOR_REVISION_TEXT = "Previous revision body: the review window is 30 days."


class TestAC10LedgerFailureRetainsCurrentRevision(CompilePipelineTestBase):
    async def test_ac10_failed_publication_retains_previous_current_revision(self):
        try:
            # Seed an existing current revision (a manual save) exactly as a
            # draft with history looks before a replacement compile.
            draft = self.store.get_draft(self.draft_id, OWNER_ID)
            prior = self.store.create_manual_revision(
                draft_id=self.draft_id,
                owner_id=OWNER_ID,
                lock_version=draft.lock_version,
                base_revision_id=None,
                content_md=_PRIOR_REVISION_TEXT,
            )
            self.conn.execute(
                "UPDATE drafts SET status = 'running' WHERE id = ?",
                (self.draft_id,),
            )
            self.conn.commit()
            job_id = self._make_compile_job()

            # The replacement compile runs to Assemble; its revision COMMIT
            # succeeds, then the ledger write fails (simulate a crash/fault in
            # publication after the new revision bytes were committed).
            def exploding_ledger(run_self, revision_id, candidate):
                raise RuntimeError("ledger publication exploded")

            with patch.object(_CompileRun, "_db_write_ledger", exploding_ledger):
                with self.assertRaises(CompileFailure):
                    await run_compile(
                        job_id=job_id, pool=self.pool, deps=self._deps()
                    )

            # REQUIRED: the previous revision is still the current one.
            with self.pool.connection() as conn:
                prior_row = conn.execute(
                    "SELECT is_current FROM draft_revisions WHERE id = ?",
                    (prior.id,),
                ).fetchone()
            self.assertEqual(
                int(prior_row["is_current"]),
                1,
                "the failed replacement publication demoted the previous "
                "current revision",
            )
            with self.pool.connection() as conn:
                store = DraftStore(conn)
                current = store.get_current_revision(
                    draft_id=self.draft_id, owner_id=OWNER_ID
                )
                self.assertIsNotNone(
                    current, "no current revision remains after the failure"
                )
                self.assertEqual(current.id, prior.id)
                got = store.get_revision(
                    draft_id=self.draft_id,
                    owner_id=OWNER_ID,
                    revision_id=prior.id,
                )
                self.assertEqual(got.content_md, _PRIOR_REVISION_TEXT)
        except Exception:
            print("AC10 CHECK: FAIL")
            raise
        print("AC10 CHECK: PASS")


# ═══════════════════════════════════════════════════════════════════════════
# AC17 (DRAFT-005): a cancel landing after the last preflight check is honored
# ═══════════════════════════════════════════════════════════════════════════


class TestAC17CancelAtPreflightBoundaryDiscardsOutput(ParseJobTestBase):
    async def test_ac17_cancel_after_last_preflight_check_discards_output(self):
        try:
            draft = self.make_draft()
            input_record = await self.add_input(draft.id)
            job = self.enqueue_parse_job(draft.id, 1, input_record.id)

            resolved_path = str(self.storage.resolve(input_record.storage_relpath))
            parsed_but_cancelled = "parsed but cancelled before commit"
            self.extraction.responses[resolved_path] = ExtractedDocument(
                text=parsed_but_cancelled,
                character_count=len(parsed_but_cancelled),
                media_type="text/plain",
                warnings=[],
            )

            # Cancel lands at the char-limit evaluation await: AFTER the
            # pre-commit cancellation check #2 has already passed, but BEFORE
            # the output commit. The permission re-check that follows does not
            # look at cancellation, so honoring the cancel requires a re-check
            # at the commit boundary.
            real_limit = self.processor._exceeds_parsed_char_limit

            def limit_eval_and_cancel(draft_id, input_id, extracted):
                with self.pool.connection() as conn:
                    DraftStore(conn).request_job_cancel(
                        draft_id=draft_id, owner_id=1, job_id=job.id
                    )
                return real_limit(draft_id, input_id, extracted)

            self.processor._exceeds_parsed_char_limit = limit_eval_and_cancel

            await self.processor.start()
            try:
                await self.wait_until(
                    lambda: self.get_job(draft.id, 1, job.id).status
                    in ("completed", "failed", "cancelled")
                )
            finally:
                await self.processor.stop()

            # REQUIRED: no parsed text was persisted for the cancelled input.
            raw = self.raw_input_row(input_record.id)
            self.assertIsNone(
                raw["parsed_text"],
                "the cancelled job's parsed output was committed anyway",
            )
            self.assertNotEqual(raw["parse_status"], "ready")
            self.assertEqual(raw["parse_status"], "cancelled")
            self.assertEqual(
                self.get_job(draft.id, 1, job.id).status, "cancelled"
            )
        except Exception:
            print("AC17 CHECK: FAIL")
            raise
        print("AC17 CHECK: PASS")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
