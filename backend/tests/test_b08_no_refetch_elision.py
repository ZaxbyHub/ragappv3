"""Issue #697 feedback round (PR #838 review PRR-001): committed coverage for
the candidates query's CASE-NULL no-refetch elision.

The near-duplicate text-fallback candidates query
(``backend/app/services/near_duplicates.py``) must never materialize a
scan-marked file's full ``parsed_text``: the SELECT list routes
stored-fingerprint rows (``nd.dim = 256 AND nd.embedding_model IS NULL``)
through ``NULL`` and the ``!= ''`` predicate routes them through the
``'marked'`` constant, so a converged vault's later ingests neither read nor
transfer marked files' texts (they compare via the stored ~1 KB fingerprint
blob instead).

A pre-publication probe asserted this only from a git-excluded scratch script;
mutation runs proved every other committed test stays green if either CASE
elision is dropped (AC2's fingerprint counter is transfer-insensitive because
the Python stored-blob branch short-circuits before ``parsed_text`` is used).
These tests close that gap WITHOUT hand-copying SQL: they capture the
production candidates statement at runtime via sqlite3's trace callback and
assert the elisions in the statement the app actually executed, then pin the
row-level marking state the elision depends on.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

_B08_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b08_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards.

    Mirrors the sibling ``test_b08_*`` modules: every app import in this
    module is function-local, so this module-scoped fixture has run (and the
    env keys are set) before the first ``app.*`` module is imported. All
    previous values are restored on teardown.
    """
    saved = {key: os.environ.get(key) for key in _B08_ENV}
    os.environ.update(_B08_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _mkdb(tmp_path: Path) -> sqlite3.Connection:
    from app.models.database import run_migrations

    db_path = str(tmp_path / "app.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    conn.commit()
    return conn


def _seed(conn: sqlite3.Connection, name: str, parsed_text: str) -> int:
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size,"
        " status, parsed_text) VALUES (1, ?, ?, ?, 10, 'indexed', ?)",
        (f"/b08elide/{name}", name, f"hash-{name}", parsed_text),
    )
    return int(conn.execute("SELECT id FROM files WHERE file_name = ?", (name,)).fetchone()[0])


def test_marked_rows_elided_in_production_candidates_query(tmp_path) -> None:
    """PRR-001: the runtime-captured candidates query keeps BOTH CASE elisions
    and marked rows resolve to NULL text after the marking wave."""
    from app.config import settings
    from app.services import near_duplicates

    conn = _mkdb(tmp_path)
    try:
        legacy_text = "unrelated legacy corpus number {} with plenty of padding words"
        for i in range(5):
            _seed(conn, f"legacy{i}.txt", legacy_text.format(i))
        x = _seed(conn, "x.txt", "the quick brown fox jumps over the lazy dog")
        y = _seed(conn, "y.txt", "the quick brown fox jumps over the lazy dog again")
        conn.commit()

        near_duplicates.record_file_centroid(
            conn, 1, x, [[1.0] * 8], document_text="the quick brown fox jumps over the lazy dog"
        )

        captured: list[str] = []
        conn.set_trace_callback(captured.append)
        try:
            near_duplicates.record_file_centroid(
                conn, 1, y, [[1.0] * 8],
                document_text="the quick brown fox jumps over the lazy dog again",
            )
        finally:
            conn.set_trace_callback(None)

        candidates_stmts = [
            stmt for stmt in captured
            if "LEFT JOIN document_near_dups" in stmt and "NOT EXISTS" in stmt
        ]
        assert len(candidates_stmts) == 1, (
            f"expected exactly one candidates query during the second ingest, "
            f"got {len(candidates_stmts)} of {len(captured)} captured statements"
        )
        stmt = candidates_stmts[0]
        # The trace callback hands us the EXPANDED statement (parameters
        # inlined), so assert on parameter-independent elision fragments:
        # each marker substring exists only in its CASE branch, so dropping
        # either elision fails here even though every behavioral test above
        # stays green.
        #
        # Select-list elision: marked rows never materialize parsed_text.
        assert "THEN NULL ELSE files.parsed_text END" in stmt
        # Predicate elision: marked rows pass via the constant, unmarked rows
        # are still gated on real text.
        assert "THEN 'marked' ELSE files.parsed_text END" in stmt
        assert "files.parsed_text != ''" not in stmt, (
            "unguarded parsed_text predicate reintroduced: marked rows would "
            "be materialized for the != '' comparison on every ingest"
        )

        # Row-level state the elision depends on: every legacy file is marked
        # (fingerprint row, model NULL) and stays un-grouped by the marking;
        # the ingested file's own row carries the current model identity.
        rows = conn.execute(
            "SELECT file_id, dim, embedding_model, group_id FROM document_near_dups"
        ).fetchall()
        by_file = {row[0]: row for row in rows}
        assert len(rows) == 7  # 5 legacy markers + x's own row + y's own row
        for i in range(5):
            legacy_id = int(
                conn.execute(
                    "SELECT id FROM files WHERE file_name = ?", (f"legacy{i}.txt",)
                ).fetchone()[0]
            )
            assert by_file[legacy_id][1] == 256
            assert by_file[legacy_id][2] is None
            assert by_file[legacy_id][3] is None
        assert by_file[y][2] == str(settings.embedding_model)
    finally:
        conn.close()


def test_first_ingest_marks_unmatched_candidates(tmp_path) -> None:
    """The marking wave writes one fingerprint marker row per scanned
    no-match candidate (previously unasserted: TC-03/TC-29), and the wave's
    own ingest fingerprints each unmarked candidate exactly once."""
    from unittest.mock import patch

    from app.services import near_duplicates

    conn = _mkdb(tmp_path)
    try:
        for i in range(5):
            _seed(conn, f"legacy{i}.txt", f"unrelated corpus {i} padding words")
        x = _seed(conn, "x.txt", "the quick brown fox jumps over the lazy dog")
        conn.commit()

        real_fp = near_duplicates._text_fingerprint
        calls = {"n": 0}

        def counting_fp(text):
            calls["n"] += 1
            return real_fp(text)

        with patch.object(near_duplicates, "_text_fingerprint", counting_fp):
            near_duplicates.record_file_centroid(
                conn, 1, x, [[1.0] * 8],
                document_text="the quick brown fox jumps over the lazy dog",
            )

        # x's own text + each of the 5 unmarked candidates: the wave reads
        # (and fingerprints) full text exactly once per unmarked file.
        assert calls["n"] == 6
        marked = conn.execute(
            "SELECT COUNT(*) FROM document_near_dups "
            "WHERE dim = 256 AND embedding_model IS NULL"
        ).fetchone()[0]
        assert marked == 5
        self_row = conn.execute(
            "SELECT embedding_model FROM document_near_dups WHERE file_id = ?", (x,)
        ).fetchone()
        assert self_row is not None and self_row[0] is not None
    finally:
        conn.close()
