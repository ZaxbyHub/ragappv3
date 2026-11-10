"""Issue #963 acceptance checks: partial-status files are content-available
(#693 producer semantics) and must stay eligible in the bulk
selection/healing consumers that still hardcode pre-#693 status sets.

Checks (mirrors the mixed-status idiom of test_o03_kms_compile_vault_status.py):

- AC1 (DISCRIMINATING): ``_reindex_embed_all``'s file selection — the
  vault-scoped variant AND the global variant (plus the retry-scope
  narrowing) — selects a ``partial`` file alongside indexed/error while
  excluding deleted/processing. At base the four selection variants
  hardcode ``IN ('indexed', 'error')`` and skip partial.
- AC2a (DISCRIMINATING): the stranded-enrichment startup sweep
  ``_recover_stranded_enrichment_rows`` recovers a ``partial`` file whose
  ``enrichment_status`` is stranded in 'processing' (an ``indexed`` twin is
  recovered as the harness control — the sweep runs; only the partial arm
  is filtered out at base).
- AC2b (DISCRIMINATING): the multimodal backfill selection inside
  ``_resume_pending_atom_enrichment`` (the SQL at background_tasks.py
  ~2714-2729) selects ``partial`` files with eligible atoms. Reachability
  (issue #963's evidence requirement): enrichment + atom fan-out runs
  whenever the processor returns a result (background_tasks.py:5465-5488),
  and a partial file still returns a result — ``embed_batch(fail_fast=False)``
  drops only the failed chunks and the file settles 'partial' with the
  kept ones — so a partial file interrupted mid-enrichment leaves a real
  stranded row the sweep must recover. Only the authorization gate
  (``_should_enqueue_atom_enrichment``) is stubbed; the selection SQL is
  production.
- AC3 (DISCRIMINATING, doc fix per the issue): the
  ``_check_duplicate_in_flight`` docstring states ``_check_duplicate``'s
  live ``('indexed', 'partial')`` predicate, and the
  ``migrate_add_files_processing_progress`` docstring in database.py states
  the live four-state in-flight dedup filter
  ``('pending', 'processing', 'indexed', 'partial')``. Plain source-text
  assertions via Path read (deterministic, Windows-safe).
- AC4 (DISCRIMINATING): ``_check_supersession`` surfaces a PARTIAL newer
  version (supersedes_file_id set on the newer row, newer
  status='partial'); an indexed newer version is the harness control that
  already warns at base.

RED at base for AC1/AC2/AC4 (the selections/docstrings exclude or misstate
partial); GREEN after the fix widens the predicates and corrects the
docstrings.

Harness: temp ``run_migrations`` DBs addressed through
``SQLiteConnectionPool``; the reindex selection is driven through the real
``_reindex_embed_all`` (only ``process_existing_file`` is doubled — the
selection SQL itself is production code); ``_check_supersession`` runs the
production query against a temp DB addressed via ``settings.sqlite_path``
(the module's ``_get_pool`` seam).
"""

import ast
import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor

BACKEND_DIR = Path(__file__).resolve().parents[1]
DOCUMENT_PROCESSOR_SRC = (
    BACKEND_DIR / "app" / "services" / "document_processor.py"
).read_text(encoding="utf-8")
DATABASE_SRC = (BACKEND_DIR / "app" / "models" / "database.py").read_text(
    encoding="utf-8"
)

# The mixed-status census: one row per status the selection sites reason
# about (the real files-table CHECK enum is pending/processing/indexed/
# partial/error/cancelled — 'cancelled' is the non-eligible control beside
# 'processing'). Ids are fixed so assertions read as file ids, not rowids.
MIXED_STATUSES = [
    (1, "indexed"),
    (2, "partial"),
    (3, "error"),
    (4, "cancelled"),
    (5, "processing"),
]


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name="ac963.db"):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    conn = _connect(db_path)
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V963')")
    return db_path, conn


