"""Permanent pipeline tests for app.services.draft_pipeline (issue #436, SPEC 17.1).

Exercises ``run_compile`` end to end against a REAL temp SQLite database
(``init_db`` + ``run_migrations``) with a ``SimpleConnectionPool``, matching the
harness style of ``test_draft_store.py`` / ``test_draft_job_processor.py``.
There is no network and no real model call: ``PipelineDeps`` is injected with

* ``complete`` -- a deterministic fake routed on the ``PROMPT_ID:`` line that
  every ``draft_prompts`` template embeds, so each stage gets a stage-correct
  structured payload;
* ``retrieve_sources`` -- a deterministic fake returning ``RetrievedSource``
  -shaped snapshots (never importing ``rag_engine``, which drags in the vector
  store);
* ``now`` -- an injected clock, so the wall-clock budget is deterministic.

The provider-origin allowlist is exercised for real: ``settings.ollama_chat_url``
is pointed at ``http://127.0.0.1:11434`` with ``ALLOW_LOCAL_SERVICES=1`` and that
exact origin in ``settings.draft_allowed_model_origins``, so
``assert_provider_allowed`` (and the SSRF guard beneath it) genuinely run and
genuinely pass without leaving the machine.

The pipeline -> research seam is exercised for real: ``run_research`` is the
genuine implementation, so facet derivation, per-facet retrieval, label
assignment and evidence snapshotting all run end to end. An earlier revision
passed ``ctx.brief_json`` (a ``str``) and ``ctx.inputs`` (frozen
``_InputSnapshot`` dataclasses) straight into ``run_research``, which reads its
inputs with ``input_record.get(...)`` and so would have raised ``AttributeError``
on every production compile; ``_stage_research`` now marshals both through
``_research_brief()`` / ``_research_inputs()``.
"""

import ast
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.config import settings
from app.services import draft_pipeline, draft_research
from app.services.draft_pipeline import (
    CODE_ASSEMBLE_HASH_MISMATCH,
    CODE_ASSEMBLE_WITHOUT_FACT,
    CODE_INVALID_STAGE_OUTPUT,
    CODE_JOB_CANCELLED,
    CODE_JOB_TIMEOUT,
    CODE_MODEL_CALL_BUDGET_EXCEEDED,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_SECTION_BUDGET_EXCEEDED,
    COMPILE_STAGE_ORDER,
    PROVISIONAL_ASSEMBLY_ENABLED,
    CompileFailure,
    PipelineDeps,
    _build_context,
    _CompileRun,
    _stage_input_hash,
    run_compile,
)
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION, IntakeManifest
from app.services.draft_store import DraftStore, canonical_json, sha256_text


class PipelinePool(SimpleConnectionPool):
    """``SimpleConnectionPool`` plus the ``with pool.connection()`` idiom.

    ``draft_pipeline`` reaches SQLite exclusively through
    ``with pool.connection() as conn``, which is what the production
    ``SQLiteConnectionPool`` exposes; the shared route-test pool only offers
    get/release. This adds the context manager and nothing else.
    """

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


OWNER_ID = 91001
VAULT_ID = 91001

#: Loopback origin used for every provider check. Resolves without a network,
#: and is placed on the real Draft Room allowlist so the real guard runs.
PROVIDER_URL = "http://127.0.0.1:11434"

MANUSCRIPT_TEXT = (
    "The internal review window for charter amendments is thirty days."
)
EVIDENCE_PASSAGE = (
    "Section 4 of the 2019 charter fixes the internal review window at 30 days."
)
EVIDENCE_TITLE = "Charter section 4"

#: What the fake Draft desk emits, and therefore the compile candidate.
SECTION_MARKDOWN = "The review window is 30 days. [S1]"
CANDIDATE = SECTION_MARKDOWN

BRIEF = {
    "piece_type": "memo",
    "audience": "internal counsel",
    "purpose": "restate the review window",
    "target_words": 60,
    "transformation_strength": "moderate",
}


# ── Deterministic retrieval doubles ──────────────────────────────────────────


@dataclass(frozen=True)
class FakeSource:
    """Mirrors ``rag_engine.RetrievedSource`` without importing rag_engine."""

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
    """Mirrors ``rag_engine.RAGRetrievalResult``."""

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
    """Returns the research evidence for facet queries, nothing for claims.

    Research derives its facet queries from the manuscript sentence; a
    claim-specific Fact retrieval uses the normalized claim proposition, which
    never equals that sentence. Returning nothing for the latter is what makes
    the "zero-result retrieval still records an audit" case reachable.
    """

    def __init__(self, *, facet_sources=(DOC_SOURCE,), error=None) -> None:
        self.facet_sources = tuple(facet_sources)
        self.error = error
        self.queries: list[str] = []

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
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


# ── Deterministic model double ───────────────────────────────────────────────

_PROMPT_ID_RE = re.compile(r"PROMPT_ID: draft_room\.([a-z]+)\.v1")


