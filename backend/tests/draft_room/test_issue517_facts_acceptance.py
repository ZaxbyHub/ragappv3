"""Issue #517 acceptance checks: Copy/Standards/Fact desk ledger contracts.

Discriminating regression tests (expected RED at HEAD d9e3460) for the
draft-review findings DRAFT-008, DRAFT-009, DRAFT-010, DRAFT-012 and
DRAFT-015:

* **AC3 (DRAFT-008)** — Copy/Standards desk ``findings`` (``list[str]`` on
  ``CopyReport``/``StandardsReport``) are persisted only inside stage
  artifacts and never become ``draft_findings`` rows; the checkpoint-reuse
  path drops them as well.
* **AC4 (DRAFT-009)** — SPEC 11.7's bounded Copy/Standards convergence loop
  is absent on the first pass: a semantic edit from the FIRST Standards run
  never re-triggers Copy before Fact.
* **AC5 (DRAFT-010)** — ``_resolve_claims`` drops any claim whose proposition
  is not a verbatim substring of the candidate, so a reported ``unsupported``
  paraphrase leaves no claim row, no blocker finding, and a ``passed``
  fact_status.
* **AC6 (DRAFT-012)** — claim-specific retrieval only runs when
  ``status == "unsupported"``; a *supported* factual claim gets no retrieval
  and its persisted ``retrieval_audit_json`` is the empty object.
* **AC7 (DRAFT-015)** — the correction-loop Copy desk prompt carries only the
  candidate; the Fact stage's findings never reach the desk that is supposed
  to correct them.

Harness copied from ``test_draft_pipeline.py`` (real temp SQLite database,
deterministic stage-routed fake model, deterministic fake retrieval, injected
clock) so the seams behave exactly as the permanent suite drives them. Each
discriminating test prints an ``AC<n> CHECK: FAIL`` sentinel immediately
before the assertion group the current tree fails, so the failure log matches
the expected regex.
"""

import hashlib
import json
import os
import re
import shutil
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
from app.services import draft_pipeline
from app.services.draft_pipeline import (
    PipelineDeps,
    _build_context,
    run_compile,
)
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_store import DraftStore, sha256_text

# ── Shared harness (mirrors test_draft_pipeline.py) ──────────────────────────


class PipelinePool(SimpleConnectionPool):
    """``SimpleConnectionPool`` plus the ``with pool.connection()`` idiom."""

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


OWNER_ID = 92001
VAULT_ID = 92001
PROVIDER_URL = "http://127.0.0.1:11434"

MANUSCRIPT_TEXT = (
    "The internal review window for charter amendments is thirty days."
)
EVIDENCE_PASSAGE = (
    "Section 4 of the 2019 charter fixes the internal review window at 30 days."
)
EVIDENCE_TITLE = "Charter section 4"
SECTION_MARKDOWN = "The review window is 30 days. [S1]"
CANDIDATE = SECTION_MARKDOWN

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
    """Records every query; answers with ``sources`` for the facet query.

    ``empty`` mode returns a genuinely-empty successful retrieval for every
    query (the empty-vault contract). Facet queries are recognized the same
    way ``test_draft_pipeline.FakeRetriever`` does; ``facet_query`` lets a
    test whose manuscript differs from the canonical one still be answered.
    """

    def __init__(self, *, sources=(DOC_SOURCE,), empty=False,
                 facet_query=MANUSCRIPT_TEXT) -> None:
        self.sources = tuple(sources)
        self.empty = empty
        self.facet_query = facet_query
        self.queries: list[str] = []

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        self.queries.append(query)
        if self.empty:
            matched: tuple = ()
        else:
            matched = (
                self.sources
                if query.strip().rstrip(".") == self.facet_query.strip().rstrip(".")
                else ()
            )
        return FakeRetrievalResult(
            status="ok",
            sources=matched,
            requested_kinds=_ALL_KINDS,
            successful_kinds=_ALL_KINDS,
            failed_kinds=frozenset(),
            source_only=not matched,
        )


_PROMPT_ID_RE = re.compile(r"PROMPT_ID: draft_room\.([a-z]+)\.v1")


