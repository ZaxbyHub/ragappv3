"""Arm's-length acceptance specs for issues #1149/#1150, file A2 (checks C7-C13
plus the plan-F8 scope items).

Authored BEFORE the production fix (plan
``.agents/issue-traces/1149-1150-workstream-a-draft-pipeline/07-approved-plan.md``,
work items F3-F6). Every test here except the explicitly-marked PRESERVING
controls is expected to FAIL against the current production tree (the red
state) and to pass once the plan lands.

Red-state discipline: the new ``run_deterministic_lint`` kwargs
(``target_words``/``sections``/``piece_title``) and the new symbols
(``SectionInput``, the new settings keys) are guarded with assertions that
name the plan work item, so the pre-fix failure is a legible contract gap.
Plan-added settings keys are patched through a context manager that works
whether or not the field is declared yet (pydantic refuses ``setattr`` on
undeclared fields, so ``patch.object(..., create=True)`` cannot be used
pre-fix).

PRESERVING controls (pass with the production fix withheld, per the plan F8
non-vacuity matrix): ``test_no_kwargs_run_deterministic_lint_compatibility``
pins base-rule behavior that pre-exists, and the negative arms inside the
duplication/exemption tests are only meaningful together with their positive
arms, which are discriminating.
"""

import asyncio
import hashlib
import inspect
import json
import logging
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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.api.routes import draft_room as _draft_room_route
from app.config import settings
from app.services import draft_pipeline, draft_prompts, draft_quality, draft_research
from app.services.draft_pipeline import PipelineDeps, _build_context, run_compile
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_store import DraftStore, canonical_json, sha256_text

# ── settings patching that works before the field is declared ────────────────


@contextmanager
def _patch_settings_key(name: str, value):
    """Patch ``settings.<name>`` even when the field is not declared yet."""
    if hasattr(settings, name) and name in type(settings).model_fields:
        with patch.object(settings, name, value):
            yield
        return
    object.__setattr__(settings, name, value)
    try:
        yield
    finally:
        settings.__dict__.pop(name, None)


# ── lint helpers (plan F5/F6) ────────────────────────────────────────────────


def _lint(
    candidate,
    *,
    needed=(),
    locked_spans=(),
    target_words=None,
    sections=None,
    piece_title=None,
    preserve_texts=None,
):
    """Call ``run_deterministic_lint``, first asserting the plan-F5 kwargs exist.

    ``needed`` names the new kwargs this call relies on; a missing kwarg is a
    legible contract failure instead of a bare ``TypeError``.
    """
    params = inspect.signature(draft_quality.run_deterministic_lint).parameters
    missing = sorted(k for k in needed if k not in params)
    assert not missing, (
        f"plan F5 contract missing on run_deterministic_lint kwargs: {missing}"
    )
    kwargs = {"locked_spans": list(locked_spans)}
    if target_words is not None:
        kwargs["target_words"] = target_words
    if sections is not None:
        kwargs["sections"] = sections
    if piece_title is not None:
        kwargs["piece_title"] = piece_title
    if preserve_texts is not None:
        kwargs["preserve_texts"] = preserve_texts
    return draft_quality.run_deterministic_lint(candidate, **kwargs)


def _section_inputs(triples):
    """Build plan-F5 ``SectionInput`` descriptors from (id, heading, budget)."""
    section_input = getattr(draft_quality, "SectionInput", None)
    assert section_input is not None, (
        "plan F5 contract missing: draft_quality.SectionInput"
    )
    return [
        section_input(section_id=sid, heading=heading, budget_words=budget)
        for sid, heading, budget in triples
    ]


def _blockers(report):
    return [f for f in report.findings if f.severity == "blocker"]


def _rule(report, rule_id):
    return [f for f in report.findings if f.rule_id == rule_id]


def _words(n):
    """``n`` unambiguous words (any sane tokenizer counts exactly ``n``)."""
    return " ".join(f"word{i}" for i in range(n))


def _count_words(text):
    """The length view's word basis: masked text, letter/apostrophe words."""
    masked = draft_quality.mask_excluded_spans(text)
    return len(re.findall(r"[A-Za-z']+", masked.masked))


BOILERPLATE_PHRASE = "it is important to note that"


# ── fixture constants (compile harness) ──────────────────────────────────────

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

SECTION_MARKDOWN = (
    "The internal review window for charter amendments is thirty days, and "
    "the committee records every granted extension in the ledger. [S1]"
)

#: A candidate whose final non-empty line is a trailing source citation
#: (C13 / plan F5 ``process.trailing_source_line``).
TRAILING_SOURCE_MARKDOWN = SECTION_MARKDOWN + "\n\nSource: [S1]"

BRIEF = {
    "piece_type": "memo",
    "audience": "internal counsel",
    "purpose": "restate the review window",
    "target_words": 20,
    "transformation_strength": "moderate",
}


# ── deterministic retrieval doubles (research-level scope items) ────────────


@dataclass(frozen=True)
class FakeSource:
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
    )


class FakeRetriever:
    def __init__(self, *, facet_sources=(), error=None) -> None:
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


DOC_SOURCE = _source(
    "document",
    EVIDENCE_TITLE,
    score=0.71,
    score_kind="cosine_distance",
    passage=EVIDENCE_PASSAGE,
)


async def _run_research(sources):
    retriever = FakeRetriever(facet_sources=sources)

    async def complete(prompt, **kwargs):
        return json.dumps({"contradictions": [], "gaps": []})

    return await draft_research.run_research(
        brief=dict(BRIEF),
        inputs=[{"input_id": 1, "role": "manuscript", "text": MANUSCRIPT_TEXT}],
        vault_id=VAULT_ID,
        retrieve=retriever,
        complete=complete,
        limit=8,
        retry_limit=0,
    )


# ── deterministic model double ───────────────────────────────────────────────

_PROMPT_ID_RE = re.compile(r"PROMPT_ID: draft_room\.([a-z]+)\.v1")


def _stage_of(prompt: str) -> str:
    match = _PROMPT_ID_RE.search(prompt)
    if match is None:  # pragma: no cover - defensive
        raise AssertionError("prompt carries no recognizable PROMPT_ID")
    return match.group(1)


class FakeModel:
    """Stage-routed fake ``complete`` (mirrors test_draft_pipeline.py)."""

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
    def __init__(self, start: datetime, *, hold: int = 10**9, jump=timedelta(0)):
        self.start = start
        self.hold = hold
        self.jump = jump
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.start if self.calls <= self.hold else self.start + self.jump


def research_json() -> str:
    return json.dumps({"retrieval_status": "ok", "contradictions": [], "gaps": []})


def outline_json(section_count: int = 1, verdict: str = "approved"):
    section = {
        "section_id": "sec-01",
        "heading": "Heading 1",
        "purpose": "state the review window",
        "target_words": 40,
        "evidence_labels": ["S1"],
        "must_preserve": [],
        "acceptance_checks": ["names the window"],
    }
    return json.dumps(
        {
            "mode": "rewrite",
            "sections": [
                dict(section, section_id=f"sec-{n:02d}", heading=f"Heading {n}")
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

    def _make_compile_job(self):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version, "
            "compile_input_sha256) "
            "VALUES (?, ?, ?, 'compile', 'running', 40, 1800, ?, NULL)",
            (self.draft_id, VAULT_ID, OWNER_ID, PROMPT_BUNDLE_VERSION),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _seal(self, job_id):
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

    async def run(self, *, model=None, retriever=None, clock=None):
        model = model if model is not None else FakeModel(happy_responses())
        deps = PipelineDeps(
            retrieve_sources=retriever or FakeRetriever(facet_sources=(DOC_SOURCE,)),
            complete=model,
            now=clock or self.clock,
        )
        await run_compile(job_id=self.job_id, pool=self.pool, deps=deps)

    def stage_rows(self, stage, *, job_id=None):
        return self.conn.execute(
            "SELECT id, attempt, status, artifact_json FROM draft_job_stages "
            "WHERE job_id = ? AND stage = ? ORDER BY id",
            (job_id or self.job_id, stage),
        ).fetchall()

    def stage_row_by_id(self, row_id):
        return self.conn.execute(
            "SELECT id, attempt, status FROM draft_job_stages WHERE id = ?",
            (row_id,),
        ).fetchone()

    def stage_names(self):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT stage FROM draft_job_stages WHERE job_id = ? ORDER BY id",
                (self.job_id,),
            ).fetchall()
        ]

    def current_revision(self):
        return self.conn.execute(
            "SELECT id, content_md, content_sha256, fact_status, qa_summary_json "
            "FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
            (self.draft_id,),
        ).fetchone()

    def findings(self, *, rule_id=None):
        rows = self.conn.execute(
            "SELECT id, stage, rule_id, severity, waivable, message, rule_version, "
            "span_start, span_end, status FROM draft_findings "
            "WHERE job_id = ? ORDER BY id",
            (self.job_id,),
        ).fetchall()
        if rule_id is None:
            return rows
        return [r for r in rows if r["rule_id"] == rule_id]

    def draft_lock_version(self):
        return int(
            self.conn.execute(
                "SELECT lock_version FROM drafts WHERE id = ?", (self.draft_id,)
            ).fetchone()[0]
        )

    def close(self):
        for p in reversed(self._patches):
            p.stop()
        self.pool.release_connection(self.conn)
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)