def _stage_of(prompt: str) -> str:
    """Route on the PROMPT_ID literal every draft_prompts template embeds."""
    match = _PROMPT_ID_RE.search(prompt)
    if match is None:  # pragma: no cover - defensive
        raise AssertionError("prompt carries no recognizable PROMPT_ID")
    return match.group(1)


class FakeModel:
    """Stage-routed fake ``complete``.

    ``responses[stage]`` is a list consumed in order whose LAST element repeats
    forever. An element may be a ``str`` (returned verbatim), an ``Exception``
    (raised), or a zero-argument callable (called; its return value is used).
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


# ── Canonical stage payloads ─────────────────────────────────────────────────


def research_json() -> str:
    # Only contradictions/gaps are taken from the model; facets, evidence and
    # retrieval status are recomputed deterministically by draft_research.
    return json.dumps(
        {"retrieval_status": "ok", "contradictions": [], "gaps": []}
    )


def outline_json(section_count: int = 1, verdict: str = "approved") -> str:
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


def draft_section_json(markdown: str = SECTION_MARKDOWN) -> str:
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


def no_edits_json() -> str:
    return json.dumps({"edits": [], "findings": []})


def edit_json(text: str, old: str, new: str, *, semantic: bool) -> str:
    """One precise, hash-pinned desk edit replacing ``old`` with ``new``."""
    start = text.index(old)
    return json.dumps(
        {
            "edits": [
                {
                    "section_id": "sec-01",
                    "start": start,
                    "end": start + len(old),
                    "before_sha256": sha256_text(old),
                    "after_sha256": sha256_text(new),
                    "before_excerpt": old,
                    "after_excerpt": new,
                    "category": "precision",
                    "rationale": "match the charter wording",
                    "semantic_change": semantic,
                    "affected_claim_ids": [],
                    "affected_evidence_labels": ["S1"],
                }
            ],
            "findings": [],
        }
    )


def fact_json(
    *,
    proposition: str = "The review window is 30 days",
    status: str = "supported",
    claim_type: str = "factual",
    labels=("S1",),
) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "claim_type": claim_type,
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
        "research": [research_json()],
        "outline": [outline_json()],
        "draft": [draft_section_json()],
        "copy": [no_edits_json()],
        "standards": [no_edits_json()],
        "fact": [fact_json()],
    }
    responses.update(overrides)
    return responses


# ── Research seam adapter (see module docstring) ─────────────────────────────

# ── Base harness ─────────────────────────────────────────────────────────────


class PipelineTestBase(unittest.IsolatedAsyncioTestCase):
    """Real temp SQLite DB, a real compile job row, and injected fakes."""

    maxDiff = None

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = PipelinePool(self._db_path)
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
        # The owner must still hold vault read: SPEC 9.1 rule 4 re-checks
        # permission immediately before the final revision is stored, so a
        # fixture without membership is indistinguishable from a revocation
        # mid-compile and the job correctly fails permission_revoked.
        self.conn.execute(
            "INSERT OR IGNORE INTO vault_members "
            "(vault_id, user_id, permission, granted_by) VALUES (?, ?, 'read', ?)",
            (VAULT_ID, OWNER_ID, OWNER_ID),
        )
        # The document the fake retriever cites must really exist in the
        # vault: SPEC 12.6 re-resolution runs before Assemble, so evidence
        # pointing at a row that was never there is indistinguishable from a
        # source deleted mid-compile, and the compile correctly fails closed.
        # Seeding it keeps the fixture honest rather than weakening the gate.
        self._seed_source_document()
        self.conn.commit()
        self.store = DraftStore(self.conn)

        # Real provider-policy enforcement against a loopback origin.
        self._patches = [
            patch.object(settings, "ollama_chat_url", PROVIDER_URL),
            patch.object(settings, "instant_chat_url", PROVIDER_URL),
            patch.object(settings, "draft_allowed_model_origins", [PROVIDER_URL]),
            # SPEC 9.2 re-checks the kill switch before EVERY model
            # call, so a compile cannot run with the feature off.
            patch.object(settings, "draft_room_enabled", True),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
            # Keep bounded-backoff tests fast; the bound itself is asserted by
            # the call count, not by wall time.
            patch.object(draft_pipeline, "_backoff_seconds", lambda attempt: 0.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self.draft_id, self.input_id = self._make_draft_with_input()
        self.job_id = self._make_compile_job()

    def _seed_source_document(self):
        """Insert the ``files`` row DOC_SOURCE claims to come from.

        ``file_hash`` is set to the canonical whole-source hash the pipeline
        stores on the evidence row, so freshness re-resolution finds the source
        unchanged.
        """
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

    def tearDown(self):
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- fixtures --

    def _make_draft_with_input(self):
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
        # The compile lifecycle owns the draft from here (SPEC 10.3).
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()
        return draft.id, record.id

    def _make_compile_job(self, *, max_model_calls=40, timeout_seconds=1800,
                          fingerprint=None, bundle=PROMPT_BUNDLE_VERSION):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version, "
            "compile_input_sha256) "
            "VALUES (?, ?, ?, 'compile', 'running', ?, ?, ?, ?)",
            (
                self.draft_id,
                VAULT_ID,
                OWNER_ID,
                max_model_calls,
                timeout_seconds,
                bundle,
                fingerprint,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- run helpers --

    def _deps(self, model=None, retriever=None, clock=None) -> PipelineDeps:
        self.model = model or FakeModel(happy_responses())
        self.retriever = retriever or FakeRetriever()
        self.clock = clock or FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
        return PipelineDeps(
            retrieve_sources=self.retriever,
            complete=self.model,
            now=self.clock,
        )

    async def _run(self, **kwargs) -> None:
        await run_compile(job_id=self.job_id, pool=self.pool, deps=self._deps(**kwargs))

    async def _run_expect_failure(self, **kwargs) -> CompileFailure:
        with self.assertRaises(CompileFailure) as caught:
            await self._run(**kwargs)
        return caught.exception

    # -- assertions / readers --

    def _stages(self):
        return DraftStore(self.conn).list_stages(job_id=self.job_id, limit=500)

    def _completed_stage_names(self, *a, **k):
        """Stage names for COMPLETED rows only.

        Failed attempts now persist a row too, so a test asserting a stage did
        not finish must look at completed rows rather than at row existence.
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT stage FROM draft_job_stages WHERE job_id = ? "
                "AND status = 'completed' ORDER BY id",
                (self.job_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def _stage_names(self):
        return [row.stage for row in self._stages()]

    def _stage(self, name, *, occurrence=0):
        rows = [row for row in self._stages() if row.stage == name]
        self.assertGreater(
            len(rows), occurrence, f"no {name!r} stage row #{occurrence}"
        )
        return rows[occurrence]

    def _current_revision(self):
        return self.conn.execute(
            "SELECT id, content_md, content_sha256, fact_status, is_current, source "
            "FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (self.draft_id,),
        ).fetchone()

    def _draft_row(self):
        return self.conn.execute(
            "SELECT status, ready_revision_id, ready_by, ready_at FROM drafts "
            "WHERE id = ?",
            (self.draft_id,),
        ).fetchone()

    def _job_row(self):
        return self.conn.execute(
            "SELECT status, error_code, output_revision_id, model_call_count "
            "FROM draft_jobs WHERE id = ?",
            (self.job_id,),
        ).fetchone()

    def _findings(self):
        return self.conn.execute(
            "SELECT stage, rule_id, severity, waivable, message FROM draft_findings "
            "WHERE job_id = ? ORDER BY id ASC",
            (self.job_id,),
        ).fetchall()

    def _claims(self):
        revision = self._current_revision()
        self.assertIsNotNone(revision, "no current revision was created")
        return self.conn.execute(
            "SELECT ordinal, claim_text, status, severity, claim_type, "
            "retrieval_audit_json FROM draft_claims WHERE revision_id = ? "
            "ORDER BY ordinal ASC",
            (revision["id"],),
        ).fetchall()


# ── 1. Canonical stage order ─────────────────────────────────────────────────


class TestCanonicalStageOrder(PipelineTestBase):
    async def test_recorded_stages_equal_compile_stage_order_with_copy_before_standards(
        self,
    ):
        await self._run()

        recorded = self._stage_names()
        self.assertEqual(tuple(recorded), COMPILE_STAGE_ORDER)
        self.assertLess(
            recorded.index("copy"),
            recorded.index("standards"),
            "Copy must always run before Standards (SPEC 11.7)",
        )
        for row in self._stages():
            self.assertEqual(row.status, "completed", row.stage)
        job = self._job_row()
        self.assertEqual(job["status"], "completed")
        self.assertIsNotNone(job["output_revision_id"])


# ── 2/3. Byte and hash continuity ────────────────────────────────────────────


class TestHashContinuity(PipelineTestBase):
    async def test_standards_fact_assemble_and_revision_share_one_candidate_hash(self):
        await self._run()

        standards = self._stage("standards").candidate_sha256
        fact = self._stage("fact").candidate_sha256
        assemble = self._stage("assemble").candidate_sha256
        revision = self._current_revision()

        self.assertEqual(standards, fact)
        self.assertEqual(fact, assemble)
        self.assertEqual(assemble, revision["content_sha256"])
        self.assertEqual(revision["content_md"], CANDIDATE)
        self.assertEqual(revision["content_sha256"], sha256_text(CANDIDATE))
        self.assertEqual(revision["source"], "pipeline")

    async def test_every_stage_artifact_sha256_matches_its_artifact_json(self):
        await self._run()

        rows = self._stages()
        self.assertEqual(len(rows), len(COMPILE_STAGE_ORDER))
        for row in rows:
            self.assertIsNotNone(row.artifact_sha256, row.stage)
            self.assertEqual(
                row.artifact_sha256,
                sha256_text(row.artifact_json),
                f"{row.stage} artifact hash does not match its stored JSON",
            )
            # Stored artifacts are canonical JSON, not free-form model output.
            json.loads(row.artifact_json)

    async def test_fact_stage_never_mutates_the_candidate(self):
        await self._run()

        pre_fact = self._stage("standards").candidate_sha256
        fact_row = self._stage("fact")
        post_fact = self._stage("assemble").candidate_sha256

        self.assertEqual(pre_fact, fact_row.candidate_sha256)
        self.assertEqual(fact_row.candidate_sha256, post_fact)
        self.assertEqual(fact_row.content_md, CANDIDATE)
        self.assertEqual(self.model.count("fact"), 1)


# ── 4. A Standards semantic change forces another Fact run ───────────────────


class TestSemanticChangeReturnsToFact(PipelineTestBase):
    async def test_standards_semantic_edit_forces_fact_to_rerun_before_assemble(self):
        corrected = CANDIDATE.replace("30 days", "30 business days")
        model = FakeModel(
            happy_responses(
                standards=[
                    no_edits_json(),
                    edit_json(CANDIDATE, "30 days", "30 business days", semantic=True),
                    no_edits_json(),
                ],
                # First verdict demands a correction; the re-run approves the
                # text Standards actually produced.
                fact=[
                    fact_json(status="stale"),
                    fact_json(proposition="The review window is 30 business days"),
                ],
            )
        )
        await self._run(model=model)

        names = self._stage_names()
        self.assertEqual(names.count("fact"), 2)
        self.assertEqual(names.count("standards"), 2)
        self.assertEqual(names.count("copy"), 2)
        # Copy still precedes Standards inside the correction loop.
        self.assertLess(
            names.index("copy", names.index("standards")),
            names.index("standards", names.index("standards") + 1),
        )
        # The second Fact ran against the post-Standards bytes, and Assemble
        # shipped exactly those.
        first_fact = self._stage("fact", occurrence=0)
        second_fact = self._stage("fact", occurrence=1)
        second_standards = self._stage("standards", occurrence=1)
        self.assertNotEqual(first_fact.candidate_sha256, second_fact.candidate_sha256)
        self.assertEqual(
            second_standards.candidate_sha256, second_fact.candidate_sha256
        )
        self.assertEqual(second_standards.semantic_changed, 1)
        self.assertEqual(
            self._stage("assemble").candidate_sha256, second_fact.candidate_sha256
        )
        revision = self._current_revision()
        self.assertEqual(revision["content_md"], corrected)
        # Fact is the LAST desk to touch the bytes: no stage row after the final
        # Fact carries a different candidate.
        rows = self._stages()
        last_fact_index = max(i for i, r in enumerate(rows) if r.stage == "fact")
        for row in rows[last_fact_index:]:
            self.assertEqual(row.candidate_sha256, second_fact.candidate_sha256)


# ── 5. Correction-loop cap (SPEC 11.8) ───────────────────────────────────────


class TestCorrectionLoopCap(PipelineTestBase):
    async def test_reaching_the_cap_still_stores_a_needs_review_revision(self):
        """SPEC 11.8: the cap ends the loop, it does not fail the job.

        Residual issues stay visible as findings and the output IS stored as a
        ``needs_review`` revision. (An earlier revision of the module raised
        ``correction_loop_exceeded``; that behavior is gone and must not return.)
        """
        model = FakeModel(happy_responses(fact=[fact_json(status="stale")]))
        await self._run(model=model)

        cap = settings.draft_qa_retry_limit
        names = self._stage_names()
        # cap correction loops => cap + 1 Fact runs, and cap extra Copy/Standards.
        self.assertEqual(names.count("fact"), cap + 1)
        self.assertEqual(names.count("copy"), cap + 1)
        self.assertEqual(names.count("standards"), cap + 1)

        # The output IS stored.
        revision = self._current_revision()
        self.assertIsNotNone(revision)
        self.assertEqual(revision["content_md"], CANDIDATE)
        self.assertEqual(revision["fact_status"], "findings")

        # The draft lands in needs_review, and the job completed normally.
        draft = self._draft_row()
        self.assertEqual(draft["status"], "needs_review")
        job = self._job_row()
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["error_code"])

        # Residual non-waivable findings persist as rows.
        blockers = [
            row
            for row in self._findings()
            if row["severity"] == "blocker" and row["waivable"] == 0
        ]
        self.assertTrue(blockers, "residual blockers must be persisted as findings")
        self.assertIn("fact.claim_stale", {row["rule_id"] for row in blockers})
        # And the claim itself is recorded with its unresolved verdict.
        statuses = {row["status"] for row in self._claims()}
        self.assertEqual(statuses, {"stale"})

    async def test_cap_is_not_reported_as_a_failure_code(self):
        model = FakeModel(happy_responses(fact=[fact_json(status="stale")]))
        await self._run(model=model)  # must not raise
        job = self._job_row()
        self.assertEqual(job["status"], "completed")
        self.assertNotIn("correction_loop", str(job["error_code"]))


