"""Arm's-length acceptance specs for issues #1149/#1150, file A1 (checks C1-C6).

Authored BEFORE the production fix (plan
``.agents/issue-traces/1149-1150-workstream-a-draft-pipeline/07-approved-plan.md``,
work items F1-F4). These tests are the FROZEN acceptance contract for
not-yet-written behavior: every test in this file is expected to FAIL against
the current production tree (the red state), and to pass once the plan lands.

Red-state discipline: failures must be legible contract failures. Symbols the
plan ADDS (``OutlineModelOutput``, ``ProviderCallRecord``, ``CompletionResult``,
the new settings keys) are resolved lazily with ``getattr`` guards whose
assertion messages name the plan work item, and the new settings keys are
patched through a context manager that works whether or not the field is
declared yet (pydantic refuses ``setattr`` on undeclared fields, so
``patch.object(..., create=True)`` cannot be used pre-fix).

The pipeline harness mirrors ``tests/draft_room/test_draft_pipeline.py`` (real
temp SQLite DB, injected deterministic model/retrieval/clock doubles); it is
duplicated here rather than imported so this spec file stays self-contained
and collectable on its own.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import types
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.config import settings
from app.services import draft_pipeline, draft_prompts, draft_research
from app.services.draft_pipeline import PipelineDeps, _build_context, run_compile
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_store import DraftStore, canonical_json, sha256_text

# ── settings patching that works before the field is declared ────────────────


@contextmanager
def _patch_settings_key(name: str, value):
    """Patch ``settings.<name>`` even when the field is not declared yet.

    Pydantic v2 refuses plain ``setattr`` on undeclared fields, so the
    pre-fix tree cannot use ``patch.object(..., create=True)``. Setting the
    instance ``__dict__`` entry directly keeps the value visible to
    ``settings.<name>`` readers pre-fix (nothing reads it yet) and lets the
    normal ``patch.object`` path take over once the field is declared (F10).
    Restoration is exact: added keys are removed, declared keys restored.
    """
    if hasattr(settings, name) and name in type(settings).model_fields:
        with patch.object(settings, name, value):
            yield
        return
    object.__setattr__(settings, name, value)
    try:
        yield
    finally:
        settings.__dict__.pop(name, None)


# ── fixture constants ─────────────────────────────────────────────────────────

OWNER_ID = 91001
VAULT_ID = 91001

#: Loopback provider origin on the real Draft Room allowlist (SPEC 9.2); the
#: real provider-policy guard runs and passes without leaving the machine.
PROVIDER_URL = "http://127.0.0.1:11434"

MANUSCRIPT_TEXT = (
    "The internal review window for charter amendments is thirty days."
)
EVIDENCE_PASSAGE = (
    "Section 4 of the 2019 charter fixes the internal review window at 30 days."
)
EVIDENCE_TITLE = "Charter section 4"

#: The fake Draft desk emits one mid-band paragraph. The brief's target_words
#: (20) matches its word count so the post-fix length gates (F5) never fire on
#: these fixtures for reasons unrelated to the check under test.
SECTION_MARKDOWN = (
    "The internal review window for charter amendments is thirty days, and "
    "the committee records every granted extension in the ledger. [S1]"
)
CANDIDATE = SECTION_MARKDOWN

BRIEF = {
    "piece_type": "memo",
    "audience": "internal counsel",
    "purpose": "restate the review window",
    "target_words": 20,
    "transformation_strength": "moderate",
}


# ── deterministic retrieval doubles ──────────────────────────────────────────


@dataclass(frozen=True)
class FakeSource:
    """Mirrors ``rag_engine.RetrievedSource`` (plus the plan-F3 score_kind)."""

    kind: str
    title: str
    passage: str
    score: float
    content_sha256: str
    score_kind: str = "unknown"
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


_ALL_KINDS = frozenset({"document", "wiki", "kms"})


def _source(kind, title, *, score, score_kind, passage=None, content_sha256=None):
    passage = passage if passage is not None else f"Passage unique to {title}."
    return FakeSource(
        kind=kind,
        title=title,
        passage=passage,
        score=score,
        score_kind=score_kind,
        content_sha256=content_sha256
        or hashlib.sha256(passage.encode("utf-8")).hexdigest(),
        updated_at="2019-04-01T00:00:00Z",
        file_id=4242 if kind == "document" else None,
        chunk_uid=f"chunk-{title}" if kind == "document" else None,
    )


DOC_SOURCE = _source(
    "document",
    EVIDENCE_TITLE,
    score=0.71,
    score_kind="cosine_distance",
    passage=EVIDENCE_PASSAGE,
)


class FakeRetriever:
    """Returns the research evidence for facet queries, nothing for claims."""

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


# ── deterministic model double ───────────────────────────────────────────────

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

    def prompts_for(self, stage: str) -> list[str]:
        return [p for s, p in zip(self.calls, self.prompts) if s == stage]

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


# ── canonical stage payloads ─────────────────────────────────────────────────


def research_json() -> str:
    return json.dumps({"retrieval_status": "ok", "contradictions": [], "gaps": []})


def outline_json(section_count: int = 1, verdict: str = "approved", mode="rewrite"):
    """One approved outline. ``mode=None`` OMITS the key entirely (C1's
    model-omitted-mode arm)."""
    section = {
        "section_id": "sec-01",
        "heading": "Heading 1",
        "purpose": "state the review window",
        "target_words": 40,
        "evidence_labels": ["S1"],
        "must_preserve": [],
        "acceptance_checks": ["names the window"],
    }
    data = {
        "sections": [dict(section, section_id=f"sec-{n:02d}", heading=f"Heading {n}")
                     for n in range(1, section_count + 1)],
        "voice_rules": [],
        "critic": {"verdict": verdict, "findings": []},
    }
    if mode is not None:
        data["mode"] = mode
    return json.dumps(data)


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


def fact_json(
    *,
    proposition: str = "The internal review window for charter amendments is thirty days",
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


# ── compile harness (mirrors tests/draft_room/test_draft_pipeline.py) ────────


class _Pool(SimpleConnectionPool):
    """``SimpleConnectionPool`` plus the ``with pool.connection()`` idiom."""

    def connection(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            conn = self.get_connection()
            try:
                yield conn
            finally:
                self.release_connection(conn)

        return _cm()


class _Harness:
    """Real temp SQLite DB, a real compile job row, and injected fakes."""

    def __init__(self, *, mode="rewrite", title="Charter memo", brief=None):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = _Pool(self._db_path)
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

        self.clock = FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
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

        self.draft_id, self.input_id = self._make_draft_with_input(
            mode=mode, title=title, brief=brief or BRIEF
        )
        self.job_id = self._make_compile_job()
        self._seal(self.job_id)

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

    def _make_draft_with_input(self, *, mode, title, brief):
        draft = self.store.create_draft(
            vault_id=VAULT_ID,
            created_by=OWNER_ID,
            title=title,
            mode=mode,
            tier="standard",
            brief_json=json.dumps(brief),
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

    def _make_compile_job(self, *, parent_job_id=None, start_stage=None,
                          attempt_no=1):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version, "
            "compile_input_sha256, parent_job_id, start_stage, attempt_no) "
            "VALUES (?, ?, ?, 'compile', 'running', 40, 1800, ?, ?, ?, ?, ?)",
            (
                self.draft_id,
                VAULT_ID,
                OWNER_ID,
                PROMPT_BUNDLE_VERSION,
                None,
                parent_job_id,
                start_stage,
                attempt_no,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _seal(self, job_id):
        """Store the job's real compile fingerprint so resume/inheritance match."""
        with self.pool.connection() as conn:
            job = DraftStore(conn).get_job(
                draft_id=self.draft_id, owner_id=OWNER_ID, job_id=job_id
            )
            ctx = _build_context(conn, job, self.clock.start)
        self.conn.execute(
            "UPDATE draft_jobs SET compile_input_sha256 = ?, "
            "prompt_bundle_version = ? WHERE id = ?",
            (ctx.compile_fingerprint, PROMPT_BUNDLE_VERSION, job_id),
        )
        self.conn.commit()

    def make_retry_job(self, *, start_stage):
        """A retry job that may inherit this harness job's checkpoints."""
        retry_id = self._make_compile_job(
            parent_job_id=self.job_id, start_stage=start_stage, attempt_no=2
        )
        self._seal(retry_id)
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (self.draft_id,)
        )
        self.conn.commit()
        return retry_id

    # -- run helpers --

    async def run(self, *, job_id=None, model=None, retriever=None, clock=None):
        job_id = job_id or self.job_id
        model = model if model is not None else FakeModel(happy_responses())
        deps = PipelineDeps(
            retrieve_sources=retriever or FakeRetriever(),
            complete=model,
            now=clock or self.clock,
        )
        await run_compile(job_id=job_id, pool=self.pool, deps=deps)

    # -- readers --

    def stage_rows(self, stage, *, job_id=None):
        return self.conn.execute(
            "SELECT id, attempt, status, artifact_json, artifact_sha256, "
            "input_tokens, output_tokens FROM draft_job_stages "
            "WHERE job_id = ? AND stage = ? ORDER BY id",
            (job_id or self.job_id, stage),
        ).fetchall()

    def latest_completed_artifact(self, stage, *, job_id=None):
        rows = [
            r
            for r in self.stage_rows(stage, job_id=job_id)
            if r["status"] == "completed"
        ]
        assert rows, f"no completed {stage!r} stage row"
        return json.loads(rows[-1]["artifact_json"])

    def current_revision(self):
        return self.conn.execute(
            "SELECT id, content_md, content_sha256, fact_status, qa_summary_json "
            "FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (self.draft_id,),
        ).fetchone()

    def rewrite_outline_checkpoint_to_legacy(self, *, job_id):
        """Simulate a pre-F1 outline checkpoint: mode 'rewrite', nonempty
        sections, internally-consistent hashes (so ``_reusable`` trusts it)."""
        row = self.conn.execute(
            "SELECT id, artifact_json FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'outline' AND status = 'completed' "
            "ORDER BY attempt DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        assert row is not None, "no completed outline checkpoint to corrupt"
        data = json.loads(row["artifact_json"])
        data["mode"] = "rewrite"
        artifact_json = canonical_json(data)
        self.conn.execute(
            "UPDATE draft_job_stages SET artifact_json = ?, artifact_sha256 = ? "
            "WHERE id = ?",
            (artifact_json, sha256_text(artifact_json), row["id"]),
        )
        self.conn.commit()

    def close(self):
        for p in reversed(self._patches):
            p.stop()
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)


