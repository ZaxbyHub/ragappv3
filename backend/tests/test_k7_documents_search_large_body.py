"""K7 acceptance checks — documents ranked search over an oversized dense body.

Trace: kms-fts-snippet-large-body-wedge. The twin call site of the KMS
retrieval wedge lives in the ranked document search handler
(app/api/routes/documents.py search_documents): snippet(files_content_fts, 0)
over the unbounded files.parsed_text, with the full parsed_text also carried
through the CTE.

Check in this file:
- AC4 (DISCRIMINATING): search_documents over a vault holding one file whose
  parsed_text is ~1.2M dense chars completes within the 5s budget and returns
  results; AND a small normal file in the same vault still yields a
  <mark>-containing excerpt. Both asserts in ONE test.

Invocation follows the repo's own route-handler-direct convention
(tests/test_b08_off_loop_scans.py::test_list_documents_resolves_enrichment_off_loop):
the async handler is awaited with its Depends arguments constructed manually
(conn from a check_same_thread=False connection with Row factory — the
handler dispatches conn.execute via asyncio.to_thread — plus a superadmin
user dict and an allow-all evaluate callable). No TestClient.

The dense body builder cycles the term list by TOKEN INDEX so char-slicing
cannot silently drop AND-terms and empty the MATCH result.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes.documents import search_documents  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402

# ---------------------------------------------------------------------------
# Dense-body fixture (same shape as the KMS-side file)
# ---------------------------------------------------------------------------

_DENSE_TERMS = [
    "opmed",
    "cdp",
    "order",
    "medications",
    "permission",
    "health",
    "system",
    "t6",
    "row",
    "value",
    "status",
    "active",
]

_DENSE_QUERY = "opmed cdp order"

_BODY_SIZE = 1_200_000

_TIME_BUDGET_S = 5.0

# Term that occurs ONLY in the small file's body — proves the normal-path
# <mark> excerpt behavior is preserved in the same vault as the oversized row.
_SMALL_FILE_TERM = "nimbus"
_SMALL_FILE_TEXT = (
    "The nimbus staging cluster is ready for launch and fully validated "
    "by the operations team."
)


def build_dense_body(size: int) -> str:
    """Return ~size chars of dense-match text, token-index cycled."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


async def _allow_policy(user, resource_type, resource_id, action):
    return True


class TestK7AC4DocumentsSearchTiming:
    """AC4 (DISCRIMINATING): ranked search latency + preserved mark excerpt."""

    async def test_k7_ac4_ranked_search_dense_body_within_budget_and_small_file_mark(
        self,
    ):
        tmp = tempfile.mkdtemp(prefix="k7_documents_search_")
        db_path = str(Path(tmp) / "app.db")
        conn = None
        try:
            init_db(db_path)
            run_migrations(db_path)

            seed = sqlite3.connect(db_path)
            try:
                seed.execute("INSERT INTO vaults (id, name) VALUES (100, 'K7V')")
                # Oversized file: ~1.2M dense chars of parsed_text. The
                # files_content_fts / files_search_fts AFTER INSERT triggers
                # (app/models/database.py) feed both FTS indexes.
                seed.execute(
                    "INSERT INTO files "
                    "(vault_id, file_path, file_name, file_size, file_type, "
                    " status, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "/k7/big.txt",
                        "k7-big.txt",
                        _BODY_SIZE,
                        "txt",
                        "indexed",
                        build_dense_body(_BODY_SIZE),
                    ),
                )
                seed.execute(
                    "INSERT INTO files "
                    "(vault_id, file_path, file_name, file_size, file_type, "
                    " status, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "/k7/small.txt",
                        "k7-small.txt",
                        len(_SMALL_FILE_TEXT),
                        "txt",
                        "indexed",
                        _SMALL_FILE_TEXT,
                    ),
                )
                seed.commit()
            finally:
                seed.close()

            # search_documents dispatches conn.execute via asyncio.to_thread,
            # so the connection must be check_same_thread=False (repo
            # convention: b08 AC5) and Row-typed for the row["..."] reads.
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            user = {"id": 1, "username": "k7-admin", "role": "superadmin"}

            # --- Timing half: ranked search over the vault with the 1.2M-char
            # dense parsed_text must complete within the shared budget.
            start = time.perf_counter()
            response = await search_documents(
                q=_DENSE_QUERY,
                vault_id=100,
                limit=20,
                offset=0,
                conn=conn,
                user=user,
                evaluate=_allow_policy,
            )
            elapsed = time.perf_counter() - start

            assert response.total >= 1, (
                f"expected the dense-body file in results, got total="
                f"{response.total}"
            )
            assert response.results, "expected non-empty ranked results"
            assert elapsed < _TIME_BUDGET_S, (
                f"search_documents over a {_BODY_SIZE}-char dense parsed_text "
                f"took {elapsed:.2f}s (budget {_TIME_BUDGET_S}s)"
            )

            # --- Preserving half: a small normal file in the SAME vault still
            # yields a match-centered <mark>-containing excerpt.
            small_response = await search_documents(
                q=_SMALL_FILE_TERM,
                vault_id=100,
                limit=20,
                offset=0,
                conn=conn,
                user=user,
                evaluate=_allow_policy,
            )
            marked = [
                r.file_name
                for r in small_response.results
                if "<mark>" in r.excerpt
            ]
            assert marked, (
                "no <mark>-containing excerpt for the small file; got "
                f"{[(r.file_name, r.excerpt) for r in small_response.results]}"
            )
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)