# ── 6/7/8. Assemble gates ────────────────────────────────────────────────────


class TestAssembleGates(PipelineTestBase):
    """Assemble's two refusals are unreachable through a well-behaved run, so
    they are driven directly on a real ``_CompileRun`` built from the real job.
    """

    async def _fresh_run(self) -> _CompileRun:
        deps = self._deps()

        def build():
            with self.pool.connection() as conn:
                job = DraftStore(conn).get_job(
                    draft_id=self.draft_id, owner_id=OWNER_ID, job_id=self.job_id
                )
                return _build_context(conn, job, deps.now())

        ctx = await asyncio.to_thread(build)
        return _CompileRun(pool=self.pool, deps=deps, ctx=ctx)

    async def test_assemble_refuses_a_candidate_that_is_not_the_fact_candidate(self):
        run = await self._fresh_run()
        run._fact_report = object()
        run._fact_candidate_sha256 = sha256_text("the candidate fact approved")
        run._candidate = "a different candidate nobody fact-checked"

        with self.assertRaises(CompileFailure) as caught:
            await run._stage_assemble()

        self.assertEqual(caught.exception.code, CODE_ASSEMBLE_HASH_MISMATCH)
        self.assertFalse(caught.exception.retryable)
        self.assertIsNone(self._current_revision())

    async def test_assemble_refuses_to_run_without_a_successful_fact_stage(self):
        run = await self._fresh_run()
        run._candidate = CANDIDATE
        self.assertIsNone(run._fact_report)

        with self.assertRaises(CompileFailure) as caught:
            await run._stage_assemble()

        self.assertEqual(caught.exception.code, CODE_ASSEMBLE_WITHOUT_FACT)
        self.assertFalse(caught.exception.retryable)
        self.assertIsNone(self._current_revision())
        # SPEC 11.10's provisional path is disabled for all new jobs.
        self.assertFalse(PROVISIONAL_ASSEMBLY_ENABLED)

    async def test_assemble_leaves_the_draft_needs_review_with_no_ready_fields(self):
        # Pre-set the ready columns so the test proves Assemble CLEARS them
        # rather than merely never having set them.
        self.conn.execute(
            "UPDATE drafts SET ready_by = ?, ready_at = CURRENT_TIMESTAMP WHERE id = ?",
            (OWNER_ID, self.draft_id),
        )
        self.conn.commit()

        await self._run()

        draft = self._draft_row()
        self.assertEqual(draft["status"], "needs_review")
        self.assertIsNone(draft["ready_revision_id"])
        self.assertIsNone(draft["ready_by"])
        self.assertIsNone(draft["ready_at"])
        self.assertNotEqual(draft["status"], "ready")


