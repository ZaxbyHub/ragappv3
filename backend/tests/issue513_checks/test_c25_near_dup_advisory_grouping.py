"""C25 - AC25 (#229 legacy-06): near-duplicate detection groups advisorially
while retaining every distinct revision.

Contract under test (issue #513 AC25):

  With four documents ingested into a vault - A, A' (near-identical to A with a
  small edit), A'' (a revision with small changes), and B (distinct) - plus a
  fifth near-identical document A4 ingested afterwards:

  1. RETENTION (always asserted): every document is retained and retrievable -
     near-duplicate detection is ADVISORY ONLY: no ingest rejection, no
     deletion, no content loss. Exact-hash dedup must not mistake near-dupes
     for duplicates.

  2. GROUPING SURFACE (discriminating): an advisory near-duplicate grouping
     capability must exist - a schema surface (a ``files`` column or a
     dedicated table whose name matches near-dup/similarity grouping) and/or a
     code entry point in the ingestion modules - that marks similar documents
     as an advisory group. Exact-hash dedup alone does not satisfy this.

  3. GROUPING CORRECTNESS (when the surface exists): after ingesting A4
     (near-identical to A), A4 shares the advisory group marker with the
     similar documents, while B stays ungrouped.

Pre-fix expectation: only exact-hash dedup exists - no grouping surface at all
-> FAIL on part 2.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c25_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

_TEXT_A = (
    "Quarterly financial overview. Revenue grew 12 percent year over year driven by "
    "strong demand in the European market. Operating margins improved following the "
    "supply-chain consolidation completed last spring. The board recommends a modest "
    "increase to the dividend and continued investment in the platform roadmap."
)
_TEXT_A_NEAR = _TEXT_A.replace("12 percent", "13 percent")
_TEXT_A_REV = _TEXT_A + " Additionally, the audit committee approved the new risk framework."
_TEXT_B = (
    "Assembly instructions for the model aircraft: glue the wings to the fuselage, "
    "attach the horizontal stabilizer, and verify the center of gravity before the "
    "first flight. Use only the supplied adhesive."
)
_TEXT_A4 = _TEXT_A.replace("12 percent", "12.5 percent")

_DOCS = [
    ("A", _TEXT_A, "hashA"),
    ("A2", _TEXT_A_NEAR, "hashA2"),
    ("A3", _TEXT_A_REV, "hashA3"),
    ("B", _TEXT_B, "hashB"),
]

_GROUP_TABLE_RE = re.compile(r"near.?dup|similar", re.IGNORECASE)
_GROUP_COL_RE = re.compile(r"near.?dup|similar", re.IGNORECASE)
_CODE_RE = re.compile(r"near.?dup|similar|advisory", re.IGNORECASE)
_KNOWN_TABLES = {"similarity_search_evals"}  # unrelated pre-existing surfaces


def _schema_surfaces(db_path: str) -> tuple[str | None, list[str]]:
    """Return (files grouping column name or None, group table names)."""
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        col = next((c for c in cols if _GROUP_COL_RE.search(c)), None)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if _GROUP_TABLE_RE.search(r[0]) and r[0] not in _KNOWN_TABLES
        ]
        return col, tables
    finally:
        conn.close()


def _code_surface() -> str | None:
    import importlib

    names = []
    for module_name in (
        "app.services.document_processor",
        "app.api.documents",
        "app.services.dedup",
        "app.services.near_duplicates",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for attr in dir(module):
            if _CODE_RE.search(attr):
                names.append(f"{module_name}.{attr}")
    return ", ".join(names) if names else None


def _all_retained(db_path: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT file_hash, status, parsed_text FROM files ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < 4:
        return False, f"only {len(rows)} document rows retained (expected >= 4)"
    for file_hash, status, parsed in rows:
        if not parsed:
            return False, f"document {file_hash} lost its parsed text"
        if status == "error":
            return False, f"document {file_hash} ended in error status"
    return True, ""


async def _ingest_near_duplicate(db_path: str, tmp: Path) -> str | None:
    """Ingest A4 (near-identical to A) through the real existing-file path with
    offline fakes. Returns the file row id or None on harness failure."""
    from app.config import settings
    from app.models.database import get_pool
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor

    class _Embed:
        MAX_TEXT_LENGTH = 8192
        embedding_doc_prefix = ""

        async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
            return [[0.5] * 4 for _ in texts], []

    class _VStore:
        async def init_table(self, dim):  # noqa: ANN001, ANN202
            return None

        async def add_chunks(self, records):  # noqa: ANN001, ANN202
            return {}

        async def delete_old_generation_by_file(self, fid, gen):  # noqa: ANN001, ANN202
            return 0

        async def count_by_file(self, fid):  # noqa: ANN001, ANN202
            return 1

    upload = tmp / "a4.txt"
    upload.write_text(_TEXT_A4, encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, ?, 'a4.txt', 'hashA4', 8, 'pending')",
        (str(upload),),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files WHERE file_hash = 'hashA4'").fetchone()[0]
    conn.close()

    processor = DocumentProcessor(pool=get_pool(db_path, max_size=3), embedding_service=_Embed(), vector_store=_VStore())
    chunk = ProcessedChunk(
        text=_TEXT_A4, metadata={"chunk_scale": "default", "raw_text": _TEXT_A4}, chunk_index=0
    )
    with patch.object(settings, "wiki_enabled", False), \
            patch.object(settings, "wiki_compile_on_ingest", False), \
            patch.object(settings, "kms_enabled", False), \
            patch.object(settings, "multi_scale_indexing_enabled", False), \
            patch.object(settings, "contextual_chunking_enabled", False), \
            patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=([chunk], _TEXT_A4, ParsedDocument(atoms=()))),
            ), \
            patch("app.services.document_processor.compute_parent_windows"):
        try:
            await processor.process_existing_file(
                file_id=file_id, file_path=str(upload), vault_id=1
            )
        except Exception:  # noqa: BLE001
            return None
    return file_id


def _grouping_verified(db_path: str, col: str | None, tables: list[str]) -> tuple[bool, str]:
    """Verify A4 shares the advisory group with A while B stays ungrouped."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        def _hash_id(file_hash: str) -> int:
            return conn.execute(
                "SELECT id FROM files WHERE file_hash = ?", (file_hash,)
            ).fetchone()[0]

        a_id, a4_id, b_id = _hash_id("hashA"), _hash_id("hashA4"), _hash_id("hashB")
        if col:
            marks = {
                h: conn.execute(
                    f"SELECT {col} AS m FROM files WHERE id = ?", (_hash_id(h),)
                ).fetchone()["m"]
                for h in ("hashA", "hashA2", "hashA3", "hashA4", "hashB")
            }
            if marks["hashA4"] is None:
                return False, f"grouping column files.{col} never populated for the near-duplicate ingest"
            if marks["hashA"] != marks["hashA4"]:
                return False, (
                    f"near-duplicates A and A4 carry different {col} markers "
                    f"({marks['hashA']!r} vs {marks['hashA4']!r})"
                )
            if marks["hashB"] is not None and marks["hashB"] == marks["hashA"]:
                return False, f"distinct document B shares A's {col} group marker"
            return True, ""
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            except sqlite3.Error:
                continue
            if not rows:
                continue
            cols = rows[0].keys()
            if "file_id" not in cols:
                continue
            group_col = next((c for c in cols if "group" in c), None)
            if group_col is None:
                continue
            groups = {
                r["file_id"]: r[group_col] for r in rows if r[group_col] is not None
            }
            if a4_id not in groups:
                continue
            if groups.get(a_id) != groups.get(a4_id):
                continue
            if groups.get(b_id) == groups.get(a4_id):
                continue
            return True, ""
        return False, "grouping surface present but no advisory group links A with A4 (B distinct)"
    finally:
        conn.close()