class FakeModel:
    """Stage-routed fake ``complete`` that records every prompt verbatim.

    ``responses[stage]`` is a list consumed in order whose LAST element repeats
    forever. An element may be a ``str`` or a zero-argument callable.
    """

    def __init__(self, responses) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def count(self, stage: str) -> int:
        return sum(1 for s in self.calls if s == stage)

    def prompts_for(self, stage: str) -> list[str]:
        return [p for s, p in zip(self.calls, self.prompts) if s == stage]

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive=False):
        match = _PROMPT_ID_RE.search(prompt)
        if match is None:  # pragma: no cover - defensive
            raise AssertionError("prompt carries no recognizable PROMPT_ID")
        stage = match.group(1)
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
    def __init__(self, start: datetime, *, hold: int = 10**9) -> None:
        self.start = start
        self.hold = hold
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.start if self.calls <= self.hold else self.start + timedelta(
            hours=10
        )


# ── Canonical stage payloads ─────────────────────────────────────────────────


def research_json() -> str:
    return json.dumps(
        {"retrieval_status": "ok", "contradictions": [], "gaps": []}
    )


def outline_json(
    section_count: int = 1,
    *,
    labels=("S1",),
    must_preserve=(),
    verdict: str = "approved",
) -> str:
    return json.dumps(
        {
            "mode": "rewrite",
            "sections": [
                {
                    "section_id": f"sec-{n:02d}",
                    "heading": f"Heading {n}",
                    "purpose": "state the review window",
                    "target_words": 40,
                    "evidence_labels": list(labels),
                    "must_preserve": list(must_preserve),
                    "acceptance_checks": ["names the window"],
                }
                for n in range(1, section_count + 1)
            ],
            "voice_rules": [],
            "critic": {"verdict": verdict, "findings": []},
        }
    )


def draft_section_json(
    markdown: str = SECTION_MARKDOWN, labels=("S1",)
) -> str:
    return json.dumps(
        {
            "section_id": "sec-01",
            "markdown": markdown,
            "evidence_labels_used": list(labels),
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


def no_edits_json(findings=()) -> str:
    return json.dumps({"edits": [], "findings": list(findings)})


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
    findings=(),
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
                    "high_stakes": False,
                }
            ],
            "findings": list(findings),
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


# ── Base fixture ─────────────────────────────────────────────────────────────