# ── C7 (#1150 AC1 / plan F5): length tolerance ───────────────────────────────


def test_length_outside_tolerance_is_a_blocker():
    report = _lint(_words(200), needed=("target_words",), target_words=100)
    blockers = _rule(report, "length.total_words")
    assert blockers and all(f.severity == "blocker" for f in blockers), (
        "plan F5: outside ±25% of target_words the total-length rule must be "
        "a blocker"
    )
    assert not _rule(report, "length.total_words_near"), (
        "plan F5: the near-band advisory must not fire when the blocker fired"
    )

    near = _lint(_words(120), needed=("target_words",), target_words=100)
    assert not _rule(near, "length.total_words"), (
        "+20% is inside the ±25% blocker band"
    )
    near_findings = _rule(near, "length.total_words_near")
    assert near_findings and all(f.severity == "advisory" for f in near_findings), (
        "plan F5: outside ±15% but inside ±25% the near-band advisory fires"
    )

    for boundary in (125, 115):
        at_band = _lint(_words(boundary), needed=("target_words",), target_words=100)
        assert not _rule(at_band, "length.total_words"), (
            f"exactly-at-band ({boundary} of 100) must pass the blocker"
        )
        assert not _rule(at_band, "length.total_words_near"), (
            f"exactly-at-band ({boundary} of 100) must pass the near advisory"
        )


# ── C8 (#1150 AC2 / plan F5): cross-section duplicate sentences ─────────────

_CROSS_SECTION_CANDIDATE = (
    "# First section\n\n"
    "Introductory remarks open the discussion. The committee reviewed the "
    "charter amendment proposal carefully before voting.\n\n"
    "# Second section\n\n"
    "Other opening remarks follow here. The committee reviewed the charter "
    "amendment proposal carefully before voting. A closing note."
)


def test_cross_section_duplicate_sentences_block():
    report = _lint(
        _CROSS_SECTION_CANDIDATE,
        needed=("sections",),
        sections=_section_inputs(
            [("sec-01", "First section", 40), ("sec-02", "Second section", 40)]
        ),
    )
    findings = _rule(report, "duplication.cross_section_sentence")
    assert findings and all(f.severity == "blocker" for f in findings), (
        "plan F5: a sentence of >=8 words appearing in more than one section "
        "must be a blocker"
    )
    # The duplicated sentence is exempt when carried by an outline
    # must_preserve span (SPEC §13.1: all occurrences, every duplicate rule).
    dup_sentence = (
        "The committee reviewed the charter amendment proposal carefully "
        "before voting"
    )
    exempted = _lint(
        _CROSS_SECTION_CANDIDATE,
        needed=("sections",),
        sections=_section_inputs(
            [("sec-01", "First section", 40), ("sec-02", "Second section", 40)]
        ),
        preserve_texts=[dup_sentence],
    )
    assert not _rule(exempted, "duplication.cross_section_sentence"), (
        "plan F5/SPEC 13.1: a must_preserve sentence repeated across sections "
        "is exempt from the cross-section rule"
    )
    finding = findings[0]
    assert 0 < finding.start < finding.end <= len(_CROSS_SECTION_CANDIDATE), (
        "plan F5/R5: every new finding carries a real, non-zero-length span"
    )
    second = _CROSS_SECTION_CANDIDATE.index("# Second section")
    assert finding.start >= second, (
        "plan F5: the cross-section finding names the sentence's span in its "
        "latest section"
    )


# ── C9 (#1150 AC3 / plan F5): process commentary ─────────────────────────────


def test_process_commentary_is_a_blocker():
    candidate = (
        "The window is thirty days. Verification was not performed for this "
        "claim. The board concurs."
    )
    report = _lint(candidate)
    commentary = [
        f for f in _blockers(report) if f.rule_id.startswith("process_commentary.")
    ]
    assert commentary, (
        "plan F5: pipeline-instruction leakage ('verification was not "
        "performed') must be a process_commentary blocker"
    )


# ── C10 (#1150 AC4 / plan F5): trailing source line ──────────────────────────


def test_trailing_source_line_is_a_blocker():
    for final_line in ("Source: [D1]", "Source: [S1], [W2].", "Source: D12"):
        candidate = f"The window is fixed by the charter.\n\n{final_line}"
        report = _lint(candidate)
        findings = _rule(report, "process.trailing_source_line")
        assert findings and all(f.severity == "blocker" for f in findings), (
            f"plan F5: final line {final_line!r} must be a trailing-source-line "
            "blocker (regex on the raw final non-empty line)"
        )

    inline = (
        "The registrar cited [D1] in the body of the memo.\n\n"
        "Closing prose follows here."
    )
    report = _lint(inline)
    assert not _rule(report, "process.trailing_source_line"), (
        "plan F5: inline evidence labels elsewhere in the candidate are not "
        "flagged as a trailing source line"
    )


# ── C11 (#1150 AC5 / plan F5): a correct candidate raises no blocker ────────

_MUST_PRESERVE_SENTENCE = (
    "The internal review window for charter amendments is thirty days."
)
_QUOTE = (
    '"Section 4 of the 2019 charter fixes the internal review window at '
    'thirty days."'
)
_CLEAN_CANDIDATE = (
    "# Charter review\n\n"
    f"{_MUST_PRESERVE_SENTENCE} [S1]\n\n"
    f"The charter states {_QUOTE} in its fourth section. [S1]\n\n"
    "# Adoption notes\n\n"
    "The board adopted the amendment on schedule after a recorded vote."
)


def test_clean_candidate_raises_no_blocker():
    locked_start = _CLEAN_CANDIDATE.index(_MUST_PRESERVE_SENTENCE)
    report = _lint(
        _CLEAN_CANDIDATE,
        needed=("target_words", "sections", "piece_title"),
        locked_spans=[(locked_start, locked_start + len(_MUST_PRESERVE_SENTENCE))],
        target_words=_count_words(_CLEAN_CANDIDATE),
        sections=_section_inputs(
            [("sec-01", "Charter review", 40), ("sec-02", "Adoption notes", 40)]
        ),
        piece_title="Charter memo",
    )
    assert _blockers(report) == [], (
        "plan F5: a correct candidate built from the outline's must_preserve "
        "facts (locked spans, quoted prose, on-budget sections, on-target "
        f"length) must raise no blocker; got {_blockers(report)}"
    )


# ── C12 (#1150 AC6 / plan F6): unsupported claims enter the correction loop ─


async def test_unsupported_claim_enters_correction_loop():
    h = _Harness()
    try:
        with patch.object(settings, "draft_qa_retry_limit", 2):
            model = FakeModel(happy_responses(fact=[fact_json(status="unsupported")]))
            await h.run(model=model)
        revision = h.current_revision()
        assert revision is not None, "the compile stored no revision"
        qa = json.loads(revision["qa_summary_json"])
        loops = int(qa.get("correction_loops", 0))
        assert 0 < loops <= 2, (
            "plan F6: an unsupported claim is a correctable status, so the "
            f"correction loop must run at least once within the cap; got {loops}"
        )
        assert h.stage_names().count("fact") == loops + 1
    finally:
        h.close()


# ── C13 (#1150 AC7 / plan F6): Ready refuses open new-kind blockers ─────────