# ── C1 (#1149 AC1 / plan F1): the project, not the model, owns the mode ──────


async def test_outline_mode_is_stamped_from_project():
    # Fresh call: a compose project whose model emits a CONTRADICTORY mode.
    # The parent job FAILS at the fact desk (after outline completed, before
    # any revision is stored): checkpoint inheritance is bound to the compile
    # fingerprint, which carries prior_revision_sha256, so a post-SUCCESS
    # retry can never inherit (draft_pipeline.py:4100). A failed parent is
    # the reachable premise for the reuse leg below.
    h = _Harness(mode="compose")
    try:
        model = FakeModel(
            happy_responses(
                outline=[outline_json(mode="rewrite")],
                fact=[RuntimeError("synthetic fact-desk failure")],
            )
        )
        try:
            await h.run(model=model)
        except draft_pipeline.CompileFailure:
            pass
        else:
            raise AssertionError("the synthetic fact failure must fail the parent job")
        persisted = h.latest_completed_artifact("outline")
        assert persisted.get("mode") == "compose", (
            "plan F1: a compose-mode compile must persist OutlineArtifact.mode "
            f"stamped from the project ctx, got {persisted.get('mode')!r}"
        )

        # Checkpoint reuse: a legacy checkpoint carrying mode='rewrite' with
        # nonempty sections must be re-stamped to compose and RE-PERSISTED
        # under a new attempt on the reusing job — without a model call.
        h.rewrite_outline_checkpoint_to_legacy(job_id=h.job_id)
        retry_id = h.make_retry_job(start_stage="draft")
        model2 = FakeModel(
            happy_responses(outline=[])  # any outline model call fails loudly
        )
        await h.run(job_id=retry_id, model=model2)
        assert model2.count("outline") == 0, (
            "plan F1: the checkpoint-reuse path re-stamps the loaded artifact; "
            "it must not burn a model call"
        )
        rows = h.stage_rows("outline", job_id=retry_id)
        assert rows, "the retry job must carry the inherited outline checkpoint"
        latest = max(rows, key=lambda r: (r["attempt"], r["id"]))
        artifact = json.loads(latest["artifact_json"]) if latest["artifact_json"] else {}
        assert artifact.get("mode") == "compose", (
            "plan F1: checkpoint reuse must re-stamp a legacy model-chosen mode "
            f"from ctx; latest retry-job outline mode = {artifact.get('mode')!r}"
        )
        assert latest["status"] == "completed"
    finally:
        h.close()

    # Model-omitted mode: the model-facing schema has no `mode` field (F1), so
    # an output without one validates cleanly and the pipeline stamps compose.
    h2 = _Harness(mode="compose")
    try:
        model3 = FakeModel(happy_responses(outline=[outline_json(mode=None)]))
        await h2.run(model=model3)
        persisted2 = h2.latest_completed_artifact("outline")
        assert persisted2.get("mode") == "compose", (
            "plan F1: a model output without `mode` must validate (schema has "
            "no mode field) and be stamped from ctx"
        )
    finally:
        h2.close()


