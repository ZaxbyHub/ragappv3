"""Issue #1005 acceptance checks — email metadata snippet bounds (frozen).

``files.email_subject`` / ``email_sender`` are stored uncapped from mail
headers, and the documents ranked-search metadata arm evaluates unguarded
``snippet(files_search_fts, 4/5, ...)`` per matched row — the same
super-linear FTS5 wedge class issue #1002 guarded on the content arm.

Checks (arm's-length, frozen):
* C14 (DISCRIMINATING): ranked search over a file whose ``email_subject``
  is a large (~800K char, environment-calibrated) dense value must
  complete within the 5s budget and return a bounded excerpt. At base the
  unguarded snippet() costs ~13s per matched row on this box, so the
  timing assertion fails for that reason.
* C15 (DISCRIMINATING): (a) the census classifier must flag an UNGUARDED
  ``snippet(files_search_fts, ...)`` call as needing a guard — at base the
  table-name exemption marks it bounded-metadata; and (b) the live
  documents.py metadata arm must sit behind the NUL-proof
  ``length(CAST(<email col> AS BLOB))`` guard shape for BOTH email
  columns — at base neither guard exists.
* C16 (DISCRIMINATING): the #1002 release fragment must record the #1005
  closure (mentions "#1005", "closed", "bounded").

The C14 harness mirrors tests/test_k7_documents_search_large_body.py
(route-function-direct, seeded FTS via the AFTER INSERT triggers); C15
imports the census helpers from tests/test_k7_snippet_guard_census.py.
"""

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from app.api.routes.documents import search_documents
from app.models.database import init_db, run_migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS_SOURCE = REPO_ROOT / "backend" / "app" / "api" / "routes" / "documents.py"
RELEASE_FRAGMENT = (
    REPO_ROOT / "docs" / "releases" / "pending" / "1002-kms-fts-snippet-guard.md"
)

# ~800K chars. Calibrated on this environment's SQLite so the BASE leg
# fails in a bounded ~13s (route measured 13.1s at this size, 0.96s at
# 300K — snippet() cost is super-linear in column size). Kept far below
# the 1M+ regime (~200s) so the red base leg stays fast; post-fix the
# length guard skips snippet() for over-ceiling columns entirely.
_SUBJECT_SIZE = 800_000
_TIME_BUDGET_S = 5.0
_EXCERPT_BOUND = 10_000  # snippet()/fallback excerpts are a few hundred chars

_DENSE_TERMS = [
    "opmed",
    "cdp",
    "order",
    "medications",
    "permission",
    "health",
    "system",
    "row",
    "value",
    "status",
    "active",
]
_DENSE_QUERY = "opmed cdp order"


def _build_dense_subject(size: int) -> str:
    """~size chars of dense-match text, token-index cycled (k7 builder)."""
    approx_tokens = size // 2 + 64
    parts = []
    for i in range(approx_tokens):
        parts.append(_DENSE_TERMS[i % len(_DENSE_TERMS)])
        parts.append("\n" if i % 8 == 7 else " ")
    return "".join(parts)[:size]


async def _allow_policy(user, resource_type, resource_id, action):
    return True