# ── 9. Budgets ───────────────────────────────────────────────────────────────


class TestBudgets(PipelineTestBase):
    async def test_model_call_budget_exhaustion_is_stable_and_non_retryable(self):
        self.conn.execute(
            "UPDATE draft_jobs SET max_model_calls = 1 WHERE id = ?", (self.job_id,)
        )
        self.conn.commit()

        failure = await self._run_expect_failure()

        self.assertEqual(failure.code, CODE_MODEL_CALL_BUDGET_EXCEEDED)
        self.assertFalse(failure.retryable)
        job = self._job_row()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], CODE_MODEL_CALL_BUDGET_EXCEEDED)
        self.assertEqual(self._draft_row()["status"], "failed")
        self.assertIsNone(self._current_revision())

    async def test_wall_clock_budget_exhaustion_is_stable_and_non_retryable(self):
        # The first tick sets the deadline; every later tick is past it.
        clock = FakeClock(
            datetime(2026, 8, 1, tzinfo=timezone.utc), hold=1, jump=timedelta(hours=10)
        )
        failure = await self._run_expect_failure(clock=clock)

        self.assertEqual(failure.code, CODE_JOB_TIMEOUT)
        self.assertFalse(failure.retryable)
        self.assertEqual(self._job_row()["error_code"], CODE_JOB_TIMEOUT)
        self.assertEqual(self._stage_names(), [])

    async def test_section_budget_exhaustion_is_stable_and_non_retryable(self):
        model = FakeModel(
            happy_responses(outline=[outline_json(settings.draft_max_sections + 1)])
        )
        failure = await self._run_expect_failure(model=model)

        self.assertEqual(failure.code, CODE_SECTION_BUDGET_EXCEEDED)
        self.assertFalse(failure.retryable)
        self.assertEqual(self._job_row()["error_code"], CODE_SECTION_BUDGET_EXCEEDED)
        # The over-budget outline was refused, not persisted and drafted from.
        # A failed attempt now leaves an audit row, so assert the stage never
        # COMPLETED rather than that no row exists.
        self.assertNotIn("outline", self._completed_stage_names())
        self.assertIn("outline", self._stage_names())
        self.assertNotIn("draft", self._stage_names())