async def test_ready_refused_while_lint_blocker_open():
    h = _Harness()
    try:
        with patch.object(settings, "draft_qa_retry_limit", 2):
            model = FakeModel(
                happy_responses(draft=[draft_section_json(TRAILING_SOURCE_MARKDOWN)])
            )
            await h.run(model=model)
        revision = h.current_revision()
        assert revision is not None, "the compile stored no revision"

        conn = h.conn
        try:
            _draft_room_route._sync_mark_ready(
                conn,
                draft_id=h.draft_id,
                owner_id=OWNER_ID,
                revision_id=revision["id"],
                lock_version=h.draft_lock_version(),
                acknowledge_source_only=False,
            )
        except _draft_room_route.DraftRoomHTTPError as exc:
            assert exc.status_code == 409, (
                f"plan F6/C13: mark-ready must refuse with 409 while an "
                f"unwaived new-kind blocker is open; got {exc.status_code}"
            )
        else:
            raise AssertionError(
                "plan F6/C13: mark-ready succeeded while the revision carries "
                "an unwaived process.trailing_source_line blocker"
            )
        conn.rollback()

        finding = h.findings(rule_id="process.trailing_source_line")
        assert finding, (
            "plan F5/F6: the trailing-source-line blocker must be persisted as "
            "a finding the Ready gate can see"
        )
        row = finding[0]
        content = revision["content_md"]
        span_hash = (
            sha256_text(content[row["span_start"] : row["span_end"]])
            if row["span_start"] is not None
            else None
        )
        h.store.waive_finding(
            draft_id=h.draft_id,
            finding_id=row["id"],
            resolved_by=OWNER_ID,
            resolution_note="Registrar confirmed the source line is intentional.",
            waiver_rule_version=row["rule_version"],
            waiver_text_sha256=span_hash,
        )

        _draft_room_route._sync_mark_ready(
            conn,
            draft_id=h.draft_id,
            owner_id=OWNER_ID,
            revision_id=revision["id"],
            lock_version=h.draft_lock_version(),
            acknowledge_source_only=False,
        )
        status = conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (h.draft_id,)
        ).fetchone()[0]
        assert status == "ready", (
            "plan F6/C13: after a valid waiver (actor, reason, rule version, "
            "unchanged span hash) Ready must succeed"
        )
    finally:
        h.close()


# ── scope: paragraph-level duplication ───────────────────────────────────────

_HASH_PARA = "Repeated paragraph body stands alone here"
_HASH_CANDIDATE = (
    "First opener paragraph with unique words.\n\n"
    f"{_HASH_PARA}.\n\n"
    "A middle bridging paragraph differs entirely.\n\n"
    f"{_HASH_PARA}."
)


def test_paragraph_hash_duplicate_is_a_blocker():
    report = _lint(_HASH_CANDIDATE)
    findings = _rule(report, "duplication.paragraph_hash")
    assert findings and all(f.severity == "blocker" for f in findings), (
        "plan F5: two+ paragraphs (any positions) sharing normalized text "
        "must be a paragraph_hash blocker"
    )


_SIM_BASE = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november",
]
_SIM_A = " ".join(_SIM_BASE)  # 14 distinct words
_SIM_B = " ".join(_SIM_BASE[:13])  # 13 shared -> Jaccard 13/14 >= 0.9, not identical
_SIM_CANDIDATE = f"{_SIM_A}.\n\nSome unrelated bridging sentence content.\n\n{_SIM_B}."


def test_paragraph_similarity_duplicate_is_a_blocker():
    report = _lint(_SIM_CANDIDATE)
    findings = _rule(report, "duplication.paragraph_similarity")
    assert findings and all(f.severity == "blocker" for f in findings), (
        "plan F5: two >=12-word paragraphs with word-set Jaccard >= 0.9 must "
        "be a paragraph_similarity blocker"
    )

    # A pair intersecting a locked span (either side) is excluded from the rule.
    start = _SIM_CANDIDATE.index(_SIM_A)
    guarded = _lint(
        _SIM_CANDIDATE, locked_spans=[(start, start + len(_SIM_A))]
    )
    assert not _rule(guarded, "duplication.paragraph_similarity"), (
        "plan F5: a paragraph pair intersecting a locked span is excluded "
        "from the duplicate rules"
    )


def test_must_preserve_repeated_occurrences_exempt_from_duplicate_rules():
    locked = "The registrar confirmed the figures in person before the vote"
    candidate = (
        f"{locked}.\n\nBridge paragraph with other content here.\n\n{locked}."
        f"\n\nA third paragraph differs.\n\n{locked}."
    )
    first = (0, len(locked))
    # Three copies: first-occurrence-only masking would still flag the third
    # copy, so this candidate discriminates all-occurrence from
    # first-occurrence exemption (issue #1150 feedback — the old 2-copy test
    # passed without the helper doing anything).
    guarded = _lint(candidate, locked_spans=[first])
    assert not _rule(guarded, "duplication.paragraph_hash"), (
        "plan F5: the duplicate rules must mask ALL occurrences of each "
        "locked-span text"
    )
    # The outline's must_preserve texts reach the same exemption through
    # preserve_texts WITHOUT any locked span (production shape — the
    # pipeline resolves only the first occurrence of a locked span).
    via_outline = _lint(candidate, preserve_texts=[locked])
    assert not _rule(via_outline, "duplication.paragraph_hash"), (
        "plan F5: outline must_preserve texts are exempt at all occurrences "
        "through the preserve_texts path"
    )
    unguarded = _lint(candidate)
    assert _rule(unguarded, "duplication.paragraph_hash"), (
        "positive control: without the locked span the repeated paragraph "
        "must be a blocker"
    )


# ── scope: section budgets and structure ─────────────────────────────────────


def test_section_over_budget_is_a_blocker():
    over = _lint(
        "# Head\n\n" + _words(61),
        needed=("sections",),
        sections=_section_inputs([("sec-01", "Head", 20)]),
    )
    assert _rule(over, "length.section_over_budget"), (
        "plan F5: 61 words against a 20-word budget (threshold "
        "max(1.5*20, 20+40)=60) must be over budget"
    )
    at_threshold = _lint(
        "# Head\n\n" + _words(59),
        needed=("sections",),
        sections=_section_inputs([("sec-01", "Head", 20)]),
    )
    assert not _rule(at_threshold, "length.section_over_budget"), (
        "plan F5: at the threshold (<= max(1.5*budget, budget+floor)) the "
        "section passes"
    )
    with _patch_settings_key("draft_quality_section_floor_words", 10):
        low_floor_over = _lint(
            "# Head\n\n" + _words(31),
            needed=("sections",),
            sections=_section_inputs([("sec-01", "Head", 20)]),
        )
        assert _rule(low_floor_over, "length.section_over_budget"), (
            "plan F5: with floor=10 the threshold is max(30, 30)=30, so 31 "
            "words is over budget (the 1.5x arm)"
        )
        low_floor_at = _lint(
            "# Head\n\n" + _words(29),
            needed=("sections",),
            sections=_section_inputs([("sec-01", "Head", 20)]),
        )
        assert not _rule(low_floor_at, "length.section_over_budget")


def test_multiple_h1_is_a_blocker():
    candidate = (
        "# First heading\n\nBody text here.\n\n# Second heading\n\nMore body follows."
    )
    report = _lint(candidate)
    findings = _rule(report, "structure.multiple_h1")
    assert findings and all(f.severity == "blocker" for f in findings), (
        "plan F5: more than one H1 outside code fences must be a blocker"
    )
    fenced = (
        "# Only heading\n\nBody text here.\n\n```\n# fenced markup, not a "
        "heading\n```\n\nMore body follows."
    )
    assert not _rule(_lint(fenced), "structure.multiple_h1"), (
        "an H1-looking line inside a code fence is not a heading"
    )


def test_heading_repeats_title_unit_gate():
    candidate = "# Charter review\n\nBody text here."
    fires = _lint(
        candidate, needed=("piece_title",), piece_title="CHARTER   REVIEW"
    )
    findings = _rule(fires, "structure.heading_repeats_title")
    assert findings and all(f.severity == "blocker" for f in findings), (
        "plan F5: a heading whose normalized text equals the normalized "
        "piece_title must be a blocker"
    )
    disabled = _lint(candidate, needed=("piece_title",), piece_title="")
    assert not _rule(disabled, "structure.heading_repeats_title"), (
        "plan F5: an empty piece_title disables heading_repeats_title"
    )