async def _scenario() -> str:
    from app.models.database import run_migrations

    tmp = Path(tempfile.mkdtemp(prefix="c25_db_"))
    db_path = str(tmp / "app.db")
    # run_migrations (not bare init_db) so files.parsed_text and its FTS exist.
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    # AMEND (CHECK_WRONG): the seeded documents simulate COMPLETED ingests, so
    # each must also carry the advisory centroid a real ingestion records. The
    # original harness seeded file rows only, so no grouping was observable
    # regardless of implementation. Vectors: A/A2/A3 mutually identical,
    # B distinct — mirroring the texts. Assertions unchanged.
    try:
        from app.services.near_duplicates import record_file_centroid
    except ImportError:  # pre-fix tree: no advisory surface, surface probe fails below
        record_file_centroid = None

    _seed_vectors = {
        "hashA": [0.5, 0.5, 0.5, 0.5],
        "hashA2": [0.5, 0.5, 0.5, 0.5],
        "hashA3": [0.5, 0.5, 0.5, 0.5],
        "hashB": [0.1, 0.9, 0.1, 0.1],
    }
    for name, text, file_hash in _DOCS:
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, "
            "status, parsed_text) VALUES (1, ?, ?, ?, 8, 'indexed', ?)",
            (str(tmp / f"{name}.txt"), f"{name}.txt", file_hash, text),
        )
        (tmp / f"{name}.txt").write_text(text, encoding="utf-8")
        if record_file_centroid is not None:
            record_file_centroid(conn, 1, cur.lastrowid, [_seed_vectors[file_hash]])
    conn.commit()
    conn.close()

    # 1. Retention: nothing rejected/lost.
    retained, reason = _all_retained(db_path)
    if not retained:
        return f"content retention violated: {reason} (#229)"

    # 2. Grouping surface.
    col, tables = _schema_surfaces(db_path)
    code = _code_surface()
    if col is None and not tables and code is None:
        return (
            "no near-duplicate advisory grouping capability exists (no files "
            "column, no dedicated table, no ingestion entry point matching "
            "near-dup/similarity grouping) - exact-hash dedup only (#229)"
        )

    # 3. Grouping correctness via a real near-duplicate ingest.
    a4 = await _ingest_near_duplicate(db_path, tmp)
    if a4 is None:
        return "harness invalid: near-duplicate ingest via process_existing_file failed"
    retained, reason = _all_retained(db_path)
    if not retained:
        return f"content retention violated after near-duplicate ingest: {reason} (#229)"
    ok, reason = _grouping_verified(db_path, col, tables)
    if not ok:
        return reason
    return ""


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C25 CHECK: FAIL: {reason}")
        return 1
    print("C25 CHECK: PASS")
    return 0


def test_c25_near_dup_advisory_grouping() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
