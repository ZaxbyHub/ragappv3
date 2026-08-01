"""Permanent tests for app.services.draft_events (issue #436, SPEC sections
8.4, 9.2).

Two layers:

1. ``build_event``'s fail-closed payload allowlist -- pure unit tests against
   the module's public contract, no DB/network involved.
2. The compile-job event *lifecycle* as actually produced by
   ``DraftJobProcessor`` dispatching a real ``draft_pipeline.run_compile``
   against a real temp SQLite DB, with ``PipelineDeps`` injected fakes (no
   network, no real model) -- mirrors the harness in ``test_draft_pipeline.py``
   (which owns stage-by-stage pipeline *correctness*; this file owns what
   reaches the SSE bus while that pipeline runs, never duplicating the former).

KNOWN GAP (found while writing this file, NOT fixed here per scope): SPEC
section 8.4 lists ``stage_started``, ``stage_progress``, ``stage_completed``
and ``finding_created`` as part of the SSE contract, and ``draft_events.py``'s
``EVENT_TYPES``/``_ALLOWED_PAYLOAD_FIELDS`` allowlists both support them, but
Stage events ARE published: the pipeline emits stage_started before each
stage and stage_completed after the stage row commits. This harness injects
the REAL publisher so the suite sees exactly what production emits.
stage_progress, finding_created and heartbeat remain allowlisted but
unpublished, asserted explicitly in TestStageEventsArePublished.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.config import settings
from app.services import draft_pipeline
from app.services.draft_events import (
    EVENT_TYPES,
    DraftEventBus,
    DraftEventPayloadError,
    build_event,
    get_draft_event_bus,
)
from app.services.draft_job_processor import DraftJobProcessor
from app.services.draft_pipeline import PipelineDeps
from app.services.draft_store import DraftStore, sha256_text

# ── build_event: fails closed on its payload allowlist ──────────────────────


class TestBuildEventAllowlist(unittest.TestCase):
    def test_unknown_event_type_is_rejected(self):
        with self.assertRaises(DraftEventPayloadError):
            build_event("draft_content_leaked", draft_id=1)

    def test_disallowed_field_is_rejected_not_silently_dropped(self):
        """A caller passing a field outside ``_ALLOWED_PAYLOAD_FIELDS`` must
        get a hard failure, never a best-effort event with that field quietly
        stripped -- silently dropping would let a future caller believe an
        unreviewed field reached the wire when it did not, and would let a
        reviewed field ship unnoticed the day someone renames it."""
        with self.assertRaises(DraftEventPayloadError):
            build_event("job_started", draft_id=1, manuscript_text="secret prose")

        # Confirm this is a hard failure, not a partial/best-effort event:
        # no event object is returned at all on the rejected call above, and
        # a subsequent *valid* call is unaffected (the allowlist check has no
        # hidden state).
        event = build_event("job_started", draft_id=1, job_id=2, status="running")
        self.assertEqual(event, {"type": "job_started", "draft_id": 1, "job_id": 2, "status": "running"})

    def test_every_declared_event_type_is_buildable_with_allowed_fields_only(self):
        for event_type in EVENT_TYPES:
            with self.subTest(event_type=event_type):
                event = build_event(event_type, draft_id=1, job_id=2, status="running")
                self.assertEqual(event["type"], event_type)

    def test_bool_is_rejected_even_though_it_is_an_int_subclass(self):
        with self.assertRaises(DraftEventPayloadError):
            build_event("job_started", draft_id=1, job_id=True)

    def test_non_scalar_value_is_rejected(self):
        with self.assertRaises(DraftEventPayloadError):
            build_event("job_started", draft_id=1, job_id=[1, 2, 3])

    def test_overlong_string_is_rejected(self):
        with self.assertRaises(DraftEventPayloadError):
            build_event("job_failed", draft_id=1, error_code="x" * 65)

        build_event("job_failed", draft_id=1, error_code="x" * 64)  # exactly at the bound

    def test_newline_in_string_is_rejected(self):
        with self.assertRaises(DraftEventPayloadError):
            build_event("job_failed", draft_id=1, error_code="bad\ncode")

    def test_none_values_are_dropped_not_rejected(self):
        event = build_event("job_completed", draft_id=1, job_id=None, status="completed")
        self.assertNotIn("job_id", event)


# ── no manuscript/evidence/prompt/exception content ever reaches an event ──


class TestNoContentInEmittedEvents(unittest.TestCase):
    """SPEC section 8.4: "Payloads contain IDs, stage, progress, and small
    summaries only. They MUST NOT contain manuscript text, evidence passages,
    prompts, or draft content." Exercised over every event type the compile
    path can emit."""

    # Deliberately longer than the module's 64-char string bound so the
    # length backstop reliably fires no matter which allowed field carries
    # it (a realistic *short* content fragment is covered separately below).
    FORBIDDEN_CONTENT = (
        "The internal review window for charter amendments is set at exactly thirty days.",
        "Section 4 of the 2019 charter fixes the internal review window at 30 days, no more.",
        "PROMPT_ID: draft_room.research.v1\nSystem: you are an editor bound by policy rules",
        "Traceback (most recent call last):\n  File x, line 1, in <module>\n    raise ValueError",
    )

    def test_allowlisted_fields_cannot_carry_a_content_payload(self):
        # Every field name in the allowlist is a short identifier/enum/stage
        # name -- none of them is even *named* for content, and the value
        # bound is limited to a 64-char scalar, which is itself too small to
        # hold a manuscript passage or a rendered prompt. This asserts that
        # invariant directly against the module's own allowlist rather than
        # re-deriving it.
        from app.services.draft_events import _ALLOWED_PAYLOAD_FIELDS, _MAX_STR_LEN

        content_field_names = {
            "manuscript", "manuscript_text", "passage", "evidence_passage",
            "prompt", "prompt_body", "content", "content_md", "exception",
            "traceback", "raw_error",
        }
        self.assertEqual(_ALLOWED_PAYLOAD_FIELDS & content_field_names, set())
        self.assertLessEqual(_MAX_STR_LEN, 64)

    def test_attempting_to_smuggle_content_through_every_event_type_is_rejected(self):
        for event_type in EVENT_TYPES:
            for content in self.FORBIDDEN_CONTENT:
                with self.subTest(event_type=event_type, content=content[:20]):
                    with self.assertRaises(DraftEventPayloadError):
                        build_event(event_type, draft_id=1, error_code=content)
                    with self.assertRaises(DraftEventPayloadError):
                        build_event(event_type, draft_id=1, status=content)

    def test_the_length_bound_is_a_backstop_not_a_content_classifier(self):
        """Honest documentation of what ``build_event`` actually guarantees:
        a short (<=64 char), single-line string handed to an allowed field
        name is accepted as an opaque scalar -- there is no semantic
        "is this manuscript text" check. The real guarantee is structural:
        every production call site in ``draft_job_processor.py`` only ever
        passes ID-shaped values (job/draft/finding IDs, enum statuses, stable
        error codes) to these fields, never a content fragment, however
        short. This test exists so a future reader does not mistake the
        length/newline bound above for a content filter."""
        short_fragment = "supported"  # a real claim-status enum value, 9 chars
        event = build_event("job_failed", draft_id=1, error_code=short_fragment)
        self.assertEqual(event["error_code"], short_fragment)


# ── DraftEventBus: bounded, best-effort, never raises into a caller ────────


class TestDraftEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_publish_to_zero_subscribers_is_a_silent_no_op(self):
        bus = DraftEventBus()
        # No subscribe() call at all -- publish must not raise.
        bus.publish(1, build_event("job_completed", draft_id=1, job_id=1, status="completed"))

    async def test_subscriber_receives_published_event(self):
        bus = DraftEventBus()
        queue = bus.subscribe(1)
        event = build_event("job_started", draft_id=1, job_id=7, status="running")
        bus.publish(1, event)
        received = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(received, event)

    async def test_unsubscribe_stops_delivery(self):
        bus = DraftEventBus()
        queue = bus.subscribe(1)
        bus.unsubscribe(1, queue)
        self.assertEqual(bus.subscriber_count(1), 0)
        bus.publish(1, build_event("job_completed", draft_id=1, job_id=1, status="completed"))
        self.assertTrue(queue.empty())


# ── compile-job event lifecycle, driven end to end through the real bus ────

OWNER_ID = 92001
VAULT_ID = 92001
PROVIDER_URL = "http://127.0.0.1:11434"
MANUSCRIPT_TEXT = "The internal review window for charter amendments is thirty days."
EVIDENCE_PASSAGE = "Section 4 of the 2019 charter fixes the internal review window at 30 days."
EVIDENCE_TITLE = "Charter section 4"
SECTION_MARKDOWN = "The review window is 30 days. [S1]"


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
    kind="document", title=EVIDENCE_TITLE, passage=EVIDENCE_PASSAGE, score=0.71,
    content_sha256=hashlib.sha256(EVIDENCE_PASSAGE.encode()).hexdigest(),
    updated_at="2019-04-01T00:00:00Z", file_id=4242, chunk_uid="chunk-4242-1",
)
_ALL_KINDS = frozenset({"document", "wiki", "kms"})


class FakeRetriever:
    def __init__(self, *, facet_sources=(DOC_SOURCE,)) -> None:
        self.facet_sources = tuple(facet_sources)

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        matched = query.strip().rstrip(".") == MANUSCRIPT_TEXT.strip().rstrip(".")
        sources = self.facet_sources if matched else ()
        return FakeRetrievalResult(
            status="ok", sources=sources, requested_kinds=_ALL_KINDS,
            successful_kinds=_ALL_KINDS, failed_kinds=frozenset(), source_only=not sources,
        )


_PROMPT_ID_RE = re.compile(r"PROMPT_ID: draft_room\.([a-z]+)\.v1")


def _stage_of(prompt: str) -> str:
    match = _PROMPT_ID_RE.search(prompt)
    if match is None:  # pragma: no cover - defensive
        raise AssertionError("prompt carries no recognizable PROMPT_ID")
    return match.group(1)


class FakeModel:
    """Stage-routed fake ``complete``, copied down from ``test_draft_pipeline.py``
    (trimmed to what this file needs). ``responses[stage]`` is consumed in
    order; the last element repeats forever."""

    def __init__(self, responses) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    def count(self, stage: str) -> int:
        return sum(1 for s in self.calls if s == stage)

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        stage = _stage_of(prompt)
        self.calls.append(stage)
        queue = self.responses.get(stage)
        if not queue:  # pragma: no cover - defensive
            raise AssertionError(f"no fake response configured for stage {stage!r}")
        index = min(self.count(stage) - 1, len(queue) - 1)
        return queue[index]


def _research_json() -> str:
    return json.dumps({"retrieval_status": "ok", "contradictions": [], "gaps": []})


def _outline_json() -> str:
    return json.dumps({
        "mode": "rewrite",
        "sections": [{
            "section_id": "sec-01", "heading": "Heading", "purpose": "state the window",
            "target_words": 40, "evidence_labels": ["S1"], "must_preserve": [],
            "acceptance_checks": ["names the window"],
        }],
        "voice_rules": [], "critic": {"verdict": "approved", "findings": []},
    })


def _draft_section_json() -> str:
    return json.dumps({
        "section_id": "sec-01", "markdown": SECTION_MARKDOWN, "evidence_labels_used": ["S1"],
        "preserved_span_results": [],
        "model_call_audit": {
            "prompt_id": "", "prompt_version": "", "prompt_sha256": "", "model": "",
            "temperature": 0.0, "output_sha256": "",
        },
    })


def _no_edits_json() -> str:
    return json.dumps({"edits": [], "findings": []})


def _fact_json() -> str:
    return json.dumps({
        "claims": [{
            "claim_id": "c1", "claim_type": "factual",
            "proposition": "The review window is 30 days", "status": "supported",
            "evidence_labels": ["S1"], "retrieval_audit": None,
            "single_source_warning": False, "high_stakes": True,
        }],
        "findings": [],
    })


def _happy_responses() -> dict:
    return {
        "research": [_research_json()], "outline": [_outline_json()],
        "draft": [_draft_section_json()], "copy": [_no_edits_json()],
        "standards": [_no_edits_json()], "fact": [_fact_json()],
    }


class EventsPool(SimpleConnectionPool):
    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


class CompileEventsTestBase(unittest.IsolatedAsyncioTestCase):
    """A real temp SQLite DB, a real vault-permitted owner, and
    ``DraftJobProcessor`` driving one compile job end to end through
    ``draft_pipeline.run_compile`` with injected fakes -- no network."""

    async def asyncSetUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = EventsPool(self._db_path)
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
        # The cited document must exist: SPEC 12.6 re-resolution runs before
        # Assemble, so evidence pointing at a non-existent row reads as a
        # source deleted mid-compile and the compile correctly fails closed.
        _info = list(self.conn.execute("PRAGMA table_info(files)"))
        _row = {}
        for _cid, _name, _ctype, _notnull, _dflt, _pk in _info:
            if _name == "id":
                _row[_name] = DOC_SOURCE.file_id
            elif _name == "vault_id":
                _row[_name] = VAULT_ID
            elif _name == "file_hash":
                _row[_name] = DOC_SOURCE.content_sha256
            elif _notnull and _dflt is None:
                _row[_name] = 0 if "INT" in (_ctype or "").upper() else "x"
        self.conn.execute(
            "INSERT OR IGNORE INTO files ({}) VALUES ({})".format(  # nosec B608
                ", ".join(_row), ", ".join("?" * len(_row))
            ),
            tuple(_row.values()),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
            "VALUES (?, ?, 'read', ?)",
            (VAULT_ID, OWNER_ID, OWNER_ID),
        )
        self.conn.commit()
        self.store = DraftStore(self.conn)

        self._patches = [
            patch.object(settings, "ollama_chat_url", PROVIDER_URL),
            patch.object(settings, "instant_chat_url", PROVIDER_URL),
            patch.object(settings, "draft_allowed_model_origins", [PROVIDER_URL]),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
            patch.object(draft_pipeline, "_backoff_seconds", lambda attempt: 0.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        # get_draft_event_bus() is a process-wide singleton, and every test
        # here gets a fresh temp DB whose autoincrement IDs restart at 1 --
        # without resetting it, a leftover subscriber queue from a prior
        # test's draft_id=1 would leak into this one.
        import app.services.draft_events as draft_events_module

        draft_events_module._bus = None
        self.bus = get_draft_event_bus()
        self.draft_id, self.input_id = self._make_draft_with_input()
        self.processor = DraftJobProcessor(
            self.pool, storage=MagicMock(), extraction=MagicMock(), engine=None
        )

    async def asyncTearDown(self):
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _make_draft_with_input(self):
        draft = self.store.create_draft(
            vault_id=VAULT_ID, created_by=OWNER_ID, title="Charter memo", mode="rewrite",
            tier="standard", brief_json=json.dumps({
                "piece_type": "memo", "audience": "internal counsel",
                "purpose": "restate the review window", "target_words": 60,
                "transformation_strength": "moderate",
            }),
        )
        record = self.store.reserve_input(
            draft_id=draft.id, owner_id=OWNER_ID, role="manuscript", authority="primary",
            as_of_date=None, original_name="manuscript.txt", stored_name="manuscript.txt",
            extension=".txt", media_type="text/plain", size_bytes=len(MANUSCRIPT_TEXT),
            content_sha256=sha256_text(MANUSCRIPT_TEXT), max_inputs=10,
            max_total_input_bytes=10_000_000,
        )
        self.store.set_input_parse_status(input_id=record.id, target="parsing")
        self.store.set_input_parse_status(
            input_id=record.id, target="ready", parsed_text=MANUSCRIPT_TEXT,
            parsed_text_sha256=sha256_text(MANUSCRIPT_TEXT), parsed_char_count=len(MANUSCRIPT_TEXT),
        )
        self.conn.execute("UPDATE drafts SET status = 'running' WHERE id = ?", (draft.id,))
        self.conn.commit()
        return draft.id, record.id

    def _make_compile_job(self, *, status="running"):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, status, "
            "max_model_calls, timeout_seconds, prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', ?, 40, 1800, ?)",
            (self.draft_id, VAULT_ID, OWNER_ID, status, draft_pipeline.PROMPT_BUNDLE_VERSION
             if hasattr(draft_pipeline, "PROMPT_BUNDLE_VERSION") else "1"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _fake_deps(self, model=None, retriever=None):
        self.model = model or FakeModel(_happy_responses())
        self.retriever = retriever or FakeRetriever()
        return PipelineDeps(
            retrieve_sources=self.retriever, complete=self.model,
            now=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
            # The REAL publisher. PipelineDeps.publish defaults to a no-op, so
            # omitting it would send every stage event into a black hole and
            # let this suite certify behaviour production does not have -- which
            # is exactly how a dropped stage_completed went unnoticed.
            publish=draft_pipeline._default_publish,
        )

    async def _dispatch(self, job_id, *, deps=None):
        """Drive one already-``running`` job through the exact processor
        method production uses (``_dispatch_job``), with
        ``draft_pipeline.default_deps`` swapped for injected fakes -- this
        is the real event-publishing code path, not a re-implementation of
        it."""
        deps = deps or self._fake_deps()
        job = self.store.get_job(draft_id=self.draft_id, owner_id=OWNER_ID, job_id=job_id)
        with patch.object(draft_pipeline, "default_deps", lambda engine=None: deps):
            await self.processor._dispatch_job(job)

    def _job_status(self, job_id):
        row = self.conn.execute(
            "SELECT status, error_code FROM draft_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return str(row[0]), row[1]


class TestCompileLifecycleEvents(CompileEventsTestBase):
    async def test_happy_path_event_ordering_is_job_started_then_job_completed(self):
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)

        await self._dispatch(job_id)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "job_started")
        self.assertEqual(types[-1], "job_completed")
        self.assertLess(
            types.index("job_started"), types.index("stage_started")
        )
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[-1]["status"], "completed")
        for event in events:
            self.assertEqual(event["job_id"], job_id)
            self.assertEqual(event["draft_id"], self.draft_id)

        status, error_code = self._job_status(job_id)
        self.assertEqual(status, "completed")
        self.assertIsNone(error_code)

    async def test_failure_path_emits_job_failed_with_error_code_only(self):
        # A structurally invalid outline response fails structured-output
        # parsing; SPEC 10.2 allows exactly one repair attempt, so two bad
        # responses in a row exhausts it with a non-retryable
        # invalid_stage_output failure.
        bad_model = FakeModel({
            "research": [_research_json()],
            "outline": ["not valid json", "still not valid json"],
        })
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)

        await self._dispatch(job_id, deps=self._fake_deps(model=bad_model))

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        # Stage events now interleave; assert the job-level ORDER rather than
        # an exhaustive sequence so adding a stage event is not a false failure.
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "job_started")
        self.assertEqual(types[-1], "job_failed")
        self.assertNotIn("job_completed", types)
        failed = events[-1]
        self.assertEqual(failed["error_code"], "invalid_stage_output")
        # No exception text, traceback, or model output ever reaches the
        # event -- only the stable machine code.
        blob = json.dumps(failed)
        self.assertNotIn("not valid json", blob)
        self.assertNotIn("Traceback", blob)

        status, error_code = self._job_status(job_id)
        self.assertEqual(status, "failed")
        self.assertEqual(error_code, "invalid_stage_output")

    async def test_disconnected_subscriber_does_not_affect_job_execution(self):
        """SPEC section 8.4: 'A disconnected SSE client does not affect job
        execution.' Subscribing and then unsubscribing *before* the job runs
        -- as a dropped HTTP connection would leave no queue behind -- must
        not change whether or how the job completes."""
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)
        self.bus.unsubscribe(self.draft_id, queue)
        self.assertEqual(self.bus.subscriber_count(self.draft_id), 0)

        await self._dispatch(job_id)

        status, error_code = self._job_status(job_id)
        self.assertEqual(status, "completed")
        self.assertIsNone(error_code)
        # Publishing after every subscriber vanished must not have raised
        # (the dispatch above would have propagated any such exception).
        self.assertTrue(queue.empty())

    async def test_no_subscriber_at_all_does_not_affect_job_execution(self):
        job_id = self._make_compile_job()
        # No subscribe() call whatsoever -- publish() has zero subscribers.
        await self._dispatch(job_id)
        status, _ = self._job_status(job_id)
        self.assertEqual(status, "completed")

    async def test_canonical_status_comes_from_sqlite_not_from_the_event_stream(self):
        """The event bus is notification only; a subscriber that never reads
        its queue must still be able to observe job completion by polling
        the database -- the DB write commits before the event is published,
        never the reverse."""
        job_id = self._make_compile_job()
        self.bus.subscribe(self.draft_id)  # queue created but never drained

        await self._dispatch(job_id)

        # The database is authoritative regardless of whether anything ever
        # reads the (still-full, undrained) event queue.
        status, _ = self._job_status(job_id)
        self.assertEqual(status, "completed")

    async def test_terminal_event_is_never_lost_to_a_full_droppable_queue(self):
        """Fill a subscriber's bounded queue with droppable events, then run
        a job to completion: the terminal ``job_completed`` must still be
        delivered (SPEC 8.4: 'make space for terminal events'), even though
        this compile path itself never emits ``stage_progress``/heartbeat
        events (see this module's docstring) -- exercised directly against
        the bus with synthetic filler so the terminal-event guarantee is
        checked independent of whether the pipeline ever produces enough
        organic progress events to fill a 100-slot queue."""
        queue = self.bus.subscribe(self.draft_id)
        for _ in range(100):
            self.bus.publish(
                self.draft_id,
                build_event("stage_progress", draft_id=self.draft_id, progress_percent=50.0),
            )
        self.assertTrue(queue.full())

        job_id = self._make_compile_job()
        await self._dispatch(job_id)

        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        self.assertIn("job_completed", [e["type"] for e in drained])


class TestStageEventsArePublished(CompileEventsTestBase):
    """SPEC section 8.4 stage events must actually reach subscribers.

    This previously asserted the opposite -- that only job-level events were
    ever observed -- and passed for two reasons that were both wrong: the
    pipeline published nothing, and the harness injected a no-op publisher so
    it could not have seen the events anyway. Both are fixed; the assertions
    are inverted accordingly.

    ``stage_progress``, ``finding_created`` and ``heartbeat`` remain
    allowlisted but unpublished, which is asserted explicitly below so that
    starting to publish one is a deliberate change rather than a silent one.
    """

    async def test_stage_events_reach_the_bus_across_a_full_compile(self):
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)
        await self._dispatch(job_id)

        seen_types = set()
        stages_started = []
        while not queue.empty():
            event = queue.get_nowait()
            seen_types.add(event["type"])
            if event["type"] == "stage_started":
                stages_started.append(event["stage"])

        self.assertIn("job_started", seen_types)
        self.assertIn("stage_started", seen_types)
        self.assertIn("stage_completed", seen_types)
        self.assertIn("job_completed", seen_types)
        # Stage events arrive in canonical pipeline order.
        self.assertEqual(stages_started, list(draft_pipeline.COMPILE_STAGE_ORDER))

    async def test_still_unpublished_event_types_are_asserted_explicitly(self):
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)
        await self._dispatch(job_id)
        seen = set()
        while not queue.empty():
            seen.add(queue.get_nowait()["type"])
        for absent in ("stage_progress", "finding_created", "heartbeat"):
            self.assertNotIn(absent, seen)

    async def test_no_event_carries_content(self):
        job_id = self._make_compile_job()
        queue = self.bus.subscribe(self.draft_id)
        await self._dispatch(job_id)
        while not queue.empty():
            event = queue.get_nowait()
            blob = " ".join(str(v) for v in event.values()).lower()
            for leak in ("manuscript", "passage", "prompt_id:", "traceback"):
                self.assertNotIn(leak, blob, event)



if __name__ == "__main__":
    unittest.main()