async def test_heading_repeats_title_comes_from_the_draft_title():
    h = _Harness(
        title="Charter Review",
        brief=dict(BRIEF, target_words=22),
    )
    try:
        model = FakeModel(
            happy_responses(
                draft=[
                    draft_section_json(
                        "# Charter Review\n\n" + SECTION_MARKDOWN
                    )
                ]
            )
        )
        await h.run(model=model)
        rows = h.findings(rule_id="structure.heading_repeats_title")
        assert rows, (
            "plan F5: piece_title is snapshotted from the draft itself, so a "
            "candidate heading repeating the draft title must surface as a "
            "blocker finding"
        )
        assert rows[0]["severity"] == "blocker"
    finally:
        h.close()


def test_section_mapping_survives_interleaved_body_paragraphs():
    # Issue #1150 feedback (P-F02/F-004, hardened after review): sections
    # map by HEADING ordinal, not the index over all blocks — a body
    # paragraph between headings must not shift later sections onto the
    # wrong budget. The budgets make a dropped s2 boundary OBSERVABLE: s2's
    # 61-word body is over s2's 60-word threshold, and under any body-words
    # under s1's/s3's thresholds, so exactly one finding naming s2 proves
    # the boundary survived.
    candidate = (
        "# One\n\n" + _words(20) + "\n\n"
        "A standalone bridge paragraph between sections.\n\n"
        "# Two\n\n" + _words(61) + "\n\n"
        "Another bridge paragraph between sections.\n\n"
        "# Three\n\n" + _words(20)
    )
    report = _lint(
        candidate,
        needed=("sections",),
        sections=_section_inputs(
            [("s1", "One", 100), ("s2", "Two", 20), ("s3", "Three", 100)]
        ),
    )
    over = list(_rule(report, "length.section_over_budget"))
    assert len(over) == 1 and over[0].section_id == "s2", (
        "bridge paragraphs must not shift section mapping: exactly one "
        "over-budget finding naming s2 proves the s2 boundary survived; "
        f"got {[(f.section_id, f.message) for f in over]}"
    )
    assert over[0].start >= candidate.index("# Two"), (
        "the finding must be anchored inside s2's own block"
    )


def test_section_mapping_partial_heading_text_match_keeps_boundaries():
    # Issue #1150 feedback round 2: a PARTIAL heading text match used to
    # discard every unmatched planned section — a reworded middle heading
    # merged its body into the previous section's budget. The reworded
    # heading must still carry s2; the deeper-level sub-heading and the
    # planned-name-carrying heading must never steal the slot.
    candidate = (
        "# Alpha\n\n" + _words(20) + "\n\n"
        "# Beta Revised\n\n" + _words(65) + "\n\n"
        "## Beta detail note\n\n"
        "# Gamma\n\n" + _words(20)
    )
    report = _lint(
        candidate,
        needed=("sections",),
        sections=_section_inputs(
            [("s1", "Alpha", 100), ("s2", "Beta", 20), ("s3", "Gamma", 100)]
        ),
    )
    over = list(_rule(report, "length.section_over_budget"))
    assert len(over) == 1 and over[0].section_id == "s2", (
        "the reworded s2 heading must keep its boundary (s2 is the only "
        f"over-budget section); got {[(f.section_id, f.message) for f in over]}"
    )
    assert over[0].start >= candidate.index("# Beta Revised"), (
        "the finding must be anchored inside s2's own block"
    )


def test_section_mapping_leading_reworded_heading_keeps_budget():
    # Issue #1150 feedback round 3: a reworded FIRST section's heading sits
    # BEFORE any exact match, so the carrier pass must still map it — with
    # candidate counts equal, every unmapped heading at the matched level
    # maps 1:1 in document order.
    candidate = (
        "# Alpha Revised\n\n" + _words(70) + "\n\n"
        "# Beta\n\n" + _words(20)
    )
    report = _lint(
        candidate,
        needed=("sections",),
        sections=_section_inputs([("s1", "Alpha", 20), ("s2", "Beta", 100)]),
    )
    over = list(_rule(report, "length.section_over_budget"))
    assert len(over) == 1 and over[0].section_id == "s1", (
        "the reworded leading heading must carry s1's budget (exactly one "
        f"finding naming s1); got {[(f.section_id, f.message) for f in over]}"
    )
    assert over[0].start >= candidate.index("# Alpha Revised"), (
        "the finding must be anchored inside s1's own block"
    )


def test_section_mapping_glued_headings_keep_boundaries():
    # Issue #1150 feedback round 18: valid Markdown does not require a
    # blank line before a heading — a heading glued to the preceding body
    # must still start its own section. Boundaries are discovered from
    # ATX heading LINES, not blank-line blocks.
    candidate = (
        "# Alpha\n" + _words(20) + "\n"
        "# Beta\n" + _words(65)
    )
    report = _lint(
        candidate,
        needed=("sections",),
        sections=_section_inputs([("s1", "Alpha", 100), ("s2", "Beta", 20)]),
    )
    over = list(_rule(report, "length.section_over_budget"))
    assert len(over) == 1 and over[0].section_id == "s2", (
        "the glued Beta heading must start s2's own section (exactly one "
        f"finding naming s2); got {[(f.section_id, f.message) for f in over]}"
    )
    assert over[0].start >= candidate.index("# Beta"), (
        "the finding must be anchored inside s2's own glued block"
    )


def test_no_heading_candidate_disables_section_rules_but_total_length_applies():
    sections = _section_inputs(
        [("sec-01", "Alpha", 20), ("sec-02", "Beta", 20)]
    )
    candidate = _words(150)  # one paragraph, no ATX headings
    report = _lint(
        candidate,
        needed=("sections", "target_words"),
        sections=sections,
        target_words=200,
    )
    assert not _rule(report, "length.section_over_budget"), (
        "plan F5: no headings in the candidate disables section-scoped rules"
    )
    assert not _rule(report, "duplication.cross_section_sentence")
    assert not _rule(report, "length.total_words"), (
        "150 of 200 is exactly at the -25%% band edge and must pass"
    )

    too_short = _lint(
        candidate,
        needed=("sections", "target_words"),
        sections=sections,
        target_words=400,
    )
    assert _rule(too_short, "length.total_words"), (
        "plan F5: the whole-document total-length rule still applies to an "
        "unsectioned candidate"
    )


# ── scope: waiver reuse on a new rule ────────────────────────────────────────


async def test_waiver_reuse_on_new_rule_invalidated_by_version_bump_and_text_edit():
    h = _Harness()
    try:
        with patch.object(settings, "draft_qa_retry_limit", 2):
            model = FakeModel(
                happy_responses(draft=[draft_section_json(TRAILING_SOURCE_MARKDOWN)])
            )
            await h.run(model=model)
        revision = h.current_revision()
        assert revision is not None
        finding = h.findings(rule_id="process.trailing_source_line")
        assert finding, "the new-kind blocker must be persisted before waiving"
        row = finding[0]
        content = revision["content_md"]
        span_hash = (
            sha256_text(content[row["span_start"] : row["span_end"]])
            if row["span_start"] is not None
            else None
        )
        h.store.waive_finding(
            draft_id=h.draft_id,
            finding_id=row["id"],
            resolved_by=OWNER_ID,
            resolution_note="Reviewed and accepted for the record.",
            waiver_rule_version=row["rule_version"],
            waiver_text_sha256=span_hash,
        )

        def _mark_ready():
            _draft_room_route._sync_mark_ready(
                h.conn,
                draft_id=h.draft_id,
                owner_id=OWNER_ID,
                revision_id=revision["id"],
                lock_version=h.draft_lock_version(),
                acknowledge_source_only=False,
            )

        # Version bump: a re-lint under a new rule version rewrites the
        # finding's rule_version; the stale waiver must be refused.
        h.conn.execute(
            "UPDATE draft_findings SET rule_version = ? WHERE id = ?",
            ("bumped-2", row["id"]),
        )
        h.conn.commit()
        try:
            _mark_ready()
        except _draft_room_route.DraftRoomHTTPError as exc:
            assert exc.status_code == 409
            assert exc.code == "invalid_waiver", (
                f"expected invalid_waiver after a rule-version bump, got "
                f"{exc.code!r}"
            )
        else:
            raise AssertionError(
                "a waiver whose rule_version no longer matches the finding "
                "must be refused at Ready"
            )
        h.conn.rollback()

        # Text edit: bytes at the waived span changed after the waiver.
        h.conn.execute(
            "UPDATE draft_findings SET rule_version = ? WHERE id = ?",
            (row["rule_version"], row["id"]),
        )
        edited = content.replace("Source: [S1]", "Source: [S9]")
        assert edited != content
        h.conn.execute(
            "UPDATE draft_revisions SET content_md = ? WHERE id = ?",
            (edited, revision["id"]),
        )
        h.conn.commit()
        try:
            _mark_ready()
        except _draft_room_route.DraftRoomHTTPError as exc:
            assert exc.status_code == 409
            assert exc.code == "stale_waiver", (
                f"expected stale_waiver after a text edit, got {exc.code!r}"
            )
        else:
            raise AssertionError(
                "a waiver whose span text changed after it was granted must "
                "be refused at Ready"
            )
        h.conn.rollback()
    finally:
        h.close()