# ── 10/11/12. Provider policy, transient retry, structured-output repair ─────


class TestProviderFailureHandling(PipelineTestBase):
    async def test_provider_policy_error_is_not_retried_and_makes_zero_calls(self):
        # An empty allowlist fails closed for every origin (SPEC 9.2).
        with patch.object(settings, "draft_allowed_model_origins", []):
            failure = await self._run_expect_failure()

        self.assertEqual(failure.code, "provider_origin_not_allowed")
        self.assertFalse(failure.retryable)
        self.assertEqual(
            self.model.calls, [], "no provider call may be made after a policy refusal"
        )
        self.assertEqual(self._job_row()["error_code"], "provider_origin_not_allowed")
        self.assertEqual(self._job_row()["model_call_count"], 0)

    async def test_transient_error_is_retried_at_most_the_configured_limit(self):
        model = FakeModel(
            happy_responses(research=[ConnectionError("connection reset")])
        )
        failure = await self._run_expect_failure(model=model)

        limit = settings.draft_transient_retry_limit
        self.assertEqual(
            model.count("research"),
            limit + 1,
            "one initial attempt plus at most draft_transient_retry_limit retries",
        )
        self.assertEqual(failure.code, CODE_PROVIDER_UNAVAILABLE)
        # Surfaced to the caller, not retried again automatically.
        self.assertFalse(failure.retryable)
        self.assertEqual(self._job_row()["error_code"], CODE_PROVIDER_UNAVAILABLE)

    async def test_exactly_one_structured_output_repair_is_attempted_then_failure(self):
        model = FakeModel(happy_responses(outline=["not json at all"]))
        failure = await self._run_expect_failure(model=model)

        self.assertEqual(
            model.count("outline"), 2, "one original call plus exactly one repair"
        )
        repair_prompt = [
            p for s, p in zip(model.calls, model.prompts) if s == "outline"
        ][1]
        self.assertIn("REPAIR REQUEST", repair_prompt)
        self.assertEqual(failure.code, CODE_INVALID_STAGE_OUTPUT)
        self.assertFalse(failure.retryable)
        # A failed attempt now leaves an audit row, so assert the stage never
        # COMPLETED rather than that no row exists.
        self.assertNotIn("outline", self._completed_stage_names())
        self.assertIn("outline", self._stage_names())