# ── C2 (#1149 AC2 / plan F2+R3-3): projected, section-scoped drafter brief ───


async def test_drafter_prompt_excludes_acceptance_checks():
    h = _Harness(mode="compose")
    try:
        model = FakeModel(happy_responses(outline=[outline_json(mode="compose")]))
        await h.run(model=model)
        prompts = model.prompts_for("draft")
        assert prompts, "the draft stage never called the model"
        prompt = prompts[0]
        assert "acceptance_checks" not in prompt, (
            "plan F2: the Draft-stage prompt must not surface the outline's "
            "acceptance_checks (the incident's authority-inversion vector)"
        )
        for key in (
            "section_heading",
            "section_purpose",
            "section_target_words",
            "section_evidence_labels",
        ):
            assert key in prompt, (
                f"plan F2/R3-3: the section-scoped Draft brief must carry the "
                f"`{key}` key for the section being drafted"
            )
    finally:
        h.close()


# ── C3 (#1149 AC3 / plan F2): compose drops transformation_strength ──────────


async def test_compose_prompts_omit_transformation_strength():
    h = _Harness(mode="compose")
    try:
        model = FakeModel(happy_responses(outline=[outline_json(mode="compose")]))
        await h.run(model=model)
        assert model.count("research") >= 1, (
            "evidence is present, so the research stage must call the model"
        )
        for stage in ("research", "outline", "draft", "copy", "standards", "fact"):
            prompts = model.prompts_for(stage)
            assert prompts, f"stage {stage!r} never called the model"
            for prompt in prompts:
                assert "transformation_strength" not in prompt, (
                    f"plan F2: in a compose-mode compile the {stage!r} prompt "
                    "must omit transformation_strength (one projection layer "
                    "for every model-facing render)"
                )
    finally:
        h.close()

    # Control: rewrite mode keeps transformation_strength in the rendered brief.
    h2 = _Harness(mode="rewrite")
    try:
        model2 = FakeModel(happy_responses())
        await h2.run(model=model2)
        outline_prompts = model2.prompts_for("outline")
        assert outline_prompts, "the outline stage never called the model"
        assert any(
            "transformation_strength" in p for p in outline_prompts
        ), "plan F2: rewrite mode still renders transformation_strength"
    finally:
        h2.close()