# ── scope: evidence gating details (plan F3) ─────────────────────────────────


def _vault_titles(outcome):
    return {
        e.title for e in outcome.evidence if e.source_kind != "draft_input"
    }


async def test_dedup_filters_before_marking_the_hash_registry():
    shared_hash = hashlib.sha256(
        b"identical bytes retrieved twice at different scores"
    )
    sources = [
        _source(
            "document",
            "below-threshold-first",
            score=0.99,  # relevance 0.01 < 0.05 -> ineligible
            score_kind="cosine_distance",
            passage="identical bytes retrieved twice at different scores",
            content_sha256=shared_hash.hexdigest(),
        ),
        _source(
            "document",
            "eligible-second",
            score=0.10,
            score_kind="cosine_distance",
            passage="identical bytes retrieved twice at different scores",
            content_sha256=shared_hash.hexdigest(),
        ),
    ]
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        outcome = await _run_research(sources)
    titles = _vault_titles(outcome)
    assert titles == {"eligible-second"}, (
        "plan F3: the gate runs BEFORE the hash registry is marked, so an "
        "ineligible duplicate must not consume the hash and the later "
        f"eligible occurrence wins; got {sorted(titles)}"
    )


async def test_exact_threshold_relevance_passes():
    sources = [
        _source("document", "exact-boundary", score=0.95, score_kind="cosine_distance"),
        _source("document", "just-below", score=0.959, score_kind="cosine_distance"),
    ]
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        outcome = await _run_research(sources)
    titles = _vault_titles(outcome)
    assert "exact-boundary" in titles, (
        "plan F3: relevance == the configured minimum passes (strict <)"
    )
    assert "just-below" not in titles


async def test_lone_fts_row_gated_on_its_own_scale():
    fts_source = lambda score: _source(  # noqa: E731
        "document", f"fts-{score}", score=score, score_kind="fts"
    )
    with _patch_settings_key("draft_evidence_min_fts_score", 0.60):
        excluded = await _run_research([fts_source(0.55)])
    assert not _vault_titles(excluded), (
        "plan F3/R4-1: a lone fts row at 0.55 must be excluded when "
        "draft_evidence_min_fts_score=0.60"
    )
    assert excluded.source_only is True, (
        "plan F3: an all-filtered run keeps the source_only semantics"
    )
    assert any(
        e.kind == "draft_input" for e in excluded.packet.evidence
    ), "the minted [D#] input snapshots still ship on an all-filtered run"

    with _patch_settings_key("draft_evidence_min_fts_score", 0.5):
        default_scale = await _run_research([fts_source(0.55)])
    assert _vault_titles(default_scale) == {"fts-0.55"}, (
        "at the 0.5 default the same 0.55 row is kept"
    )

    with _patch_settings_key("draft_evidence_min_fts_score", 0.60):
        equal = await _run_research([fts_source(0.6)])
    assert _vault_titles(equal) == {"fts-0.6"}, (
        "an fts row exactly at the configured minimum passes (strict <)"
    )


async def test_singleton_far_vector_dropped():
    far = _source("document", "only-and-far", score=0.99, score_kind="cosine_distance")
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        outcome = await _run_research([far])
    assert not _vault_titles(outcome), (
        "plan F3/R3-1: a lone far-vector cosine row is dropped on ABSOLUTE "
        "relevance (no peer-max normalization)"
    )
    assert outcome.source_only is True


async def test_unknown_score_kind_never_dropped():
    sources = [
        _source("document", "unknown-kind", score=0.999, score_kind="unknown"),
        _source("document", "known-far", score=0.99, score_kind="cosine_distance"),
    ]
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        outcome = await _run_research(sources)
    titles = _vault_titles(outcome)
    assert "unknown-kind" in titles, (
        "plan F3: score_kind 'unknown' is never gated (fail-open)"
    )
    assert "known-far" not in titles, (
        "a concrete-kind far vector IS gated (the control for fail-open)"
    )


async def test_l2_rescale_equivalence():
    sources = [
        _source("document", "cos-half", score=0.5, score_kind="cosine_distance"),
        _source("document", "l2-one", score=1.0, score_kind="l2_distance"),
        _source("document", "cos-bad", score=0.96, score_kind="cosine_distance"),
        _source("document", "l2-bad", score=2.1, score_kind="l2_distance"),
    ]
    with _patch_settings_key("draft_evidence_min_score", 0.05):
        outcome = await _run_research(sources)
    titles = _vault_titles(outcome)
    assert {"cos-half", "l2-one"} <= titles, (
        "plan F3: l2_distance 1.0 rescales to the same relevance as "
        "cosine_distance 0.5 (the /2 convention) and both pass"
    )
    assert not ({"cos-bad", "l2-bad"} & titles), (
        "cosine 0.96 (relevance 0.04) and l2 2.1 (relevance clamped to 0.0) "
        "are both below the minimum"
    )


# ── scope: prompt projection details (plan F2) ──────────────────────────────


async def test_continuity_text_is_labelled_already_covered():
    section_b = (
        "Beyond the window itself, the committee also tracks every amendment "
        "the board adopts. [S1]"
    )
    h = _Harness(brief=dict(BRIEF, target_words=33))
    try:
        model = FakeModel(
            happy_responses(
                outline=[outline_json(section_count=2)],
                draft=[
                    draft_section_json(SECTION_MARKDOWN),
                    draft_section_json(section_b),
                ],
            )
        )
        await h.run(model=model)
        prompts = model.prompts_for("draft")
        assert len(prompts) >= 2, "a two-section outline must draft twice"
        second = prompts[1]
        assert "Continuity (already covered" in second and (
            "do not repeat" in second
        ), (
            "plan F2: continuity context must be labelled "
            "'Continuity (already covered — do not repeat): <tail>'"
        )
        assert "committee records every granted extension" in second, (
            "the continuity tail is the previous section's last paragraphs"
        )
    finally:
        h.close()


async def test_rendered_prompts_logged_as_ids_only_never_bodies(caplog):
    """Issue #1149 feedback (FB-001/P-F09): SPEC 9.2:769 forbids prompt
    bodies at EVERY level, so the per-render DEBUG log carries content-free
    identifiers (stage, sha256, length) and never the rendered body."""
    h = _Harness()
    try:
        with caplog.at_level(logging.DEBUG):
            manuscript = MANUSCRIPT_TEXT
            await h.run()
    finally:
        h.close()
    prompt_records = [
        r for r in caplog.records if "rendered prompt" in r.getMessage()
    ]
    assert prompt_records, (
        "the per-render DEBUG log (content-free identifiers) must still fire"
    )
    assert all(r.levelno == logging.DEBUG for r in prompt_records)
    body_markers = ["ASSIGNMENT BRIEF", "OUTPUT SCHEMA", manuscript]
    for record in caplog.records:
        message = record.getMessage()
        for marker in body_markers:
            assert marker not in message, (
                "SPEC 9.2:769: prompt bodies must never reach any log "
                f"record (marker {marker!r} leaked at level {record.levelno})"
            )


# ── scope: stage-attempt lifecycle (plan F4) ─────────────────────────────────


class _GatedModel(FakeModel):
    """Parks one stage's model call between two events (issue #126-style)."""

    def __init__(self, responses, *, gate_stage, started, release, error=None):
        super().__init__(responses)
        self.gate_stage = gate_stage
        self.started = started
        self.release = release
        self.error = error
        self.fired = False

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        stage = _stage_of(prompt)
        if stage == self.gate_stage and not self.fired:
            self.fired = True
            self.started.set()
            await asyncio.wait_for(self.release.wait(), timeout=30)
            if self.error is not None:
                raise self.error
        return await super().__call__(
            prompt,
            logical_mode=logical_mode,
            temperature=temperature,
            sensitive=sensitive,
        )


async def _settle_task(task):
    """Drain a parked background compile task without re-raising its failure."""
    if task.done():
        return
    try:
        await asyncio.wait_for(task, timeout=30)
    except (draft_pipeline.CompileFailure, asyncio.TimeoutError, RuntimeError):
        pass