# ── 13. Cancellation discards the in-flight result ───────────────────────────


class TestCancellation(PipelineTestBase):
    async def test_cancellation_during_a_provider_call_discards_the_result(self):
        job_id = self.job_id
        db_path = self._db_path

        def cancel_then_answer():
            # Cancellation is observed only after the provider has already
            # produced a perfectly valid outline.
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE draft_jobs SET cancel_requested_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            return outline_json()

        model = FakeModel(happy_responses(outline=[cancel_then_answer]))
        failure = await self._run_expect_failure(model=model)

        self.assertEqual(failure.code, CODE_JOB_CANCELLED)
        self.assertEqual(model.count("outline"), 1)
        # Cancellation discards the in-flight result entirely: it raises
        # _CompileCancelled rather than CompileFailure, so unlike a failed
        # stage it leaves NO row at all, completed or otherwise.
        self.assertNotIn("outline", self._stage_names())
        self.assertEqual(self._stage_names(), ["intake", "research"])
        self.assertEqual(self._job_row()["status"], "cancelled")
        self.assertEqual(self._draft_row()["status"], "cancelled")
        self.assertIsNone(self._current_revision())


# ── 14. Resume ───────────────────────────────────────────────────────────────


class TestResume(PipelineTestBase):
    """A checkpoint is reused only on a total match (SPEC 10.1 item 6)."""

    def _context(self):
        with self.pool.connection() as conn:
            job = DraftStore(conn).get_job(
                draft_id=self.draft_id, owner_id=OWNER_ID, job_id=self.job_id
            )
            return _build_context(conn, job, datetime(2026, 8, 1, tzinfo=timezone.utc))

    def _seed_intake_checkpoint(self, *, input_sha=None):
        """Write a completed intake stage row as a prior worker would have."""
        ctx = self._context()
        # Make the job resumable: fingerprint + prompt bundle must match.
        self.conn.execute(
            "UPDATE draft_jobs SET compile_input_sha256 = ?, "
            "prompt_bundle_version = ? WHERE id = ?",
            (ctx.compile_fingerprint, PROMPT_BUNDLE_VERSION, self.job_id),
        )
        self.conn.commit()

        real_sha = _stage_input_hash(
            "intake",
            ctx,
            {
                "inputs": [
                    [i.input_id, i.raw_sha256, i.parsed_sha256] for i in ctx.inputs
                ]
            },
        )
        manifest = IntakeManifest(
            brief_hash=ctx.brief_hash,
            inputs=[
                {
                    "input_id": i.input_id,
                    "role": i.role,
                    "raw_sha256": i.raw_sha256,
                    "parsed_sha256": i.parsed_sha256,
                    "character_count": i.character_count,
                }
                for i in ctx.inputs
            ],
            warnings=["no reference input supplied"],
        )
        artifact_json = canonical_json(manifest.model_dump(mode="json"))
        store = DraftStore(self.conn)
        row_id = store.record_stage_start(
            job_id=self.job_id,
            stage="intake",
            attempt=1,
            input_sha256=input_sha or real_sha,
        )
        store.record_stage_success(
            stage_row_id=row_id,
            artifact_json=artifact_json,
            artifact_sha256=sha256_text(artifact_json),
        )
        return real_sha

    async def test_matching_checkpoint_is_reused_and_the_stage_does_not_rerun(self):
        self._seed_intake_checkpoint()
        await self._run()

        intake_rows = [r for r in self._stages() if r.stage == "intake"]
        self.assertEqual(len(intake_rows), 1, "a matching checkpoint must be reused")
        self.assertEqual(intake_rows[0].attempt, 1)
        # The rest of the pipeline still ran on top of the reused checkpoint.
        self.assertEqual(
            [r.stage for r in self._stages()], list(COMPILE_STAGE_ORDER)
        )
        self.assertIsNotNone(self._current_revision())

    async def test_input_hash_mismatch_forces_the_stage_to_rerun(self):
        self._seed_intake_checkpoint(input_sha=sha256_text("a stale stage input"))
        await self._run()

        intake_rows = [r for r in self._stages() if r.stage == "intake"]
        self.assertEqual(len(intake_rows), 2, "a mismatched checkpoint must re-run")
        self.assertEqual([r.attempt for r in intake_rows], [1, 2])

    async def test_prompt_bundle_mismatch_forces_the_stage_to_rerun(self):
        self._seed_intake_checkpoint()
        self.conn.execute(
            "UPDATE draft_jobs SET prompt_bundle_version = 'not-this-bundle' "
            "WHERE id = ?",
            (self.job_id,),
        )
        self.conn.commit()

        await self._run()

        intake_rows = [r for r in self._stages() if r.stage == "intake"]
        self.assertEqual(len(intake_rows), 2, "a stale prompt bundle must re-run")

    async def test_fingerprint_mismatch_forces_the_stage_to_rerun(self):
        self._seed_intake_checkpoint()
        self.conn.execute(
            "UPDATE draft_jobs SET compile_input_sha256 = ? WHERE id = ?",
            (sha256_text("a different compile fingerprint"), self.job_id),
        )
        self.conn.commit()

        await self._run()

        intake_rows = [r for r in self._stages() if r.stage == "intake"]
        self.assertEqual(len(intake_rows), 2, "a stale fingerprint must re-run")