class Issue517PipelineBase(unittest.IsolatedAsyncioTestCase):
    """Real temp SQLite DB, a real compile job row, and injected fakes."""

    maxDiff = None

    #: The parsed manuscript text seeded for this test's input. Subclasses
    #: may override to drive research/facet behavior with different text.
    MANUSCRIPT = MANUSCRIPT_TEXT

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

        self.manuscript_text = self.MANUSCRIPT
        self.draft_id, self.input_id = self._make_draft_with_input()
        self.job_id = self._make_compile_job()
        self.model: Optional[FakeModel] = None

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

    def tearDown(self):
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -- fixtures --

    def _make_draft_with_input(self, *, manuscript=None):
        manuscript = manuscript if manuscript is not None else self.manuscript_text
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
            size_bytes=len(manuscript),
            content_sha256=sha256_text(manuscript),
            max_inputs=10,
            max_total_input_bytes=10_000_000,
        )
        self.store.set_input_parse_status(input_id=record.id, target="parsing")
        self.store.set_input_parse_status(
            input_id=record.id,
            target="ready",
            parsed_text=manuscript,
            parsed_text_sha256=sha256_text(manuscript),
            parsed_char_count=len(manuscript),
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()
        return draft.id, record.id

    def _make_compile_job(self, *, max_model_calls=80, timeout_seconds=1800,
                          fingerprint=None, bundle=PROMPT_BUNDLE_VERSION):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version, "
            "compile_input_sha256) VALUES (?, ?, ?, 'compile', 'running', ?, ?, ?, ?)",
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

    def _deps(self, model=None, retriever=None) -> PipelineDeps:
        self.model = model or FakeModel(happy_responses())
        self.retriever = retriever or FakeRetriever()
        self.clock = FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
        return PipelineDeps(
            retrieve_sources=self.retriever,
            complete=self.model,
            now=self.clock,
        )

    async def _run(self, job_id=None, **kwargs) -> None:
        await run_compile(
            job_id=job_id or self.job_id,
            pool=self.pool,
            deps=self._deps(**kwargs),
        )

    # -- readers --

    def _job_row(self, job_id=None):
        return self.conn.execute(
            "SELECT status, error_code, output_revision_id FROM draft_jobs "
            "WHERE id = ?",
            (job_id or self.job_id,),
        ).fetchone()

    def _current_revision(self):
        return self.conn.execute(
            "SELECT id, content_md, content_sha256, fact_status, qa_summary_json "
            "FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (self.draft_id,),
        ).fetchone()

    def _findings(self, job_id=None):
        return self.conn.execute(
            "SELECT id, stage, rule_id, severity, message FROM draft_findings "
            "WHERE job_id = ? ORDER BY id ASC",
            (job_id or self.job_id,),
        ).fetchall()

    def _claims(self, revision_id):
        return self.conn.execute(
            "SELECT ordinal, claim_text, status, severity, claim_type, "
            "retrieval_audit_json FROM draft_claims WHERE revision_id = ? "
            "ORDER BY ordinal ASC",
            (revision_id,),
        ).fetchall()


# ── AC3 (DRAFT-008): desk findings must become ledger rows ───────────────────


class TestCopyStandardsFindingsPersisted(Issue517PipelineBase):
    """Copy/Standards ``findings`` strings must land in ``draft_findings``."""

    async def test_first_pass_desk_findings_become_draft_finding_rows(self):
        model = FakeModel(
            happy_responses(
                copy=[no_edits_json(findings=["ZZXCOPY-FINDING-7"])],
                standards=[no_edits_json(findings=["ZZXSTD-FINDING-9"])],
            )
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")

        print("AC3 CHECK: FAIL", flush=True)
        copy_rows = [
            row
            for row in self._findings()
            if row["stage"] == "copy" and "ZZXCOPY-FINDING-7" in row["message"]
        ]
        self.assertTrue(
            copy_rows,
            "the Copy desk's reported finding must be persisted as a "
            "draft_findings row with stage='copy' carrying its message",
        )
        standards_rows = [
            row
            for row in self._findings()
            if row["stage"] == "standards"
            and "ZZXSTD-FINDING-9" in row["message"]
        ]
        self.assertTrue(
            standards_rows,
            "the Standards desk's reported finding must be persisted as a "
            "draft_findings row with stage='standards' carrying its message",
        )

    async def test_reused_copy_checkpoint_still_lands_its_finding(self):
        """The checkpoint-reuse path must not silently drop desk findings.

        Run 1 fails at Fact (invalid structured output) AFTER Copy and
        Standards complete with findings embedded in their artifacts. Run 2
        resumes the SAME job: the Copy checkpoint is reused verbatim (no
        second model call), and its finding must still reach the ledger.
        """
        self._seal_fingerprint()

        run1_model = FakeModel(
            happy_responses(
                copy=[no_edits_json(findings=["ZZXCOPY-REUSE-3"])],
                standards=[no_edits_json(findings=["ZZXSTD-REUSE-4"])],
                fact=["not json at all"],
            )
        )
        with self.assertRaises(draft_pipeline.CompileFailure):
            await self._run(model=run1_model)
        self.assertEqual(self._job_row()["status"], "failed")

        # Reset the failed run's terminal states; the stage checkpoints stay.
        self.conn.execute(
            "UPDATE draft_jobs SET status = 'running', error_code = NULL "
            "WHERE id = ?",
            (self.job_id,),
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (self.draft_id,)
        )
        self.conn.commit()

        run2_model = FakeModel(happy_responses())
        await self._run(model=run2_model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: resume must succeed")
        # Fixture sanity: the copy/standards checkpoints really were reused.
        self.assertEqual(
            run2_model.count("copy"),
            0,
            "fixture: the copy checkpoint was expected to be reused",
        )
        self.assertEqual(run2_model.count("standards"), 0)
        self.assertIsNotNone(self._current_revision())

        print("AC3 CHECK: FAIL", flush=True)
        copy_rows = [
            row
            for row in self._findings()
            if row["stage"] == "copy" and "ZZXCOPY-REUSE-3" in row["message"]
        ]
        self.assertTrue(
            copy_rows,
            "a finding embedded in a REUSED copy checkpoint must still be "
            "persisted as a draft_findings row when the resumed compile "
            "assembles",
        )
        standards_rows = [
            row
            for row in self._findings()
            if row["stage"] == "standards" and "ZZXSTD-REUSE-4" in row["message"]
        ]
        self.assertTrue(
            standards_rows,
            "a finding embedded in a REUSED standards checkpoint must still "
            "be persisted as a draft_findings row",
        )

    def _seal_fingerprint(self):
        """Store the compile fingerprint so the job's checkpoints are resumable."""
        job = DraftStore(self.conn).get_job(
            draft_id=self.draft_id, owner_id=OWNER_ID, job_id=self.job_id
        )
        ctx = _build_context(
            self.conn, job, datetime(2026, 8, 1, tzinfo=timezone.utc)
        )
        self.conn.execute(
            "UPDATE draft_jobs SET compile_input_sha256 = ?, "
            "prompt_bundle_version = ? WHERE id = ?",
            (ctx.compile_fingerprint, PROMPT_BUNDLE_VERSION, self.job_id),
        )
        self.conn.commit()


# ── AC4 (DRAFT-009): pre-Fact Copy/Standards convergence loop ────────────────


class TestPreFactConvergenceLoop(Issue517PipelineBase):
    """A semantic edit from the FIRST Standards pass must re-run Copy."""

    async def test_first_pass_standards_semantic_edit_re_triggers_copy_before_fact(
        self,
    ):
        model = FakeModel(
            happy_responses(
                copy=[no_edits_json()],
                standards=[
                    edit_json(
                        CANDIDATE,
                        "30 days",
                        "30 business days",
                        semantic=True,
                    ),
                    edit_json(
                        CANDIDATE.replace("30 days", "30 business days"),
                        "30 business days",
                        "thirty business days",
                        semantic=True,
                    ),
                    no_edits_json(),
                ],
                fact=[fact_json(proposition="The review window")],
            )
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")

        print("AC4 CHECK: FAIL", flush=True)
        calls = model.calls
        first_standards = calls.index("standards")
        first_fact = calls.index("fact")
        self.assertLess(
            first_standards, first_fact, "fixture: Standards precedes Fact"
        )
        copy_between = [
            call
            for call in calls[first_standards + 1 : first_fact]
            if call == "copy"
        ]
        self.assertTrue(
            copy_between,
            "a semantic edit from the FIRST Standards pass must re-trigger "
            "the Copy desk BEFORE Fact runs (SPEC 11.7 convergence loop); "
            f"observed call order: {calls}",
        )

    async def test_repeated_semantic_edits_stop_at_the_bounded_cap(self):
        """The convergence loop is bounded by ``draft_qa_retry_limit``.

        Standards returns a fresh semantic edit on every call, so the loop can
        only exit via the cap. The compile must still finish, with Copy
        re-invoked at least once but never more than cap+1 times in total.
        """
        v0 = CANDIDATE
        # Candidate after the n-th applied edit says "{30+n} days".
        chain = [v0.replace("30 days", f"{30 + n} days") for n in range(0, 6)]
        standards_responses = [
            edit_json(chain[n], f"{30 + n} days", f"{31 + n} days", semantic=True)
            for n in range(0, 5)
        ]
        standards_responses.append(no_edits_json())

        model = FakeModel(
            happy_responses(
                copy=[no_edits_json()],
                standards=standards_responses,
                fact=[fact_json(proposition="The review window")],
            )
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        self.assertIsNotNone(self._current_revision())

        print("AC4 CHECK: FAIL", flush=True)
        copy_calls = model.count("copy")
        cap = settings.draft_qa_retry_limit
        self.assertGreaterEqual(
            copy_calls,
            2,
            "repeated semantic Standards edits must re-trigger Copy at least "
            "once before Fact (the convergence loop must actually run)",
        )
        self.assertLessEqual(
            copy_calls,
            cap + 1,
            "the pre-Fact convergence loop must stop after at most "
            "draft_qa_retry_limit re-triggered iterations",
        )


# ── AC5 (DRAFT-010): unmatched/paraphrased claims stay in the ledger ─────────


class TestParaphrasedClaimPreserved(Issue517PipelineBase):
    """An ``unsupported`` claim whose wording is paraphrased must stay
    visible: a ledger row, an adverse fact_status, and a blocker finding."""

    async def test_unmatched_unsupported_claim_is_recorded_and_adverse(self):
        # The proposition paraphrases the candidate rather than quoting it.
        model = FakeModel(
            happy_responses(
                fact=[
                    fact_json(
                        proposition="The review window lasts thirty days",
                        status="unsupported",
                    )
                ]
            )
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        revision = self._current_revision()
        self.assertIsNotNone(revision, "fixture: a revision must be stored")

        print("AC5 CHECK: FAIL", flush=True)
        claims = self._claims(revision["id"])
        matching = [
            row for row in claims if "thirty days" in row["claim_text"]
        ]
        self.assertTrue(
            matching,
            "an unmatched (paraphrased) unsupported claim must still be "
            "preserved as a draft_claims row with status='unsupported' rather "
            "than silently dropped",
        )
        self.assertEqual(matching[0]["status"], "unsupported")

        self.assertNotEqual(
            revision["fact_status"],
            "passed",
            "a revision carrying an unsupported claim must not be stamped "
            "fact_status='passed'",
        )

        blockers = [
            row
            for row in self._findings()
            if row["severity"] == "blocker" and row["stage"] == "fact"
        ]
        self.assertTrue(
            blockers,
            "the unmatched unsupported claim must surface a blocker-level "
            "finding, not only a warning",
        )


# ── AC6 (DRAFT-012): retrieval audit for supported claims ────────────────────


class TestSupportedClaimRetrievalAudit(Issue517PipelineBase):
    """A supported factual claim must get a claim-specific retrieval whose
    audit is persisted (SPEC 12.3), not an empty ``{}``."""

    async def test_supported_factual_claim_gets_a_retrieval_and_audit(self):
        proposition = "The review window is 30 days"
        model = FakeModel(
            happy_responses(fact=[fact_json(proposition=proposition)])
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        revision = self._current_revision()
        claims = self._claims(revision["id"])
        self.assertEqual(len(claims), 1, "fixture: the claim must be recorded")

        print("AC6 CHECK: FAIL", flush=True)
        self.assertIn(
            proposition,
            self.retriever.queries,
            "a supported factual claim must still get a claim-specific "
            "retrieval query (SPEC 12.3) — observed queries: "
            f"{self.retriever.queries}",
        )
        audit_blob = claims[0]["retrieval_audit_json"]
        self.assertNotEqual(
            audit_blob,
            "{}",
            "the persisted claim row must carry a real retrieval audit, not "
            "the empty object",
        )
        audit = json.loads(audit_blob)
        self.assertEqual(audit.get("normalized_query"), proposition)
        self.assertIn("vault_scope_hash", audit)


# ── AC7 (DRAFT-015): correction-loop prompt must carry Fact findings ─────────


class TestCorrectionLoopPromptCarriesFactFindings(Issue517PipelineBase):
    async def test_correction_copy_prompt_contains_fact_findings_and_context(self):
        marker = "ZZXMARKER Q7 feedback"
        model = FakeModel(
            happy_responses(
                fact=[
                    # A stale verdict with a verbatim span triggers the
                    # correction loop on the current tree.
                    fact_json(status="stale", findings=[marker]),
                    fact_json(),
                ]
            )
        )
        await self._run(model=model)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        copy_prompts = model.prompts_for("copy")
        self.assertGreaterEqual(
            len(copy_prompts), 2, "fixture: the correction loop must re-run Copy"
        )

        print("AC7 CHECK: FAIL", flush=True)
        correction_prompt = copy_prompts[1]
        self.assertIn(
            marker,
            correction_prompt,
            "the correction-loop Copy desk prompt must include the Fact "
            "stage's findings so the desk knows what to correct",
        )
        self.assertIn(
            CANDIDATE,
            correction_prompt,
            "the correction-loop Copy desk prompt must still carry the "
            "candidate context it is editing",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