class TestEmailMetadataSnippetBounds:
    async def test_oversized_email_subject_search_stays_in_budget(self):
        """C14: an oversized email_subject must not wedge ranked search."""
        tmp = tempfile.mkdtemp(prefix="f4_email_subject_")
        conn = None
        try:
            db_path = str(Path(tmp) / "app.db")
            init_db(db_path)
            run_migrations(db_path)

            seed = sqlite3.connect(db_path)
            try:
                seed.execute(
                    "INSERT INTO vaults (id, name) VALUES (100, 'F4V')"
                )
                seed.execute(
                    "INSERT INTO files "
                    "(vault_id, file_path, file_name, file_size, file_type, "
                    " status, email_subject) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        100,
                        "/f4/mail.txt",
                        "f4-mail.txt",
                        _SUBJECT_SIZE,
                        "txt",
                        "indexed",
                        _build_dense_subject(_SUBJECT_SIZE),
                    ),
                )
                seed.commit()
            finally:
                seed.close()

            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            user = {"id": 1, "username": "f4-admin", "role": "superadmin"}

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
                "harness: the oversized-subject file must match the "
                f"metadata-arm query, got total={response.total}"
            )
            assert response.results, "harness: expected ranked results"
            assert elapsed < _TIME_BUDGET_S, (
                f"search_documents over a {_SUBJECT_SIZE}-char dense "
                f"email_subject took {elapsed:.2f}s (budget "
                f"{_TIME_BUDGET_S}s) — the metadata-arm snippet() is "
                "unguarded over the uncapped email column"
            )
            for result in response.results:
                assert result.excerpt is None or len(result.excerpt) < _EXCERPT_BOUND, (
                    "the metadata excerpt for an oversized email_subject "
                    f"must be bounded, got {len(result.excerpt or '')} chars"
                )
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_census_flags_unguarded_files_search_fts_snippet(self):
        """C15: the census must flag unguarded files_search_fts snippets,
        and the live metadata arm must guard both email columns."""
        import test_k7_snippet_guard_census as census

        # (a) An UNGUARDED snippet(files_search_fts, ...) call (no
        # length(CAST(... AS BLOB)) guard anywhere near it) must be
        # classified as needing a guard. At base the 40-char table-name
        # exemption marks it bounded-metadata.
        synthetic = (
            "def _meta(self, conn):\n"
            "    return (\n"
            "        \"SELECT snippet(files_search_fts, 4, '<mark>', "
            "'</mark>', '…', 24) AS subject_snippet \"\n"
            "        \"FROM files_search_fts WHERE files_search_fts "
            "MATCH ?\"\n"
            "    )\n"
        )
        sites = census.classify_source(
            "backend/app/api/routes/documents.py", synthetic
        )
        assert sites, "harness: the planted snippet( site must be found"
        verdicts = {site.verdict for site in sites}
        assert verdicts == {"unguarded"}, (
            "the census must flag an unguarded snippet(files_search_fts, "
            "...) as needing a guard once the email columns are bounded — "
            f"the table-name exemption must not cover it; got {verdicts!r} "
            f"for sites {[str(s) for s in sites]}"
        )

        # (b) The LIVE metadata arm in documents.py must sit behind the
        # NUL-proof length(CAST(<email col> AS BLOB)) guard shape for BOTH
        # email columns (the same shape as the content arm). At base no
        # such guard exists in the module.
        source = DOCUMENTS_SOURCE.read_text(encoding="utf-8")
        guarded_columns = {
            match.group(1).split(".")[-1].lower()
            for match in census._BLOB_GUARD_RE.finditer(source)
        }
        missing = {"email_subject", "email_sender"} - guarded_columns
        assert not missing, (
            "the documents.py metadata arm must guard both email metadata "
            "columns with the NUL-proof length(CAST(col AS BLOB)) shape "
            f"(the #1002 content-arm guard); missing guards for {sorted(missing)}"
        )

    def test_release_fragment_records_1005_closure(self):
        """C16: the #1002 fragment must record the #1005 closure."""
        assert RELEASE_FRAGMENT.is_file(), (
            "harness: the 1002 release fragment must exist at "
            f"{RELEASE_FRAGMENT}"
        )
        text = RELEASE_FRAGMENT.read_text(encoding="utf-8")
        missing = [
            marker
            for marker in ("#1005", "closed", "bounded")
            if marker not in text
        ]
        assert not missing, (
            "the 1002-kms-fts-snippet-guard release fragment must record "
            "the #1005 closure (a sentence carrying '#1005', 'closed' and "
            "'bounded' — e.g. \"The email-metadata residual is closed by "
            "#1005: email_subject and email_sender are bounded behind the "
            f"same length guard.\"); missing markers: {missing}"
        )