# ── 15. Fact ledger integrity ────────────────────────────────────────────────


class TestFactLedgerIntegrity(PipelineTestBase):
    async def test_evidence_label_absent_from_the_snapshot_cannot_be_supported(self):
        # The model cites S9, which this job never snapshotted.
        model = FakeModel(
            happy_responses(fact=[fact_json(status="supported", labels=("S9",))])
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(len(claims), 1)
        self.assertNotEqual(claims[0]["status"], "supported")
        self.assertEqual(claims[0]["status"], "unsupported")

        rules = {row["rule_id"] for row in self._findings()}
        self.assertIn("fact.evidence_label_not_snapshotted", rules)
        offending = [
            row
            for row in self._findings()
            if row["rule_id"] == "fact.evidence_label_not_snapshotted"
        ][0]
        self.assertEqual(offending["severity"], "blocker")
        self.assertEqual(offending["waivable"], 0)

        # No claim/source link may have been forged for a phantom label.
        revision = self._current_revision()
        links = self.conn.execute(
            "SELECT COUNT(*) FROM draft_claim_sources cs JOIN draft_claims c "
            "ON c.id = cs.claim_id WHERE c.revision_id = ?",
            (revision["id"],),
        ).fetchone()[0]
        self.assertEqual(links, 0)

    async def test_zero_result_claim_retrieval_still_records_a_retrieval_audit(self):
        model = FakeModel(
            happy_responses(fact=[fact_json(status="supported", labels=("S9",))])
        )
        await self._run(model=model)

        claims = self._claims()
        audit = json.loads(claims[0]["retrieval_audit_json"])
        self.assertEqual(claims[0]["status"], "unsupported")
        self.assertEqual(audit["normalized_query"], "The review window is 30 days")
        self.assertEqual(audit["returned_labels"], [])
        self.assertIn("vault_scope_hash", audit)
        self.assertIn("retrieved_at", audit)
        self.assertIn("retrieval_config", audit)
        # The retrieval genuinely happened and genuinely returned nothing.
        self.assertIn("The review window is 30 days", self.retriever.queries)

    async def test_a_quote_that_does_not_match_its_passage_is_not_supported(self):
        # The quoted run is absent from the snapshotted passage.
        model = FakeModel(
            happy_responses(
                draft=[
                    draft_section_json(
                        'The charter says "the window is 45 days" plainly. [S1]'
                    )
                ],
                fact=[
                    fact_json(
                        proposition='The charter says "the window is 45 days" plainly',
                        claim_type="quote",
                        status="supported",
                    )
                ],
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claim_type"], "quote")
        self.assertNotEqual(claims[0]["status"], "supported")
        self.assertEqual(claims[0]["status"], "unsupported")
        self.assertEqual(claims[0]["severity"], "blocker")

        mismatches = [
            row for row in self._findings() if row["rule_id"] == "fact.quote_mismatch"
        ]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["severity"], "blocker")
        self.assertEqual(mismatches[0]["waivable"], 0)
        self.assertEqual(self._current_revision()["fact_status"], "findings")

    async def test_a_matching_quote_is_pinned_to_the_snapshotted_passage(self):
        quoted = "the internal review window at 30 days"
        model = FakeModel(
            happy_responses(
                draft=[draft_section_json(f'The charter fixes "{quoted}". [S1]')],
                fact=[
                    fact_json(
                        proposition=f'The charter fixes "{quoted}"',
                        claim_type="quote",
                    )
                ],
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(claims[0]["status"], "supported")
        link = self.conn.execute(
            "SELECT relationship, exact_quote, lexical_overlap_score "
            "FROM draft_claim_sources cs JOIN draft_claims c ON c.id = cs.claim_id "
            "WHERE c.revision_id = ?",
            (self._current_revision()["id"],),
        ).fetchone()
        self.assertIsNotNone(link, "a supported quote must be pinned to evidence")
        self.assertEqual(link["relationship"], "supports")
        self.assertIn(link["exact_quote"], EVIDENCE_PASSAGE)


# ── Evidence snapshotting (supporting invariant for the ledger tests) ────────


class TestEvidenceSnapshot(PipelineTestBase):
    async def test_research_evidence_is_snapshotted_immutably_for_the_job(self):
        await self._run()

        rows = self.conn.execute(
            "SELECT label, source_kind, file_id, chunk_uid, title, passage, "
            "passage_sha256, source_content_sha256 FROM draft_evidence "
            "WHERE job_id = ? ORDER BY id",
            (self.job_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "S1")
        self.assertEqual(rows[0]["source_kind"], "document")
        self.assertEqual(rows[0]["file_id"], DOC_SOURCE.file_id)
        self.assertEqual(rows[0]["passage"], EVIDENCE_PASSAGE)
        self.assertEqual(rows[0]["passage_sha256"], sha256_text(EVIDENCE_PASSAGE))
        self.assertEqual(
            rows[0]["source_content_sha256"], DOC_SOURCE.content_sha256
        )


# ── Structural guarantee: the module never writes `ready` ────────────────────


class TestNeverWritesReady(unittest.TestCase):
    """SPEC 12.5 rule 8 / issue hard rule 7: no automatic path may set ``ready``."""

    @classmethod
    def setUpClass(cls):
        cls.path = Path(draft_pipeline.__file__)
        cls.source = cls.path.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_the_only_ready_literal_is_a_comparison_never_a_write(self):
        parents: dict[int, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node

        literals = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Constant) and node.value == "ready"
        ]
        self.assertTrue(literals, "expected the parse_status comparison to exist")
        for node in literals:
            self.assertIsInstance(
                parents.get(id(node)),
                ast.Compare,
                f"line {node.lineno}: a bare 'ready' literal outside a comparison "
                "could become a status write",
            )

    def test_no_sql_in_the_module_sets_a_status_to_ready(self):
        self.assertIsNone(
            re.search(r"status\s*=\s*'ready'", self.source),
            "SQL must never set a draft/job status to 'ready'",
        )
        self.assertIn("status = 'needs_review'", self.source)

    def test_set_job_status_is_only_ever_called_with_terminal_job_targets(self):
        targets = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "set_job_status":
                continue
            for keyword in node.keywords:
                if keyword.arg == "target":
                    self.assertIsInstance(keyword.value, ast.Constant)
                    targets.add(keyword.value.value)
        self.assertTrue(targets)
        self.assertTrue(targets <= {"completed", "failed", "cancelled"}, targets)

    def test_move_draft_refuses_any_target_other_than_failed_or_cancelled(self):
        with self.assertRaises(ValueError):
            draft_pipeline._move_draft(sqlite3.connect(":memory:"), 1, "ready")


# ── Research seam contract (documents the upstream bug) ──────────────────────



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