async def test_stage_row_open_while_model_in_flight_and_completed_after():
    h = _Harness()
    started, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(happy_responses(), gate_stage="outline",
                        started=started, release=release)
    task = asyncio.create_task(h.run(model=model))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)

        running = [
            r for r in h.stage_rows("outline") if r["status"] == "running"
        ]
        assert running, (
            "plan F4: the stage row must be OPEN (running) BEFORE the slow "
            "model call returns"
        )
        row_id = running[0]["id"]

        release.set()
        await task
        row = h.stage_row_by_id(row_id)
        assert row["status"] == "completed", (
            "plan F4: the SAME row opened at stage entry must be completed on "
            f"success; got {row['status']!r}"
        )
    finally:
        release.set()
        await _settle_task(task)
        h.close()


async def test_stage_row_in_flight_is_settled_failed_without_a_second_row():
    h = _Harness()
    started, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        happy_responses(),
        gate_stage="outline",
        started=started,
        release=release,
        error=RuntimeError("synthetic provider failure"),
    )
    task = asyncio.create_task(h.run(model=model))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)

        running = [
            r for r in h.stage_rows("outline") if r["status"] == "running"
        ]
        assert running, (
            "plan F4: the stage row must be OPEN (running) while the model "
            "call is in flight"
        )
        release.set()
        try:
            await task
        except draft_pipeline.CompileFailure:
            pass
        else:
            raise AssertionError("the synthetic provider failure must fail the job")

        rows = h.stage_rows("outline")
        assert len(rows) == 1, (
            "plan F4: the failure path must CLOSE the open row, not open a "
            f"second one; got {len(rows)} rows"
        )
        assert rows[0]["status"] == "failed"
    finally:
        release.set()
        await _settle_task(task)
        h.close()


async def test_mid_stage_cancellation_settles_open_rows_job_cancelled():
    """FB-003: the outer cancellation path settles the open row as failed
    with job_cancelled — no row survives 'running' on a cancelled task."""
    h = _Harness()
    started, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        happy_responses(), gate_stage="outline", started=started, release=release
    )
    task = asyncio.create_task(h.run(model=model))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        running = [
            r for r in h.stage_rows("outline") if r["status"] == "running"
        ]
        assert running, "the outline row must be open while the call is parked"
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=30)
        except (draft_pipeline.CompileFailure, asyncio.CancelledError):
            pass
        rows = h.conn.execute(
            "SELECT id, status, error_code, artifact_json FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'outline'",
            (h.job_id,),
        ).fetchall()
        assert rows and all(r["status"] == "failed" for r in rows), (
            "FB-003: the cancelled stage's open row must settle as failed; "
            f"got {[tuple(r) for r in rows]}"
        )
        assert rows[0]["error_code"] == draft_pipeline.CODE_JOB_CANCELLED, (
            "FB-003: the cancellation path must record job_cancelled, not "
            f"{rows[0]['error_code']!r}"
        )
        settled = json.loads(rows[0]["artifact_json"] or "{}")
        cancelled_calls = settled.get("provider_calls") or []
        assert len(cancelled_calls) == 1 and (
            cancelled_calls[0].get("stage") == "outline"
        ), (
            "FB-006: the cancelled in-flight attempt must be recorded exactly "
            f"once on the settled row; got {cancelled_calls!r}"
        )
    finally:
        release.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (
            draft_pipeline.CompileFailure,
            asyncio.CancelledError,
            RuntimeError,
            asyncio.TimeoutError,
        ):
            pass
        h.close()


class _SecondCopyFails(FakeModel):
    """Fails the SECOND copy call (the correction loop's nested desk)."""

    def __init__(self, responses):
        super().__init__(responses)
        self.copy_calls = 0

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        if _stage_of(prompt) == "copy":
            self.copy_calls += 1
            if self.copy_calls == 2:
                raise RuntimeError("synthetic nested-desk failure")
        return await super().__call__(
            prompt,
            logical_mode=logical_mode,
            temperature=temperature,
            sensitive=sensitive,
        )


async def test_nested_desk_failure_settles_fact_and_desk_rows_with_ledgers():
    """FB-006: a nested-desk failure inside the Fact correction loop closes
    the open desk row with its own failed-attempt ledger, and the settled
    failure artifacts still carry the complete job ledger — the first-pass
    Fact call survives (the old shared-ledger reset erased it before any
    settle could persist it)."""
    h = _Harness()
    try:
        with patch.object(settings, "draft_qa_retry_limit", 2):
            model = _SecondCopyFails(
                happy_responses(fact=[fact_json(status="unsupported")])
            )
            try:
                await h.run(model=model)
            except draft_pipeline.CompileFailure:
                pass
            else:
                raise AssertionError("the nested desk failure must fail the job")

        copy_rows = h.stage_rows("copy")
        assert len(copy_rows) == 2, (
            "FB-006: one pre-fact copy row + one correction-loop copy row; "
            f"got {len(copy_rows)}"
        )
        assert copy_rows[0]["status"] == "completed"
        assert copy_rows[1]["status"] == "failed", (
            "the failed nested-desk row must be closed, not left running; "
            f"got {copy_rows[1]['status']!r}"
        )
        desk_artifact = json.loads(copy_rows[1]["artifact_json"] or "{}")
        desk_calls = [
            c for c in (desk_artifact.get("provider_calls") or [])
            if c.get("stage") == "copy"
        ]
        assert desk_calls, (
            "the correction-loop copy row must carry its own failed-attempt "
            "ledger (the RuntimeError attempt is recorded before the "
            "terminal failure)"
        )
        settled_calls = desk_artifact.get("provider_calls") or []
        settled_stages = {c.get("stage") for c in settled_calls}
        assert settled_stages == {"copy"}, (
            "FB-006: the settled failure artifact carries the failed row's "
            f"OWN attempt window only; got {sorted(settled_stages)}"
        )

        fact_rows = h.stage_rows("fact")
        assert fact_rows and all(
            r["status"] == "completed" for r in fact_rows
        ), (
            "the first-pass Fact row completes at its verdict BEFORE the "
            f"correction loop runs; got {[r['status'] for r in fact_rows]}"
        )
        fact_artifact = json.loads(fact_rows[-1]["artifact_json"] or "{}")
        fact_calls = [
            c for c in (fact_artifact.get("provider_calls") or [])
            if c.get("stage") == "fact"
        ]
        assert fact_calls, (
            "FB-006: the Fact row's own ledger carries its first-pass call"
        )
    finally:
        h.close()


async def test_fact_reentry_row_carries_only_its_own_window():
    """FB-006 (r4 probe P3): the correction loop opens a fresh Fact row per
    pass, and each row's ledger carries exactly its OWN pass's call — no
    double-counting of an earlier iteration's paid call (the ledger window
    opens with the row)."""
    h = _Harness()
    try:
        with patch.object(settings, "draft_qa_retry_limit", 2):
            model = FakeModel(
                happy_responses(
                    fact=[fact_json(status="unsupported"), fact_json()]
                )
            )
            await h.run(model=model)
        fact_rows = h.stage_rows("fact")
        assert len(fact_rows) == 2, (
            "the correction loop opens a fresh Fact row per pass; got "
            f"{len(fact_rows)}"
        )
        window_sizes = []
        for row in fact_rows:
            artifact = json.loads(row["artifact_json"] or "{}")
            window_sizes.append(
                len(
                    [
                        c
                        for c in (artifact.get("provider_calls") or [])
                        if c.get("stage") == "fact"
                    ]
                )
            )
        assert window_sizes == [1, 1], (
            "each Fact row's ledger carries exactly its own pass's call "
            f"(no double-counting across iterations); got {window_sizes}"
        )
    finally:
        h.close()