# ── C4 (#1149 AC4 / plan F3): evidence dedup by content hash ─────────────────


async def _run_research_with(sources):
    retriever = FakeRetriever(facet_sources=sources)

    async def complete(prompt, **kwargs):
        return research_json()

    with _patch_settings_key("draft_evidence_min_score", 0.05):
        return await draft_research.run_research(
            brief=dict(BRIEF),
            inputs=[
                {
                    "input_id": 1,
                    "role": "manuscript",
                    "text": MANUSCRIPT_TEXT,
                }
            ],
            vault_id=VAULT_ID,
            retrieve=retriever,
            complete=complete,
            limit=8,
            retry_limit=0,
        )


async def test_research_deduplicates_by_content_hash():
    shared_hash = hashlib.sha256(
        "the same document content retrieved repeatedly".encode("utf-8")
    ).hexdigest()
    sources = [
        _source(
            "document",
            f"Identical copy {i}",
            score=0.90,
            score_kind="cosine_distance",
            passage="the same document content retrieved repeatedly",
            content_sha256=shared_hash,
        )
        for i in range(1, 4)
    ]
    outcome = await _run_research_with(sources)
    vault = [e for e in outcome.evidence if e.source_kind == "document"]
    assert len(vault) == 1, (
        "plan F3: several vault passages sharing one content hash must "
        f"register exactly one evidence item; got {len(vault)} "
        f"({[e.label for e in vault]})"
    )


# ── C5 (#1149 AC5 / plan F3): typed minimum relevance ────────────────────────