def _insert_mixed_status_files(conn, *, vault_id=1):
    for file_id, status in MIXED_STATUSES:
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, phase, source) VALUES (?, ?, ?, ?, 1, ?, 'queued',"
            " 'upload')",
            (file_id, vault_id, f"uploads/f{file_id}.txt", f"f{file_id}.txt", status),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# AC1 — reindex selection variants
# ---------------------------------------------------------------------------


async def test_reindex_selection_vault_scope_includes_partial(tmp_path):
    """AC1, vault-scoped variants: the selection feeding the per-file
    re-embed loop must treat a content-available 'partial' row as
    reindexable — vault-only AND vault+retry-narrowing (round-3, critic
    R2-8: every arm EXECUTES and is recorded before the single combined
    assertion, so the base log carries per-arm evidence)."""
    db_path, conn = _seed(tmp_path, name="ac963-s1v.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        _insert_mixed_status_files(conn, vault_id=1)
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)

        async def run_arm(job_id, retry_file_ids=None):
            picked = []

            async def recording_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
                picked.append(int(existing_id))
                return None

            processor.processor.process_existing_file = recording_double
            args = (
                (job_id, 1, retry_file_ids)
                if retry_file_ids is not None
                else (job_id, 1)
            )
            status, result, _error = await processor._reindex_embed_all(*args)  # noqa: SLF001
            return status, result, sorted(picked)

        vault_status, vault_result, vault_selected = await run_arm(101)
        retry_status, _retry_result, retry_selected = await run_arm(
            104, [2, 4, 5]
        )

        assert (
            vault_selected == [1, 2, 3]
            and vault_status == "completed"
            and vault_result["processed"] == 3
            and retry_selected == [2]
            and retry_status == "completed"
        ), (
            "the vault-scoped reindex selection must treat the partial row"
            " as reindexable on BOTH arms — vault-only selected"
            f" {vault_selected!r} (status {vault_status!r}, processed"
            f" {vault_result.get('processed')!r}; expected indexed(1),"
            " partial(2), error(3) while skipping cancelled(4)/processing(5))"
            " and vault+retry-narrowing selected"
            f" {retry_selected!r} (status {retry_status!r}; expected [2] —"
            " the narrowing must KEEP the partial row and drop"
            " cancelled(4)/processing(5)) — a content-available partial"
            " file is silently never re-embedded (issue #963 site 1)"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_reindex_selection_global_and_retry_scope_include_partial(tmp_path):
    """AC1, global + retry-scope variants: the global all-vaults selection
    and the retry-scope narrowing (json_each over $.retry_file_ids) must
    both keep partial eligible (round-3, critic R2-8: both arms EXECUTE and
    are recorded before the single combined assertion)."""
    db_path, conn = _seed(tmp_path, name="ac963-s1g.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        _insert_mixed_status_files(conn, vault_id=1)
        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)

        async def run_arm(job_id, retry_file_ids=None):
            picked = []

            async def recording_double(existing_id, file_path, vault_id, **kwargs):  # noqa: ARG001
                picked.append(int(existing_id))
                return None

            processor.processor.process_existing_file = recording_double
            status, _result, _error = await processor._reindex_embed_all(  # noqa: SLF001
                job_id, None,
                **({"retry_file_ids": retry_file_ids}
                   if retry_file_ids is not None else {}),
            )
            return status, sorted(picked)

        global_status, global_selected = await run_arm(102)
        retry_status, retry_selected = await run_arm(103, [1, 2, 4, 5])

        assert (
            global_selected == [1, 2, 3]
            and global_status == "completed"
            and retry_selected == [1, 2]
            and retry_status == "completed"
        ), (
            "the GLOBAL reindex selection must select indexed(1), partial(2)"
            f" and error(3) files, selected={global_selected!r} (status"
            f" {global_status!r}), and the global+retry-scope narrowing must"
            f" keep partial(2) among the retry ids and drop cancelled(4)/"
            f" processing(5), selected={retry_selected!r} (status"
            f" {retry_status!r}) — a content-available partial file is"
            " silently never re-embedded (issue #963 site 1)"
        )
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# AC2 — enrichment sweeps
# ---------------------------------------------------------------------------


async def test_stranded_enrichment_sweep_recovers_partial(tmp_path):
    """AC2 leg a: a partial file with enrichment stranded in 'processing'
    (worker died mid-enrichment) must be recovered to enrichment_status
    'error' by the startup sweep, exactly like its indexed twin — on BOTH
    arms (the cutoff-gated startup arm and the un-gated arm)."""
    db_path, conn = _seed(tmp_path, name="ac963-s2a.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        conn.executemany(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, phase, source, enrichment_status) VALUES (?, 1, ?, ?, 1,"
            " ?, 'queued', 'upload', 'processing')",
            [
                (1, "uploads/idx.txt", "idx.txt", "indexed"),
                (2, "uploads/part.txt", "part.txt", "partial"),
            ],
        )
        # Round-2 amendment (critic F6): a second partial twin with a
        # backdated enrichment_updated_at, so the CUTOFF-GATED arm of the
        # sweep (the startup recovery path) unambiguously qualifies it
        # (timestamped long before any plausible cutoff).
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
            " status, phase, source, enrichment_status,"
            " enrichment_updated_at) VALUES (3, 1, 'uploads/part2.txt',"
            " 'part2.txt', 1, 'partial', 'queued', 'upload', 'processing',"
            " '2020-01-01 00:00:00')"
        )
        conn.commit()

        processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
        # Round-3, critic R2-8: BOTH arms EXECUTE and are recorded before the
        # single combined assertion, so the base log carries per-arm
        # evidence. Cutoff arm first (startup_cutoff set; row 3's backdated
        # timestamp qualifies it — rows 1/2 also qualify via created_at, so
        # the indexed control rides along).
        await processor._recover_stranded_enrichment_rows(  # noqa: SLF001
            startup_cutoff=datetime.now(UTC).isoformat()
        )
        cutoff_rows = {
            int(r["id"]): r["enrichment_status"]
            for r in conn.execute(
                "SELECT id, enrichment_status FROM files WHERE id IN (1, 3)"
            )
        }

        # Un-gated arm: reset row 2 (the cutoff arm may already have
        # recovered it via created_at) so this arm is exercised on a fresh
        # stranded partial row.
        conn.execute(
            "UPDATE files SET enrichment_status='processing' WHERE id = 2"
        )
        conn.commit()
        await processor._recover_stranded_enrichment_rows()  # noqa: SLF001
        ungauged_rows = {
            int(r["id"]): r["enrichment_status"]
            for r in conn.execute(
                "SELECT id, enrichment_status FROM files WHERE id IN (1, 2)"
            )
        }

        assert (
            cutoff_rows.get(1) == "error"
            and cutoff_rows.get(3) == "error"
            and ungauged_rows.get(1) == "error"
            and ungauged_rows.get(2) == "error"
        ), (
            "BOTH arms of the stranded-enrichment sweep must recover partial"
            " rows exactly like their indexed twins — cutoff-gated arm:"
            f" indexed={cutoff_rows.get(1)!r} partial={cutoff_rows.get(3)!r}"
            f" (expected error/error); un-gated arm: indexed="
            f"{ungauged_rows.get(1)!r} partial={ungauged_rows.get(2)!r}"
            " (expected error/error) — the sweep's status='indexed' filter"
            " leaves partial rows stranded in 'processing' forever (issue"
            " #963 site 2)"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_multimodal_backfill_selects_partial(tmp_path, monkeypatch):
    """AC2 leg b: the multimodal backfill (no stage row at all — multimodal
    enabled after ingestion) must select partial files with eligible atoms.

    The authorization gate (``_should_enqueue_atom_enrichment``) is stubbed
    True so the selection is observable on ``atom_enrichment_queue``; the
    selection SQL itself is the production query inside
    ``_resume_pending_atom_enrichment``."""
    db_path, conn = _seed(tmp_path, name="ac963-s2b.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        for file_id, status in ((1, "indexed"), (2, "partial")):
            conn.execute(
                "INSERT INTO files (id, vault_id, file_path, file_name,"
                " file_hash, file_size, status, phase, source,"
                " active_generation_hash) VALUES (?, 1, ?, ?, ?, 1, ?,"
                " 'queued', 'upload', 'gen-a')",
                (file_id, f"uploads/f{file_id}.txt", f"f{file_id}.txt",
                 f"hash{file_id}", status),
            )
            conn.execute(
                "INSERT INTO document_atoms (atom_id, file_id,"
                " generation_hash, ordinal, kind, raw_text) VALUES (?, ?,"
                " 'gen-a', 0, 'image', 'alt text')",
                (f"atom-{file_id}", file_id),
            )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM ingestion_stage_states").fetchone()[0] == 0, (
            "harness: no stage rows — the backfill arm (not the interrupted-"
            "work arm) is under test"
        )

        processor = BackgroundProcessor(
            pool=pool, retry_delay=0.05, multimodal_service=object()
        )
        monkeypatch.setattr(
            processor, "_should_enqueue_atom_enrichment", lambda file_id, vault_id: True  # noqa: ARG001
        )
        await processor._resume_pending_atom_enrichment()  # noqa: SLF001

        enqueued = []
        while not processor.atom_enrichment_queue.empty():
            enqueued.append(int(processor.atom_enrichment_queue.get_nowait().file_id))
        assert 1 in enqueued, (
            "harness control: the indexed twin must be backfilled — if this"
            " fails the sweep never ran and the partial assertion below would"
            " be vacuous"
        )
        assert 2 in enqueued, (
            "a partial file with an eligible image atom must be selected by"
            f" the multimodal backfill, enqueued={sorted(enqueued)!r} — the"
            " f.status='indexed' filter never enriches partial content"
            " (issue #963 site 3)"
        )
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# AC3 — docstring corrections (source-text checks)
# ---------------------------------------------------------------------------


def _def_docstring(source: str, def_name: str) -> str:
    """The ACTUAL docstring of one def, top-level OR class-nested (round-3
    fix, critic R2-4: located via ast.walk so a method inside a ClassDef is
    found; ast.get_docstring on the parsed node so a comment or string
    literal elsewhere in the body can never satisfy a docstring check)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == def_name
        ):
            return ast.get_docstring(node) or ""
    raise AssertionError(f"harness: def {def_name!r} not found")


def test_in_flight_docstring_states_indexed_partial_predicate():
    """AC3 leg 1: the ``_check_duplicate_in_flight`` docstring must state
    ``_check_duplicate``'s ACTUAL predicate — status IN ('indexed', 'partial')
    — instead of the stale status='indexed'-only claim."""
    # Mechanism guard (green at base and after): the live predicate really
    # is ('indexed', 'partial').
    assert re.search(
        r"def _check_duplicate\(.*?AND status IN \('indexed', 'partial'\)",
        DOCUMENT_PROCESSOR_SRC,
        re.S,
    ), "harness: _check_duplicate's live predicate must select ('indexed','partial')"
    docstring = _def_docstring(
        DOCUMENT_PROCESSOR_SRC, "_check_duplicate_in_flight"
    )
    normalized = "".join(docstring.split())
    assert "('indexed','partial')" in normalized, (
        "the _check_duplicate_in_flight docstring must state the live"
        " ('indexed', 'partial') predicate of _check_duplicate (the #693-"
        " widened semantics) instead of claiming status='indexed' only"
        " (issue #963 site 4)"
    )


def test_migration_docstring_states_four_state_in_flight_filter():
    """AC3 leg 2: the in-flight-dedup index migration docstring in database.py
    must state the live four-state filter ('pending','processing','indexed',
    'partial'), not the stale three-state list."""
    docstring = _def_docstring(
        DATABASE_SRC, "migrate_add_files_processing_progress"
    )
    normalized = "".join(docstring.split())
    assert "('pending','processing','indexed','partial')" in normalized, (
        "the migrate_add_files_processing_progress docstring must state the"
        " live in-flight-dedup filter ('pending','processing','indexed',"
        "'partial') — the live filter (document_processor"
        "._check_duplicate_in_flight) includes 'partial' while the docstring"
        " cites only the three pre-#693 states (issue #963 site 4b)"
    )


# ---------------------------------------------------------------------------
# AC4 — supersession currency warning
# ---------------------------------------------------------------------------


async def test_check_supersession_warns_for_partial_newer_version(
    tmp_path, monkeypatch
):
    """AC4: a PARTIAL newer version (supersedes_file_id set) must surface the
    currency warning, exactly like an indexed newer version (the harness
    control that already warns at base)."""
    from app.services import rag_engine as rag_engine_module
    from app.services.document_retrieval import RAGSource
    from app.services.rag_engine import RAGEngine

    db_path, conn = _seed(tmp_path, name="ac963-s5.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
        " status, phase, source) VALUES (1, 1, 'uploads/old.txt', 'old.txt',"
        " 1, 'indexed', 'queued', 'upload')"
    )
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
        " status, phase, source, supersedes_file_id) VALUES (2, 1,"
        " 'uploads/new-partial.txt', 'new-partial.txt', 1, 'partial',"
        " 'queued', 'upload', 1)"
    )
    conn.execute(
        "INSERT INTO files (id, vault_id, file_path, file_name, file_size,"
        " status, phase, source, supersedes_file_id) VALUES (3, 1,"
        " 'uploads/new-indexed.txt', 'new-indexed.txt', 1, 'indexed',"
        " 'queued', 'upload', 1)"
    )
    conn.commit()
    conn.close()

    # The module-level _get_pool() seam (settings.sqlite_path is a derived
    # read-only property, so the pool is redirected at the function seam —
    # the same redirection c03b achieves by stubbing the method wholesale;
    # here the PRODUCTION query runs against the temp DB).
    monkeypatch.setattr(rag_engine_module, "_get_pool", lambda: pool)
    # A bare stand-in for the stored embedding dependency: constructing the
    # real EmbeddingService runs an SSRF DNS check on the configured
    # embedding URL, which does not resolve in the offline test env (the
    # c03b idiom — only the stored reference matters for this query).
    engine = RAGEngine(embedding_service=object())  # type: ignore[arg-type]
    # The column exists in the migrated schema; pin the cache so the PRAGMA
    # probe is not what the check exercises.
    engine._supersedes_column_exists = True  # noqa: SLF001
    try:
        source = RAGSource(text="t", file_id="1", score=1.0, metadata={})

        control = await engine._check_supersession([source])  # noqa: SLF001
        assert control is not None, (
            "harness control: an INDEXED newer version (id=3) must already"
            " surface the currency warning at base — if this fails the query"
            " never ran and the partial assertion below would be vacuous"
        )

        conn2 = _connect(db_path)
        try:
            conn2.execute("DELETE FROM files WHERE id = 3")
            conn2.commit()
        finally:
            conn2.close()

        result = await engine._check_supersession([source])  # noqa: SLF001
        assert result is not None, (
            "a PARTIAL newer version (supersedes_file_id=1, status="
            "'partial') must surface the same currency warning — the"
            " status='indexed' filter on the supersession lookup keeps"
            " partial successors invisible (issue #963 site 5)"
        )
    finally:
        pool.close_all()


# ---------------------------------------------------------------------------
# R6 (round-2 amendment, critic F4) — reindex / chunk-retry ownership
# interleaving on a partial file
# ---------------------------------------------------------------------------

_INTERLEAVE_DOC = """
CREATE TABLE ac963_a (id INTEGER PRIMARY KEY, note TEXT);
CREATE TABLE ac963_b (id INTEGER PRIMARY KEY, tag TEXT);
CREATE TABLE ac963_c (id INTEGER PRIMARY KEY, kind TEXT);
"""
_RETRY_CHUNK_TEXT = "AC963-RETRY-CHUNK body of the one chunk that failed"


class _GatedEmbeddingService:
    """Fake embedding service whose retry-path embed parks on a test Event.

    The ONE embed whose text is the seeded failed-chunk text (the call
    ``retry_failed_chunks`` makes) signals ``retry_entered`` and parks on
    ``gate`` — a deterministic pause of the chunk retry mid-embed, never a
    sleep. When ``doc_gate`` is provided, DOCUMENT-chunk embeds (any other
    non-probe text) park on it until the test opens it — the deterministic
    hold point for the cancel-after-promotion leg (cancel gates A and B
    both sit around that embed).
    """

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(
        self,
        gate_text: str,
        gate: asyncio.Event,
        doc_gate: "asyncio.Event | None" = None,
    ):
        self._gate_text = gate_text
        self._gate = gate
        self._doc_gate = doc_gate
        self.retry_entered = asyncio.Event()

    async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
        embeddings = [[0.1, 0.2, 0.3, 0.4] for _ in texts]
        if self._gate_text in texts:
            self.retry_entered.set()
            await self._gate.wait()
            return embeddings if fail_fast else (embeddings, [])
        if texts and "dimension_probe" in str(texts[0]):
            return embeddings if fail_fast else (embeddings, [])
        if self._doc_gate is not None:
            await self._doc_gate.wait()
        return embeddings if fail_fast else (embeddings, [])


def _build_interleaving_store(tmp_path):
    """Real VectorStore with a CONTRACT-FAITHFUL data double (round-3, critic
    R2-3a): id-keyed upsert semantics (same record id overwrites, new ids
    append), ``delete_old_generation_by_file`` drops ONLY the file's
    OLD-generation records (ids not prefixed ``{file_id}_{new_hash_short}_``)
    and never the just-written generation, ``delete_by_file`` drops all of
    the file's rows, and ``count_by_file`` counts live rows — so the store's
    per-file count stays authoritative across generation swaps."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "ac963-interleave-lance")
    state: dict = {"records": {}}

    def _live(file_id):
        return [
            r
            for r in state["records"].values()
            if str(r.get("file_id")) == str(file_id)
        ]

    async def _add_unlocked(records, *args, **kwargs):  # noqa: ARG001
        for record in records:
            state["records"][str(record.get("id"))] = record
        return {}

    async def _count_by_file(file_id, target=None):  # noqa: ARG001
        return len(_live(file_id))

    async def _get_live_dim():  # noqa: ARG001
        return None

    async def _init_table(embedding_dim, *args, **kwargs):  # noqa: ARG001
        return None

    async def _delete_old_generation(file_id, new_hash_short, *a, **k):  # noqa: ARG001
        keep_prefix = f"{file_id}_{new_hash_short}_"
        old_ids = [
            rid
            for rid, r in state["records"].items()
            if str(r.get("file_id")) == str(file_id)
            and not rid.startswith(keep_prefix)
        ]
        for rid in old_ids:
            del state["records"][rid]
        return len(old_ids)

    async def _delete_by_file(file_id, *args, **kwargs):  # noqa: ARG001
        for rid in [
            rid
            for rid, r in state["records"].items()
            if str(r.get("file_id")) == str(file_id)
        ]:
            del state["records"][rid]
        return 0

    async def _get_chunks_by_uid(uids, *args, **kwargs):  # noqa: ARG001
        return []

    async def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    store._add_chunks_unlocked = _add_unlocked  # noqa: SLF001
    store.count_by_file = _count_by_file  # noqa: SLF001
    store.get_live_embedding_dim = _get_live_dim  # noqa: SLF001
    store.init_table = _init_table  # noqa: SLF001
    store.delete_old_generation_by_file = _delete_old_generation  # noqa: SLF001
    store.delete_by_file = _delete_by_file  # noqa: SLF001
    store.get_chunks_by_uid = _get_chunks_by_uid  # noqa: SLF001
    # The reindex completion path's embedding-identity sidecar writes
    # (record_embedding_metadata / mark_ready) target the store's own
    # metadata DB, not the pool DB — out of scope for this check (and a
    # fresh sidecar without the app's init would fail them), so both are
    # no-op doubles like every other data-plane seam above.
    store.record_embedding_metadata = _noop  # noqa: SLF001
    store.mark_ready = _noop  # noqa: SLF001

    def _live_rows(file_id):
        return _live(file_id)

    return store, _live_rows


async def test_reindex_and_chunk_retry_interleaving_consistent(
    tmp_path, monkeypatch
):
    """R6 (critic F4): while a chunk-scoped retry of a PARTIAL file is parked
    mid-embed, a vault reindex must be able to process the same file and the
    two writers must end consistent.

    Planned contract (the docstring the fix must satisfy): the reindex
    per-file path acquires the same module-level per-file retry lock
    (``document_processor._per_file_retry_lock``) that
    ``retry_failed_chunks`` holds, so reindex and chunk-retry serialize
    in-process; the retry's stale-generation re-read then no-ops it when the
    reindex won, and whichever wins leaves files.status in
    ('indexed','partial'), chunk_count equal to the store's live row count
    for the file, no failed_chunks rows surviving a completed reindex pass,
    and active_generation_hash unchanged (same content re-derived).

    RED at base for the #963 reason: the vault reindex selection skips the
    partial file (processed == 0), so the interleaving cannot even start —
    the reindex-processed assertion fails while the retry is still parked.

    Harness: the REAL ``process_existing_file`` and the REAL
    ``retry_failed_chunks`` (only the embedding service is gated and the
    vector-store data plane stubbed, both documented above); synchronization
    is asyncio Events (the retry's park, the per-file-lock probe, and
    FIRST_COMPLETED completion), never sleeps — the 5s asyncio.wait cap is
    a deadlock backstop only, never the release trigger. The lock seam is
    probed by spying ``document_processor._per_file_retry_lock`` AFTER the
    retry holds it, so the spy sees only the reindex's planned acquisition.
    The seeded active_generation_hash is the REAL computed generation for
    the document content, so a completed reindex of the same content must
    leave it unchanged.
    """
    from app.services import document_processor as dp_module
    from app.services.document_processor import (
        _compile_generation,
        compute_file_hash,
    )

    db_path, conn = _seed(tmp_path, name="ac963-r6.db")
    pool = SQLiteConnectionPool(db_path, max_size=6)
    gate = asyncio.Event()
    try:
        doc_path = tmp_path / "ac963-interleave-doc.sql"
        doc_path.write_text(_INTERLEAVE_DOC, encoding="utf-8")
        file_hash = compute_file_hash(str(doc_path))
        generation_hash, _fingerprint = _compile_generation(file_hash)
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash,"
            " file_size, status, phase, source, chunk_count, chunks_failed,"
            " active_generation_hash) VALUES (1, 1, ?, ?, ?, 1, 'partial',"
            " 'partial', 'upload', 2, 1, ?)",
            (
                str(doc_path),
                "ac963-interleave-doc.sql",
                file_hash,
                generation_hash,
            ),
        )
        conn.execute(
            "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text,"
            " chunk_metadata) VALUES (1, 2, ?, ?)",
            (
                _RETRY_CHUNK_TEXT,
                json.dumps(
                    {
                        "chunk_index": 2,
                        "total_chunks": 3,
                        "chunk_scale": "default",
                        "generation_hash": generation_hash,
                    }
                ),
            ),
        )
        conn.commit()

        service = _GatedEmbeddingService(_RETRY_CHUNK_TEXT, gate)
        store, live_rows = _build_interleaving_store(tmp_path)
        processor = BackgroundProcessor(
            pool=pool,
            retry_delay=0.05,
            vector_store=store,
            embedding_service=service,
        )

        retry_task = asyncio.create_task(
            processor.processor.retry_failed_chunks(1)  # noqa: SLF001
        )
        await service.retry_entered.wait()  # the retry is parked mid-embed

        # Spy the per-file lock AFTER the retry holds it: the next
        # acquisition attempt for file 1 can only be the reindex's (the
        # planned contract's serialization point).
        lock_probe = asyncio.Event()
        original_lock = dp_module._per_file_retry_lock  # noqa: SLF001

        def _spy_lock(file_id):
            if int(file_id) == 1:
                lock_probe.set()
            return original_lock(file_id)

        monkeypatch.setattr(dp_module, "_per_file_retry_lock", _spy_lock)

        reindex_task = asyncio.create_task(processor._reindex_embed_all(1, 1))  # noqa: SLF001
        # Event-driven handoff: at BASE the reindex needs no gate and
        # completes with the file skipped (FIRST_COMPLETED fires on the
        # reindex task itself — its selection ran while the retry was
        # parked); POST-FIX the reindex reaches the file, attempts the
        # per-file lock (lock_probe fires), and waits for the parked retry.
        await asyncio.wait(
            {reindex_task, asyncio.ensure_future(lock_probe.wait())},
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Round-3, critic R2-3b: the wait is INSPECTED, not ignored — record
        # whether the reindex actually reached the per-file lock and is
        # still waiting there BEFORE the gate opens.
        reached_lock = lock_probe.is_set()
        reindex_pending_at_release = not reindex_task.done()
        gate.set()
        _status, result, _exc = await reindex_task
        await retry_task

        assert (
            reached_lock
            and reindex_pending_at_release
            and result.get("processed") == 1
        ), (
            "a vault reindex running while a partial file's chunk retry is"
            " parked mid-embed must reach the file's per-file retry lock"
            f" (reached_lock={reached_lock!r}), be waiting there when the"
            " gate opens"
            f" (reindex_pending_at_release={reindex_pending_at_release!r}),"
            " and process the partial file"
            f" (processed={result.get('processed')!r}) — at base the"
            " selection skips status='partial', so the interleaving cannot"
            " even start (issue #963 site 1, round-2 critic F4 / round-3"
            " R2-3)"
        )

        frow = conn.execute(
            "SELECT status, chunk_count, active_generation_hash FROM files"
            " WHERE id = 1"
        ).fetchone()
        live_count = len(live_rows(1))
        ledger_rows = conn.execute(
            "SELECT COUNT(*) FROM failed_chunks WHERE file_id = 1"
        ).fetchone()[0]
        assert frow["status"] in ("indexed", "partial"), (
            "after the interleaved pass the file must remain in a"
            f" selectable state, got {frow['status']!r}"
        )
        assert int(frow["chunk_count"] or 0) == live_count, (
            "files.chunk_count must agree with the vector store's live rows"
            f" for the file (chunk_count={frow['chunk_count']!r},"
            f" store rows={live_count})"
        )
        assert ledger_rows == 0, (
            "no failed_chunks ledger row may survive a completed reindex"
            f" pass ({ledger_rows} row(s) survived)"
        )
        assert frow["active_generation_hash"] == generation_hash, (
            "reindexing the same content must leave active_generation_hash"
            " unchanged"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_reindex_cancel_after_retry_promotion_restores_current_status(
    tmp_path, monkeypatch
):
    """R2-7/R3-2 (rounds 3-4): a chunk retry that promotes a partial file to
    'indexed' WHILE the reindex is waiting on the per-file lock must change
    what the reindex's cancellation restoration sees — a cancel accepted
    after the promotion must restore the status AT LOCK ACQUISITION
    ('indexed'), never the stale pre-promotion 'partial' and never a
    stranded 'processing'.

    SYNCHRONIZED CONTRACT (round-4, critic R3-2 — no scheduling
    sensitivity): park the chunk retry mid-embed on the partial file; start
    the vault reindex (post-fix it selects the file and waits on the
    per-file lock); release the retry gate so the retry COMPLETES and
    promotes the row to 'indexed'; wait until the reindex's AT-ACQUISITION
    ``clear_cancel`` has RUN for our file (spied with an Event — the cancel
    request is only accepted AFTER it, so the stale-cancel clearing can
    never race the request); the reindex's document embed then parks on
    ``doc_gate`` (cancel gates A and B both sit around that embed — either
    observes the request); accept the cancel and open the doc gate; the
    cancel gate demonstrably fires (spied) and the per-file handler restores
    the prior status — the post-promotion 'indexed'. The real cancel
    outcome is asserted: processed == 0 for this one-file job (the
    cancellation arm, not the processed arm).

    RED at base for the #963 reason: the selection skips the partial file
    (the reindex never reaches the lock, so the at-acquisition clear_cancel
    spy never fires either), reported by the combined assertion with every
    recorded observable.
    """
    from app.services import document_processor as dp_module
    from app.services.document_processor import (
        _compile_generation,
        compute_file_hash,
    )

    db_path, conn = _seed(tmp_path, name="ac963-r27.db")
    pool = SQLiteConnectionPool(db_path, max_size=6)
    gate = asyncio.Event()
    doc_gate = asyncio.Event()
    try:
        doc_path = tmp_path / "ac963-r27-doc.sql"
        doc_path.write_text(_INTERLEAVE_DOC, encoding="utf-8")
        file_hash = compute_file_hash(str(doc_path))
        generation_hash, _fingerprint = _compile_generation(file_hash)
        conn.execute(
            "INSERT INTO files (id, vault_id, file_path, file_name, file_hash,"
            " file_size, status, phase, source, chunk_count, chunks_failed,"
            " active_generation_hash) VALUES (1, 1, ?, ?, ?, 1, 'partial',"
            " 'partial', 'upload', 2, 1, ?)",
            (
                str(doc_path),
                "ac963-r27-doc.sql",
                file_hash,
                generation_hash,
            ),
        )
        conn.execute(
            "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text,"
            " chunk_metadata) VALUES (1, 2, ?, ?)",
            (
                _RETRY_CHUNK_TEXT,
                json.dumps(
                    {
                        "chunk_index": 2,
                        "total_chunks": 3,
                        "chunk_scale": "default",
                        "generation_hash": generation_hash,
                    }
                ),
            ),
        )
        conn.commit()

        service = _GatedEmbeddingService(
            _RETRY_CHUNK_TEXT, gate, doc_gate=doc_gate
        )
        store, live_rows = _build_interleaving_store(tmp_path)
        processor = BackgroundProcessor(
            pool=pool,
            retry_delay=0.05,
            vector_store=store,
            embedding_service=service,
        )

        retry_task = asyncio.create_task(
            processor.processor.retry_failed_chunks(1)  # noqa: SLF001
        )
        await service.retry_entered.wait()  # the retry is parked mid-embed

        lock_probe = asyncio.Event()
        original_lock = dp_module._per_file_retry_lock  # noqa: SLF001

        def _spy_lock(file_id):
            if int(file_id) == 1:
                lock_probe.set()
            return original_lock(file_id)

        monkeypatch.setattr(dp_module, "_per_file_retry_lock", _spy_lock)

        reindex_task = asyncio.create_task(processor._reindex_embed_all(1, 1))  # noqa: SLF001
        await asyncio.wait(
            {reindex_task, asyncio.ensure_future(lock_probe.wait())},
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        reached_lock = lock_probe.is_set()
        pending_at_release = not reindex_task.done()

        # Let the retry COMPLETE (promoting the partial row to 'indexed'
        # and clearing its ledger rows).
        gate.set()
        await retry_task
        promoted = conn.execute(
            "SELECT status FROM files WHERE id = 1"
        ).fetchone()["status"]
        assert promoted == "indexed", (
            "harness: a fully-succeeded chunk retry must promote the partial"
            f" row to indexed, got {promoted!r}"
        )

        # R3-2(a): synchronize the cancel request to land AFTER the
        # reindex's at-acquisition clear_cancel — spy it (installed only
        # now, so the retry's own activity can never trip it) and wait for
        # it to RUN for our file before requesting the cancel. At base the
        # reindex never reaches the file, so this wait resolves via the
        # already-completed reindex task instead (bounded, never a sleep).
        clear_cancel_seen = asyncio.Event()
        original_clear = processor.processor.clear_cancel  # noqa: SLF001

        def _spy_clear_cancel(file_id):
            if int(file_id) == 1:
                clear_cancel_seen.set()
            return original_clear(file_id)

        processor.processor.clear_cancel = _spy_clear_cancel  # noqa: SLF001

        cancel_gate_observations: list[bool] = []
        original_raise = processor.processor._raise_if_cancelled  # noqa: SLF001

        def _spy_raise_cancelled(file_id):
            if file_id is not None and int(file_id) == 1:
                cancel_gate_observations.append(
                    1 in processor.processor._cancel_requested  # noqa: SLF001
                )
            return original_raise(file_id)

        processor.processor._raise_if_cancelled = _spy_raise_cancelled  # noqa: SLF001

        await asyncio.wait(
            {
                asyncio.ensure_future(clear_cancel_seen.wait()),
                reindex_task,
            },
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        clear_cancel_ran = clear_cancel_seen.is_set()

        # R3-2(b): the cancel request — accepted only after the
        # at-acquisition clear_cancel ran (or, at base, after the reindex
        # already finished without the file) — then the doc embed is
        # released so a cancel gate (A before the embed, B before the
        # vector write) observes it.
        processor.processor.request_cancel(1)  # noqa: SLF001
        doc_gate.set()

        _status, result, _exc = await reindex_task
        cancel_gate_fired_with_cancel = bool(cancel_gate_observations)
        processed_count = result.get("processed")
        failed_count = result.get("failed")

        assert (
            reached_lock
            and pending_at_release
            and clear_cancel_ran
            and cancel_gate_fired_with_cancel
            and processed_count == 0
            and failed_count == 0
        ), (
            "the reindex must reach the promoted file's per-file lock"
            f" (reached_lock={reached_lock!r}), wait there across the"
            f" retry's promotion (pending_at_release={pending_at_release!r}),"
            " observe the at-acquisition clear_cancel"
            f" (clear_cancel_ran={clear_cancel_ran!r}), have a cancel gate"
            " demonstrably observe the accepted cancel"
            f" (cancel_gate_fired_with_cancel="
            f"{cancel_gate_fired_with_cancel!r}), and settle the ONE-file"
            f" job through the cancellation arm (processed="
            f"{processed_count!r}, failed={failed_count!r}; both must be 0)"
            " — at base the selection skips status='partial', the"
            " interleaving cannot even start, and no cancel gate ever runs"
            " (issue #963 site 1, rounds 3-4 R2-7/R3-2)"
        )

        frow = conn.execute(
            "SELECT status, chunk_count, active_generation_hash FROM files"
            " WHERE id = 1"
        ).fetchone()
        ledger_rows = conn.execute(
            "SELECT COUNT(*) FROM failed_chunks WHERE file_id = 1"
        ).fetchone()[0]
        assert frow["status"] == "indexed", (
            "a cancel accepted AFTER the retry's promotion must restore the"
            " status at lock acquisition — 'indexed' — never the stale"
            " pre-promotion 'partial' and never a stranded 'processing',"
            f" got {frow['status']!r}"
        )
        assert int(frow["chunk_count"] or 0) == len(live_rows(1)), (
            "files.chunk_count must agree with the store's live rows"
            f" (chunk_count={frow['chunk_count']!r},"
            f" store rows={len(live_rows(1))})"
        )
        assert ledger_rows == 0, (
            "the ledger must stay empty after the retry's completion"
            f" ({ledger_rows} row(s) present)"
        )
        assert frow["active_generation_hash"] == generation_hash, (
            "same-content reprocessing must leave active_generation_hash"
            " unchanged"
        )
    finally:
        pool.close_all()
        conn.close()