async def test_cancellation_during_terminal_settle_still_reaches_caller():
    """r12 review: a failure verdict is NEVER rerouted by a cancellation
    arriving during the terminal settle — the settle returns normally (the
    caller persists the failure), the interrupted write is retried, and the
    row settles with the run's actual terminal code."""
    h = _Harness()
    try:
        with h.pool.connection() as conn:
            row_id = DraftStore(conn).record_stage_start(
                job_id=h.job_id,
                stage="outline",
                attempt=1,
                input_sha256="a" * 64,
            )
        run = object.__new__(draft_pipeline._CompileRun)
        run._open_rows = {"outline": row_id}
        run._open_tasks = {}
        run._terminal = False
        run._terminal_code = draft_pipeline.CODE_JOB_CANCELLED
        run._call_marks = {"outline": 0}
        run._stage_calls = []
        run._pool = h.pool
        calls = {"n": 0}
        loop = asyncio.get_running_loop()
        test_task = asyncio.current_task()

        def flaky_settle(row_id, code, inputs, outputs, records=None):
            calls["n"] += 1
            if calls["n"] == 1:
                loop.call_soon_threadsafe(test_task.cancel)
            with h.pool.connection() as conn:
                DraftStore(conn).record_stage_failure(
                    stage_row_id=row_id,
                    error_code=code,
                    error_message="probe settle",
                    input_tokens=None,
                    output_tokens=None,
                    artifact_json=None,
                )

        run._db_settle_row = flaky_settle
        # A FAILURE verdict is never rerouted by a late cancellation: the
        # settle returns normally (the caller re-raises the original
        # failure; run_compile persists failed), the row is settled with
        # the true verdict, and the interrupted write was retried.
        await run._settle_open_rows(draft_pipeline.CODE_PROVIDER_UNAVAILABLE)
        rows = h.conn.execute(
            "SELECT status, error_code FROM draft_job_stages WHERE id = ?",
            (row_id,),
        ).fetchall()
        assert rows and rows[0]["status"] == "failed", (
            "the interrupted terminal pass must still settle the row"
        )
        assert rows[0]["error_code"] == draft_pipeline.CODE_PROVIDER_UNAVAILABLE, (
            "the late-settled row records the run's ACTUAL terminal code"
        )
        assert calls["n"] >= 2, (
            "the retry pass must have re-settled the interrupted row"
        )
    finally:
        h.close()


async def test_cancellation_during_settle_of_cancelled_run_propagates():
    """r12: when the run's verdict is ALREADY a cancellation, a fresh
    cancellation captured during the terminal settle propagates to the
    caller — the persisted verdict is cancelled either way."""
    h = _Harness()
    try:
        with h.pool.connection() as conn:
            row_id = DraftStore(conn).record_stage_start(
                job_id=h.job_id,
                stage="outline",
                attempt=1,
                input_sha256="b" * 64,
            )
        run = object.__new__(draft_pipeline._CompileRun)
        run._open_rows = {"outline": row_id}
        run._open_tasks = {}
        run._terminal = False
        run._terminal_code = draft_pipeline.CODE_JOB_CANCELLED
        run._call_marks = {"outline": 0}
        run._stage_calls = []
        run._pool = h.pool
        calls = {"n": 0}
        loop = asyncio.get_running_loop()
        test_task = asyncio.current_task()

        def flaky_settle(row_id, code, inputs, outputs, records=None):
            calls["n"] += 1
            if calls["n"] == 1:
                loop.call_soon_threadsafe(test_task.cancel)
            with h.pool.connection() as conn:
                DraftStore(conn).record_stage_failure(
                    stage_row_id=row_id,
                    error_code=code,
                    error_message="probe settle",
                    input_tokens=None,
                    output_tokens=None,
                    artifact_json=None,
                )

        run._db_settle_row = flaky_settle
        with pytest.raises(asyncio.CancelledError):
            await run._settle_open_rows(draft_pipeline.CODE_JOB_CANCELLED)
        rows = h.conn.execute(
            "SELECT status, error_code FROM draft_job_stages WHERE id = ?",
            (row_id,),
        ).fetchall()
        assert rows and rows[0]["status"] == "failed", (
            "the row must still be settled despite the propagated cancellation"
        )
        assert rows[0]["error_code"] == draft_pipeline.CODE_JOB_CANCELLED
    finally:
        h.close()


async def test_failure_verdict_survives_cancel_during_terminal_settle():
    """r13 outer-seam: a REAL compile whose terminal settle is hit by a
    fresh cancellation still persists the FAILURE verdict — run_compile
    persists failed, never cancelled, because the stage handler re-raises
    the original exception after a failure-verdict settle."""
    h = _Harness()
    started, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        happy_responses(),
        gate_stage="outline",
        started=started,
        release=release,
        error=RuntimeError("synthetic provider failure"),
    )
    calls = {"n": 0}
    loop = asyncio.get_running_loop()
    holder: dict = {}
    orig = draft_pipeline.DraftStore.record_stage_failure

    def cancel_once(self, **kwargs):
        calls["n"] += 1
        task = holder.get("task")
        if calls["n"] == 1 and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        return orig(self, **kwargs)

    try:
        task = asyncio.create_task(h.run(model=model))
        holder["task"] = task
        await asyncio.wait_for(started.wait(), timeout=30)
        with patch.object(
            draft_pipeline.DraftStore, "record_stage_failure", cancel_once
        ):
            release.set()
            try:
                await asyncio.wait_for(task, timeout=30)
            except draft_pipeline.CompileFailure:
                pass
            else:
                raise AssertionError("the synthetic failure must fail the job")
        status = h.conn.execute(
            "SELECT status, error_code FROM draft_jobs WHERE id = ?",
            (h.job_id,),
        ).fetchone()
        assert status["status"] == "failed", (
            "the job must persist FAILED even though a fresh cancellation "
            f"landed during the terminal settle; got {tuple(status)}"
        )
        assert status["error_code"] != draft_pipeline.CODE_JOB_CANCELLED, (
            "the job's error_code must not be rerouted to job_cancelled"
        )
        assert calls["n"] >= 1, "the settle write must have been intercepted"
    finally:
        release.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (
            draft_pipeline.CompileFailure,
            asyncio.CancelledError,
            RuntimeError,
            asyncio.TimeoutError,
        ):
            pass
        h.close()


# ── scope: legacy-checkpoint compatibility (plan F4) ─────────────────────────


def test_legacy_checkpoint_artifacts_validate_with_new_defaults():
    legacy_outline = {
        "mode": "compose",
        "sections": [
            {
                "section_id": "sec-01",
                "heading": "H",
                "purpose": "p",
                "target_words": 40,
                "evidence_labels": [],
                "must_preserve": [],
                "acceptance_checks": [],
            }
        ],
        "voice_rules": [],
        "critic": {"verdict": "approved", "findings": []},
    }
    artifact = draft_prompts.OutlineArtifact.model_validate_json(
        canonical_json(legacy_outline)
    )
    provider_calls = getattr(artifact, "provider_calls", None)
    assert provider_calls is not None and list(provider_calls) == [], (
        "plan F4: OutlineArtifact.provider_calls must exist with a legacy-safe "
        "default so old checkpoint JSON validates"
    )

    legacy_audit = {
        "prompt_id": "p",
        "prompt_version": "1",
        "prompt_sha256": "x",
        "model": "m",
        "temperature": 0.0,
        "output_sha256": "y",
    }
    audit = draft_prompts.ModelCallAudit.model_validate_json(
        canonical_json(legacy_audit)
    )
    assert getattr(audit, "duration_ms", None) == 0.0, (
        "plan F4: ModelCallAudit.duration_ms must default to 0.0 for old JSON"
    )


# ── scope: PRESERVING control — existing callers of run_deterministic_lint ──


def test_draft_path_retrieved_source_constructors_set_concrete_score_kinds():
    """Plan F3 producer census: no Draft-path RetrievedSource is constructed
    without a concrete score_kind (source-text pin, repo convention for
    wiring claims invisible to behavior tests)."""
    import inspect as _inspect

    from app.services import rag_engine as _re

    source = _inspect.getsource(_re)
    constructions = re.findall(r"RetrievedSource\(", source)
    assert constructions, "RetrievedSource construction sites disappeared"
    for kind_call in (
        "score_kind=chunk.score_type or default_score_kind",
        'score_kind=getattr(ev, "score_type", "unknown") or "unknown"',
    ):
        assert kind_call in source, (
            f"plan F3 census: Draft-path construction is missing the typed "
            f"score_kind wiring ({kind_call!r})"
        )