async def test_research_applies_minimum_relevance():
    kept = [
        _source("document", "cos-good", score=0.10, score_kind="cosine_distance"),
        _source("document", "cos-equal", score=0.95, score_kind="cosine_distance"),
        _source("document", "l2-good", score=1.0, score_kind="l2_distance"),
        _source("document", "l2-equal", score=1.9, score_kind="l2_distance"),
        _source("document", "dot-good", score=0.10, score_kind="dot_distance"),
        _source("document", "rerank-good", score=0.5, score_kind="rerank"),
        _source("document", "fts-equal", score=0.5, score_kind="fts"),
        _source("wiki", "relation-high", score=1.5, score_kind="relation"),
        _source("wiki", "claimfts-good", score=0.2, score_kind="claim_fts"),
        _source("kms", "positional-good", score=0.9, score_kind="positional"),
        # positional is a BOUNDED kind (window neighbours from
        # document_retrieval carry it with score 1.0): its relevance is the
        # row's OWN score — never distance-rescaled — so the placeholder's
        # max score survives the gate (issue #1150 feedback FB-009). Under
        # cosine-distance rescaling this row would read 0.0 and drop.
        _source(
            "kms", "positional-never-gated", score=1.0, score_kind="positional"
        ),
        _source("kms", "kmsfts-good", score=0.3, score_kind="kms_fts"),
    ]
    dropped = [
        _source("document", "cos-far", score=0.99, score_kind="cosine_distance"),
        _source("document", "l2-far", score=1.91, score_kind="l2_distance"),
        _source("document", "dot-far", score=0.99, score_kind="dot_distance"),
        _source("document", "rerank-low", score=0.01, score_kind="rerank"),
        _source("document", "fts-low", score=0.4, score_kind="fts"),
        _source("wiki", "relation-low", score=0.02, score_kind="relation"),
        _source("wiki", "pagefts-low", score=0.04, score_kind="page_fts"),
    ]
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        with _patch_settings_key("draft_evidence_min_fts_score", 0.5):
            outcome = await _run_research_with(kept + dropped)
    titles = {e.title for e in outcome.evidence if e.source_kind != "draft_input"}
    missing = {s.title for s in kept} - titles
    assert not missing, (
        f"plan F3: eligible sources were gated out: {sorted(missing)} "
        "(normalized kinds clamp per kind; equality passes; fts gates on its "
        "own raw scale)"
    )
    leaked = {s.title for s in dropped} & titles
    assert not leaked, (
        f"plan F3: sources below the configured minimum were kept: "
        f"{sorted(leaked)}"
    )


# ── C6 (#1149 AC6 / plan F4): per-call audit ledger + stage tokens ───────────


class _TokenedModel(FakeModel):
    """Returns plan-F4 ``CompletionResult`` objects carrying provider metrics."""

    def __init__(self, responses, *, prompt_tokens=12, completion_tokens=34):
        super().__init__(responses)
        self._completion_cls = getattr(draft_prompts, "CompletionResult", None)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        raw = await super().__call__(
            prompt,
            logical_mode=logical_mode,
            temperature=temperature,
            sensitive=sensitive,
        )
        if isinstance(raw, str):
            assert self._completion_cls is not None, (
                "plan F4 contract missing: draft_prompts.CompletionResult"
            )
            return self._completion_cls(
                text=raw,
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                token_source="provider",
            )
        return raw


async def test_stage_records_call_timing_and_tokens():
    assert getattr(draft_prompts, "ProviderCallRecord", None) is not None, (
        "plan F4 contract missing: draft_prompts.ProviderCallRecord"
    )
    h = _Harness()
    try:
        model = _TokenedModel(happy_responses())
        await h.run(model=model)

        outline_rows = h.stage_rows("outline")
        completed = [r for r in outline_rows if r["status"] == "completed"]
        assert completed, "the outline stage never completed"
        row = completed[-1]
        assert row["input_tokens"] == 12, (
            "plan F4: the stage row's input_tokens must carry the stage's "
            f"aggregated provider-call totals; got {row['input_tokens']!r}"
        )
        assert row["output_tokens"] == 34, (
            "plan F4: the stage row's output_tokens must carry the stage's "
            f"aggregated provider-call totals; got {row['output_tokens']!r}"
        )

        artifact = json.loads(row["artifact_json"])
        calls = artifact.get("provider_calls")
        assert calls, (
            "plan F4: OutlineArtifact must carry a non-empty provider_calls ledger"
        )
        record = calls[0]
        for field in (
            "stage",
            "attempt_kind",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "token_source",
        ):
            assert field in record, (
                f"plan F4: ProviderCallRecord.{field} missing from the ledger"
            )
        assert record["stage"] == "outline"
        assert record["attempt_kind"] == "primary"
        assert record["input_tokens"] == 12
        assert record["output_tokens"] == 34
        assert record["token_source"] == "provider"
        assert isinstance(record["duration_ms"], float)

        for stage in ("research", "copy", "standards", "fact"):
            art = h.latest_completed_artifact(stage)
            assert art.get("provider_calls"), (
                f"plan F4: the {stage!r} stage artifact must forward its "
                "collected per-call records (reports constructed anew must not "
                "inherit an empty ledger)"
            )
        draft_art = h.latest_completed_artifact("draft")
        sections = draft_art.get("sections") or []
        assert sections and sections[0].get("provider_calls"), (
            "plan F4: DraftSection must carry its provider_calls ledger"
        )
    finally:
        h.close()
