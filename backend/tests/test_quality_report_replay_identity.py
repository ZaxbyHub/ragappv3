"""Arm's-length checks for quality-report replay identity (issue #800).

Round-3 shape (plan v2 wave 2, F12b/F13b). Legs run under subTest so a
base run reports every leg's outcome individually.

C10 pins NULL-turn replay-query resolution by strict seq adjacency: the
replayed query is the user row at the greatest seq strictly below the
reported assistant row's, with NO other row between them (fail-closed on
NULL seq — no id-adjacency branch). Legs: the original adjacent NULL-turn
pair, the re-asked-identical-text pair (provenance must name the ADJACENT
occurrence's user_message_id/user_seq), and three refusal layouts (gap
with intervening rows, unresolvable NULL seq, no preceding user row) that
must 404/422 without inserting any quality_eval_cases row.

C17 pins the retrieval-time hash snapshot end to end (DISCRIMINATING
against the producer): the REAL enrichment function
(document_retrieval.attach_file_hashes — the plan-F13b batch loader) must
stamp the files-table hash into bare chunk metadata, the REAL
to_source_metadata must surface it as a top-level file_hash, and a
chunk-id/files-hash8 MISMATCH must yield an explicitly missing hash
(misattribution guard). The enriched source is then persisted through the
normal add-message route, the cited file is replaced, and the converted
case's provenance must carry the ORIGINAL hash. At base the enrichment
import fails — that IS the RED.

Harness mirrors test_quality_reports_api.py (unittest + TestClient over a
temporary SQLite database with dependency_overrides for the auth/db/policy/
engine/CSRF dependencies).
"""

import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.main import app
from app.models.database import init_db, run_migrations
from app.security import csrf_protect

ADMIN = {
    "id": 1,
    "username": "admin",
    "role": "admin",
    "is_active": True,
    "must_change_password": 0,
}

# Valid 64-char hex content hash standing in for a full file digest.
C17_ORIGINAL_HASH = (
    "c17a1f4e9b2d6c8f7a3e5b1d9f0c8a6e4b2d7f9c1a5e3b8d6f0c4a2e9b7d5f3c"
)
# Second files-table row whose hash8 prefix deliberately disagrees with
# the chunk-id prefix used in the mismatch leg.
C17_MISMATCH_HASH = "bbbb1111" + "0" * 56


class QualityReportReplayIdentityTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        db_path = str(Path(self._tmpdir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")

        vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('qri', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'QRI', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid

        # --- Legacy/migrated NULL-turn block (seq 1-5, turn_id NULL).
        # Multiple user rows with DISTINCT questions and multiple assistant
        # rows, so resolving the replay query by anything other than strict
        # seq adjacency fails visibly.
        self.question_retention = (
            "What is the backup retention window for the finance vault?"
        )
        self.question_revocation = (
            "How are revoked API tokens handled during an incident?"
        )
        # seq 1: an assistant row NO user row precedes (the session opens
        # with the assistant) — the no-user refusal leg's target.
        self.assistant_first_id = self._insert_message(
            "assistant", "No user row precedes this assistant turn.", 1
        )
        self._insert_message("user", self.question_retention, 2)
        self._insert_message(
            "assistant", "The retention window is 30 days.", 3
        )
        self._insert_message("user", self.question_revocation, 4)
        # seq 5: the original happy leg's target; its strictly-preceding
        # adjacent user row by seq is the seq-4 question.
        self.assistant_reported_id = self._insert_message(
            "assistant", "Revoked tokens are purged within one hour.", 5
        )

        # --- Re-asked-identical-text block (seq 10-13, turn_id NULL): the
        # SAME question text asked twice. The adjacency rule must pick the
        # SECOND occurrence (seq 12, adjacent to the seq-13 assistant), and
        # the case provenance must prove it via user_message_id/user_seq.
        self.question_duplicate = (
            "What is the export control limit for dual-use components?"
        )
        self._insert_message("user", self.question_duplicate, 10)
        self._insert_message(
            "assistant", "The limit is fifteen percent of annual revenue.", 11
        )
        self.user_reasked2_id = self._insert_message(
            "user", self.question_duplicate, 12
        )
        self.user_reasked2_seq = 12
        self.assistant_reasked_id = self._insert_message(
            "assistant", "The limit remains fifteen percent this year.", 13
        )

        # --- GAP block (seq 20-23, turn_id NULL): user at seq 20, then TWO
        # other assistant rows at seq 21-22, then the reported assistant at
        # seq 23. The greatest user seq strictly below 23 is 20, but rows
        # 21-22 lie between them — NOT an adjacent pair, so conversion must
        # refuse rather than guess.
        self._insert_message(
            "user", "Which teams own the migration runbook?", 20
        )
        self._insert_message(
            "assistant", "The platform team owns the runbook.", 21
        )
        self._insert_message(
            "assistant", "The data team co-owns the rollback section.", 22
        )
        self.assistant_gap_id = self._insert_message(
            "assistant", "Ownership is split across two teams.", 23
        )

        # --- Unresolvable-NULL-ordering row: an assistant row with BOTH
        # turn_id and seq NULL (its report therefore carries NULL seq).
        # F12b fails CLOSED on NULL seq — no id-adjacency branch exists —
        # so the pairing is unresolvable and conversion must refuse.
        self.assistant_nullseq_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, status,"
            " created_at) VALUES (?, 'assistant', 'No ordering anchors this"
            " row at all.', 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        ).lastrowid

        # --- Real files-table rows for the C17 revision-hash legs.
        self.c17_file_id = self.conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size) VALUES (?, 'c17/export-policy.txt',"
            " 'export-policy.txt', ?, 10)",
            (vault_id, C17_ORIGINAL_HASH),
        ).lastrowid
        self.c17_mismatch_file_id = self.conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size) VALUES (?, 'c17/rotated.txt', 'rotated.txt',"
            " ?, 10)",
            (vault_id, C17_MISMATCH_HASH),
        ).lastrowid
        # Third files row dedicated to the write-once leg (its own row so
        # the leg never interacts with leg 1's in-test replacement).
        self.c17_writetonce_file_id = self.conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size) VALUES (?, 'c17/pinned.txt', 'pinned.txt',"
            " ?, 10)",
            (vault_id, "cccc2222" + "0" * 56),
        ).lastrowid
        self.conn.commit()

        self.client = TestClient(app, raise_server_exceptions=False)

        async def allow(user, resource_type, resource_id, action):
            return True

        self._allow = allow
        from app.api.routes import quality_reports as _qr

        _qr._release_id_cache.clear()
        self.addCleanup(_qr._release_id_cache.clear)
        self.addCleanup(self.conn.close)
        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_current_active_user] = lambda: ADMIN
        app.dependency_overrides[get_evaluate_policy] = lambda: allow
        app.dependency_overrides[get_rag_engine] = lambda: None
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        for dep in (
            get_db,
            get_current_active_user,
            get_evaluate_policy,
            get_rag_engine,
            csrf_protect,
        ):
            self.addCleanup(app.dependency_overrides.pop, dep, None)

    def _insert_message(self, role, content, seq, turn_id=None):
        return self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, ?, ?, ?, ?, 'done',"
            " CURRENT_TIMESTAMP)",
            (self.session_id, role, content, turn_id, seq),
        ).lastrowid

    def _report(self, message_id, category="incorrect_answer", note="wrong"):
        return {
            "session_id": self.session_id,
            "message_id": message_id,
            "category": category,
            "note": note,
        }

    def _submit(self, message_id):
        response = self.client.post(
            "/api/quality/reports", json=self._report(message_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _convert(self, report_id, expected="expected outcome"):
        return self.client.post(
            "/api/quality/reports/%d/convert" % report_id,
            json={"expected_outcome": expected},
        )

    def _case_count(self, report_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM quality_eval_cases WHERE report_id = ?",
            (report_id,),
        ).fetchone()[0]

    def test_null_turn_convert_uses_exact_original_question(self):
        # Leg 1 — original happy leg: the adjacent NULL-turn pair resolves
        # to the EXACT question of the user row immediately preceding the
        # reported assistant row by seq (seq 4), not the session's first
        # user row and not any paraphrase.
        with self.subTest(leg="adjacent-null-turn-pair"):
            report = self._submit(self.assistant_reported_id)
            response = self._convert(
                report["id"],
                expected="Revoked tokens are purged within one hour.",
            )
            self.assertEqual(response.status_code, 200, response.text)
            case = response.json()
            self.assertEqual(case["query"], self.question_revocation)
            self.assertEqual(self._case_count(report["id"]), 1)

        # Leg 2 — re-asked identical text: same question asked twice; the
        # adjacency rule must resolve to the SECOND occurrence (the user
        # row adjacent to the reported assistant), and the provenance must
        # prove the identity via user_message_id/user_seq.
        with self.subTest(leg="re-asked-identical-text"):
            report = self._submit(self.assistant_reasked_id)
            response = self._convert(report["id"])
            self.assertEqual(response.status_code, 200, response.text)
            case = response.json()
            self.assertEqual(case["query"], self.question_duplicate)
            self.assertEqual(
                case["provenance"]["user_message_id"],
                self.user_reasked2_id,
            )
            self.assertEqual(
                case["provenance"]["user_seq"], self.user_reasked2_seq
            )

        # Leg 3 — GAP layout refused without insert (shape seeded in
        # setUp): user at seq 20, other rows at seq 21-22, reported
        # assistant at seq 23 — the greatest user seq below is not
        # adjacent, so the pairing is a guess the route must not make.
        with self.subTest(leg="gap-layout-refused"):
            report = self._submit(self.assistant_gap_id)
            response = self._convert(report["id"])
            self.assertIn(response.status_code, (404, 422), response.text)
            self.assertEqual(self._case_count(report["id"]), 0)

        # Leg 4 — unresolvable NULL ordering refused without insert: the
        # reported assistant row (and therefore its report) has NULL seq
        # and NULL turn; F12b fails closed on NULL seq (no id-adjacency
        # branch), so no rule can pair it.
        with self.subTest(leg="null-seq-refused"):
            report = self._submit(self.assistant_nullseq_id)
            response = self._convert(report["id"])
            self.assertIn(response.status_code, (404, 422), response.text)
            self.assertEqual(self._case_count(report["id"]), 0)

        # Leg 5 — no preceding user row refused without insert: the session
        # OPENS with an assistant row (seq 1), so its replay query is
        # genuinely unresolvable.
        with self.subTest(leg="no-user-row-refused"):
            report = self._submit(self.assistant_first_id)
            response = self._convert(report["id"])
            self.assertIn(response.status_code, (404, 422), response.text)
            self.assertEqual(self._case_count(report["id"]), 0)

    def test_replaced_document_retains_original_revision_hash(self):
        # Plan F13b pins the retrieval-time enrichment as
        # document_retrieval.attach_file_hashes(chunks, conn): a batch
        # `SELECT id, file_hash FROM files WHERE id IN (...)` off the event
        # loop that stamps each chunk's metadata["_file_hash"] IN PLACE
        # from the files-table snapshot. Misattribution guard: when a
        # chunk id embeds a hash8 prefix that disagrees with the table
        # row's first 8 hex chars, the chunk is left unstamped (explicitly
        # missing, never the current table value). At base the function
        # does not exist — the import failure in each leg below IS the RED
        # (producer-surface absence).

        # Leg 1 — enriched snapshot: bare chunk (metadata WITHOUT
        # _file_hash) -> attach_file_hashes stamps the table value ->
        # to_source_metadata emits it top-level -> persisted through the
        # normal add-message route -> file replaced -> convert reports the
        # ORIGINAL hash.
        with self.subTest(leg="enriched-snapshot"):
            from app.services.document_retrieval import (
                DocumentRetrievalService,
                RAGSource,
                attach_file_hashes,
            )

            row = self.conn.execute(
                "SELECT file_hash FROM files WHERE id = ?",
                (self.c17_file_id,),
            ).fetchone()
            original_hash = row[0]
            self.assertRegex(original_hash, r"^[0-9a-f]{64}$")

            chunk = RAGSource(
                text="The export control policy limits dual-use component"
                " shipments.",
                file_id=str(self.c17_file_id),
                score=0.42,
                metadata={
                    "source_file": "export-policy.txt",
                    "chunk_index": 3,
                },
            )
            attach_file_hashes([chunk], self.conn)
            self.assertEqual(chunk.metadata.get("_file_hash"), original_hash)

            emitted = DocumentRetrievalService().to_source_metadata(
                chunk, source_index=1
            )
            self.assertIn("file_hash", emitted)
            self.assertEqual(emitted["file_hash"], original_hash)

            # Persistence leg — the emitted source dict goes into
            # chat_messages.sources through the normal add-message route
            # (the same POST /chat/sessions/{id}/messages path real
            # clients use), on a turn-tagged pair so convert's turn
            # lookup resolves.
            question = "Which document defines the export control policy?"
            user_resp = self.client.post(
                "/api/chat/sessions/%d/messages" % self.session_id,
                json={
                    "role": "user",
                    "content": question,
                    "turn_id": "turn-c17",
                    "status": "complete",
                },
            )
            self.assertEqual(user_resp.status_code, 200, user_resp.text)
            assistant_resp = self.client.post(
                "/api/chat/sessions/%d/messages" % self.session_id,
                json={
                    "role": "assistant",
                    "content": "The policy is defined in the export control"
                    " memo [S1].",
                    "sources": [emitted],
                    "turn_id": "turn-c17",
                    "status": "complete",
                },
            )
            self.assertEqual(
                assistant_resp.status_code, 200, assistant_resp.text
            )
            message_id = assistant_resp.json()["id"]

            # The cited document is REPLACED after the answer was
            # persisted.
            self.conn.execute(
                "UPDATE files SET file_hash = ? WHERE id = ?",
                ("f" * 64, self.c17_file_id),
            )
            self.conn.commit()

            report = self._submit(message_id)
            response = self._convert(report["id"])
            self.assertEqual(response.status_code, 200, response.text)
            provenance = response.json()["provenance"]
            # Reader-retention assertion: the case carries the ORIGINAL
            # at-answer hash — not the replacement, and never an empty
            # snapshot.
            self.assertEqual(
                provenance["source_file_hashes"], [original_hash]
            )

        # Leg 2 — mismatch guard: the chunk id is PRODUCTION-SHAPED so the
        # real parser _extract_reupload_hash (document_retrieval.py
        # :81-92) genuinely extracts its hash8 segment: the id follows the
        # {file_id}_{hash8}_{scale}_{idx} reupload-safe format and embeds
        # hash8 "aaaaaaaa", while the seeded files-table row's hash starts
        # "bbbb1111" (the row was replaced after indexing). The enrichment
        # must NOT stamp the current table value, and the emitted
        # file_hash must be None/absent — explicitly missing, never the
        # misattributed replacement hash. At base this leg is RED via the
        # same missing producer surface (no emission at all).
        with self.subTest(leg="hash8-mismatch-guard"):
            from app.services.document_retrieval import (
                DocumentRetrievalService,
                RAGSource,
                attach_file_hashes,
            )

            chunk = RAGSource(
                text="Body text of the replaced document.",
                file_id=str(self.c17_mismatch_file_id),
                score=0.3,
                metadata={
                    "source_file": "rotated.txt",
                    "_chunk_id": "%d_aaaaaaaa_default_7"
                    % self.c17_mismatch_file_id,
                },
            )
            attach_file_hashes([chunk], self.conn)
            self.assertIsNone(chunk.metadata.get("_file_hash"))
            emitted = DocumentRetrievalService().to_source_metadata(
                chunk, source_index=1
            )
            self.assertIsNone(emitted.get("file_hash"))

        # Leg 3 — serialization wiring pin (plan F13d): rag_engine's
        # _serialize_source_candidates is the final-source serialization
        # seam serving BOTH the done-payload sources (rag_engine.py :3994)
        # and the candidates payload (:2323) — post-filtering by
        # construction. The enrichment must be wired INTO that seam
        # (attach_file_hashes called on the final chunk list, with the
        # engine's pooled connection, before to_source_metadata), which is
        # what makes normal answer sources acquire file_hash without any
        # caller invoking the helper manually. Pure AST pin; at base the
        # seam has zero references, so the leg is RED.
        with self.subTest(leg="serialization-wiring-pin"):
            engine_path = (
                Path(__file__).resolve().parents[1]
                / "app"
                / "services"
                / "rag_engine.py"
            )
            tree = ast.parse(engine_path.read_text(encoding="utf-8"))
            serialize_fn = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_serialize_source_candidates"
            )
            references = [
                node
                for node in ast.walk(serialize_fn)
                if (
                    isinstance(node, ast.Name)
                    and node.id == "attach_file_hashes"
                    and isinstance(node.ctx, ast.Load)
                )
                or (
                    isinstance(node, ast.Attribute)
                    and node.attr == "attach_file_hashes"
                )
            ]
            self.assertGreaterEqual(
                len(references),
                1,
                "_serialize_source_candidates never references"
                " attach_file_hashes (%d references)" % len(references),
            )

        # Leg 4 — write-once across serializations (plan F13e):
        # _serialize_source_candidates runs for candidates (rag_engine
        # :2320-2324) AND again for the done sources (:2491/:3994).
        # attach_file_hashes must be WRITE-ONCE per chunk — it skips any
        # chunk whose metadata ALREADY CARRIES the "_file_hash" KEY
        # (key-presence check, not value: a None missing-marker is stable
        # too). The first (candidate) serialization stamps the snapshot;
        # a replacement landing between the two calls must NOT let the
        # second (done) serialization overwrite it. The chunk id is
        # LEGACY-shaped ("{file_id}_{idx}", no hash8 segment), so no
        # mismatch guard fires and only write-once semantics protect the
        # snapshot. At base the import fails — RED like the other legs.
        with self.subTest(leg="write-once-across-serializations"):
            from app.services.document_retrieval import (
                DocumentRetrievalService,
                RAGSource,
                attach_file_hashes,
            )

            row = self.conn.execute(
                "SELECT file_hash FROM files WHERE id = ?",
                (self.c17_writetonce_file_id,),
            ).fetchone()
            original_hash = row[0]

            chunk = RAGSource(
                text="Body text of the pinned document.",
                file_id=str(self.c17_writetonce_file_id),
                score=0.35,
                metadata={
                    "source_file": "pinned.txt",
                    "_chunk_id": "%d_3" % self.c17_writetonce_file_id,
                },
            )
            service = DocumentRetrievalService()

            # Candidate-call shape: first serialization stamps the
            # files-table snapshot.
            attach_file_hashes([chunk], self.conn)
            self.assertEqual(chunk.metadata.get("_file_hash"), original_hash)
            first = service.to_source_metadata(chunk, source_index=1)
            self.assertEqual(first.get("file_hash"), original_hash)

            # The replacement lands between the two production
            # serialization calls.
            self.conn.execute(
                "UPDATE files SET file_hash = ? WHERE id = ?",
                ("e" * 64, self.c17_writetonce_file_id),
            )
            self.conn.commit()

            # Done-call shape: the second serialization must preserve the
            # candidate call's snapshot (write-once) — the stamp is
            # unchanged and the re-serialized source still carries the
            # ORIGINAL hash.
            attach_file_hashes([chunk], self.conn)
            self.assertEqual(
                chunk.metadata.get("_file_hash"), original_hash
            )
            second = service.to_source_metadata(chunk, source_index=1)
            self.assertEqual(second.get("file_hash"), original_hash)

        # Final-critic round-1 finding + 4.5 delta-review round-2 finding:
        # the enrichment must actually RUN inside a running event loop AND
        # the leg must KILL the original defect. The earlier executor
        # dispatch (run_in_executor(...).result() inside a live loop) always
        # raised InvalidStateError, silently stamping the permanent None
        # missing-marker in production; with an INSTANT fake pool the
        # executor worker could win the write-once race and false-pass, so
        # the fake pool's connection acquisition blocks briefly: under the
        # defective executor dispatch the main thread's except-branch
        # None-stamp lands first (emitted None -> RED), while the fixed
        # synchronous call is unaffected apart from the deliberate delay.
        # This leg drives the REAL RAGEngine._serialize_source_candidates
        # under a running loop and asserts the emitted source carries the
        # files-table hash (behavioral, not structural, kill-verified).
        with self.subTest(leg="running-loop-behavioral"):
            import asyncio as _asyncio
            import contextlib
            import time as _time
            import types

            from app.services.document_retrieval import (
                DocumentRetrievalService,
            )
            from app.services.rag_engine import RAGEngine

            loop_service = DocumentRetrievalService()
            # Reset the replaced row so the behavioral leg observes the
            # table hash independent of the write-once leg above.
            loop_hash = "d" * 63 + "7"
            self.conn.execute(
                "UPDATE files SET file_hash = ? WHERE id = ?",
                (loop_hash, self.c17_writetonce_file_id),
            )
            self.conn.commit()

            loop_chunk = RAGSource(
                text="Behavioral running-loop body.",
                file_id=str(self.c17_writetonce_file_id),
                score=0.4,
                metadata={
                    "source_file": "pinned.txt",
                    "_chunk_id": "%d_5" % self.c17_writetonce_file_id,
                },
            )

            async def _serialize_under_loop():
                import app.services.rag_engine as rag_engine_mod

                engine = types.SimpleNamespace(
                    document_retrieval=loop_service
                )
                real_get_pool = rag_engine_mod._get_pool

                @contextlib.contextmanager
                def _pool_yielding_test_conn():
                    # Deterministic race control: an acquiring thread waits,
                    # so the DEFECTIVE executor-dispatch main thread (whose
                    # .result() raises immediately) stamps its None
                    # missing-marker BEFORE the worker can stamp the real
                    # hash — reproducing the production loss the critic
                    # observed instead of the instant-pool false-pass.
                    _time.sleep(0.05)
                    yield self.conn

                rag_engine_mod._get_pool = lambda: types.SimpleNamespace(
                    connection=_pool_yielding_test_conn
                )
                try:
                    return RAGEngine._serialize_source_candidates(
                        engine, [loop_chunk]
                    )
                finally:
                    rag_engine_mod._get_pool = real_get_pool

            emitted = _asyncio.run(_serialize_under_loop())
            self.assertEqual(emitted[0].get("file_hash"), loop_hash)


if __name__ == "__main__":
    unittest.main()