def test_completion_result_from_metrics_provenance_matrix():
    """Mutation pins for the token-usage producer (issue #1149 feedback
    F-11/P-F11: the previous suite never caught a plain-str regression, a
    dropped usage field, or swapped/garbled counts)."""
    from app.services.draft_pipeline import _completion_result_from_metrics
    from app.services.draft_prompts import CompletionResult

    # provider-exact
    r = _completion_result_from_metrics("t", {"prompt_tokens": 10, "completion_tokens": 20})
    assert (r.prompt_tokens, r.completion_tokens, r.token_source) == (10, 20, "provider")
    # estimate-only
    r = _completion_result_from_metrics(
        "t", {"prompt_tokens_estimate": 4, "completion_tokens_estimate": 6}
    )
    assert (r.prompt_tokens, r.completion_tokens, r.token_source) == (4, 6, "estimate")
    # one exact + one estimate = mixed (the lone-exact count is NOT discarded)
    r = _completion_result_from_metrics(
        "t", {"prompt_tokens": 10, "prompt_tokens_estimate": 9, "completion_tokens_estimate": 6}
    )
    assert (r.prompt_tokens, r.completion_tokens, r.token_source) == (10, 6, "mixed")
    # booleans never fabricate provider-exact counts (int(True) == 1)
    r = _completion_result_from_metrics("t", {"prompt_tokens": True, "completion_tokens": True})
    assert r.token_source != "provider"
    # garbage usage degrades to unavailable instead of failing the call
    r = _completion_result_from_metrics(
        "t", {"prompt_tokens": "bogus", "completion_tokens": None}
    )
    assert r.prompt_tokens is None and r.completion_tokens is None


def test_unicode_word_tokenizer_counts_non_latin_and_digits():
    """FB-007 (P-F03): the tokenizer is Unicode-aware and keeps digits, so
    non-Latin drafts are measured and number-differing paragraphs are not
    read as duplicates."""
    cyrillic = "Это тестовый текст для проверки подсчёта слов в абзаце."
    report = _lint(cyrillic * 10, needed=("target_words",), target_words=100)
    # ~10 words x 10 = 100 words: exactly at target, no length blocker.
    assert not _rule(report, "length.total_words")
    # digits distinguish paragraphs: 'Table 3.' vs 'Table 7.' is NOT a
    # duplicate pair; an ASCII-only tokenizer drops the digits and would
    # read the two paragraphs as identical.
    a = "The board reviewed table 3 appendix entries at the meeting today"
    b = "The board reviewed table 7 appendix entries at the meeting today"
    report = _lint(f"{a}.\n\n{b}.")
    assert not _rule(report, "duplication.paragraph_similarity")
    assert not _rule(report, "duplication.paragraph_hash"), (
        "dropping digits must not collapse the pair into a hash duplicate"
    )


def test_process_commentary_does_not_fire_on_ordinary_prose():
    """FB-008 (P-F05): the anchored patterns must not block reported speech
    or domain vocabulary."""
    for prose in (
        "Each release passes an acceptance check before it ships.",
        "As an AI researcher, she published the replication study.",
        "The panel heard testimony about verification of the logs.",
        "I could not verify the figures with the registrar myself.",
        "I don't have access to the building after hours.",
    ):
        report = _lint(prose)
        assert not [
            f for f in _blockers(report) if f.rule_id.startswith("process_commentary")
        ], f"false positive on ordinary prose: {prose!r}"
    # true positives still fire — but only when the object is a pipeline
    # artifact (the acceptance_check noun family)
    for leak in (
        "The acceptance checks for this section were not completed.",
        "I cannot verify this section against the cited records.",
        "I do not have access to the draft records.",
    ):
        report = _lint(leak)
        assert [
            f for f in _blockers(report) if f.rule_id.startswith("process_commentary")
        ], f"the anchored pattern must still catch pipeline transcription: {leak!r}"


def test_similarity_and_cross_section_findings_are_capped():
    """FB-010 (P-F06): degenerate repetition output cannot produce hundreds
    of thousands of blockers — the duplicate rules are capped."""
    para = " ".join(_SIM_BASE[:14])
    candidate = "\n\n".join(
        f"{para} variant {i} closing words here" for i in range(500)
    )
    report = _lint(candidate)
    sim_findings = [
        f
        for f in report.findings
        if f.rule_id == "duplication.paragraph_similarity"
    ]
    assert len(sim_findings) <= draft_quality._MAX_DUPLICATE_FINDINGS
    beyond = [
        f
        for f in report.findings
        if f.rule_id == "duplication.similarity_beyond_cap"
    ]
    assert len(beyond) == 1, (
        "a 500-paragraph candidate also trips the fail-closed guard "
        "(its tail is beyond the comparison cap)"
    )

    # the HASH rule is capped too (verbatim-repeat loops)
    repeat_candidate = "\n\n".join(
        "The board confirmed the same schedule again" for _ in range(500)
    )
    report = _lint(repeat_candidate)
    hash_findings = [
        f for f in report.findings if f.rule_id == "duplication.paragraph_hash"
    ]
    assert len(hash_findings) <= draft_quality._MAX_DUPLICATE_FINDINGS, (
        f"the paragraph-hash rule must be capped; got {len(hash_findings)}"
    )

    # the cross-section rule caps INSIDE one section body: a single
    # paragraph of repeated sentences cannot out-run the cap between
    # section-boundary checks
    sentence = (
        "The committee reviewed the charter amendment proposal carefully "
        "before voting today"
    )
    cross_candidate = (
        "# Alpha\n\n" + sentence + ".\n\n"
        "# Beta\n\n" + ". ".join(sentence for _ in range(150)) + "."
    )
    report = _lint(
        cross_candidate,
        needed=("sections",),
        sections=_section_inputs([("sa", "Alpha", 200), ("sb", "Beta", 200)]),
    )
    cross_findings = [
        f
        for f in report.findings
        if f.rule_id == "duplication.cross_section_sentence"
    ]
    assert len(cross_findings) <= draft_quality._MAX_DUPLICATE_FINDINGS, (
        "the cross-section rule must cap inside a section body; got "
        f"{len(cross_findings)}"
    )

    # FAIL CLOSED (r4 review): beyond the input cap the bounded comparison
    # cannot evaluate the tail, so the candidate BLOCKS instead of silently
    # passing a near-duplicate pair that sits past the cap. The tail pair
    # QUALIFIES for the ordinary similarity rule (>=12 words, Jaccard >=0.9)
    # — proven by the control arm below inside the comparison window.
    filler = "\n\n".join(
        f"Paragraph {i} carries its own distinct closing words here {i}"
        for i in range(draft_quality._MAX_SIMILARITY_PARAGRAPHS)
    )
    base = (
        "The committee reviewed the charter amendment proposal carefully "
        "before voting today after lengthy debate among members regarding "
        "the future plans"
    )
    tail_a = base + " morning"
    tail_b = base + " afternoon"
    control = _lint(f"{tail_a}.\n\n{tail_b}.")
    assert [
        f
        for f in control.findings
        if f.rule_id == "duplication.paragraph_similarity"
    ], "control: the tail pair must qualify for the similarity rule when compared"
    report = _lint(f"{filler}\n\n{tail_a}\n\n{tail_b}")
    beyond = [
        f
        for f in report.findings
        if f.rule_id == "duplication.similarity_beyond_cap"
    ]
    assert len(beyond) == 1 and beyond[0].severity == "blocker", (
        "a candidate beyond the similarity input cap must block exactly "
        "once (fail closed) rather than silently skipping its unchecked "
        f"tail; got {len(beyond)} beyond-cap findings"
    )
    tail_pair = [
        f
        for f in report.findings
        if f.rule_id
        in ("duplication.paragraph_similarity", "duplication.paragraph_hash")
    ]
    assert tail_pair == [], (
        "the tail near-duplicate must be caught by the FAIL-CLOSED blocker, "
        "not by a truncated comparison (the pair sits beyond the cap)"
    )


def test_no_kwargs_run_deterministic_lint_compatibility():
    text = f"Consider the data. {BOILERPLATE_PHRASE} results vary."
    report = draft_quality.run_deterministic_lint(
        text,
        locked_spans=(),
        rule_version=draft_quality.BOILERPLATE_RULE_VERSION,
        section_id="",
    )
    blockers = _blockers(report)
    assert len(blockers) == 1, (
        "PRESERVING (plan F8): with the new kwargs omitted, the boilerplate "
        "behavior existing callers rely on is unchanged"
    )
    assert blockers[0].rule_id.startswith("blocked_boilerplate.")
    assert (
        text[blockers[0].start : blockers[0].end].lower() == BOILERPLATE_PHRASE
    )
    assert report.rule_version == draft_quality.BOILERPLATE_RULE_VERSION
    rewritten, applied = draft_quality.apply_bounded_rewrites(text, report)
    assert applied == 1
    assert BOILERPLATE_PHRASE not in rewritten.lower()
