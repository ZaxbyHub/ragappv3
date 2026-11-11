"""Feedback-round pins from the PR #1093 swarm-pr-review (run pr1093-20261107).

GREEN at this head; each test names the mutation or regression it kills.

* ``test_feedback_*_type_guard`` — the isinstance guards added in this
  round: a non-string (dict/list) provider ``finish_reason`` must be treated
  as NON-truncating (the pre-#1076 literal-comparison semantics) instead of
  raising TypeError on unhashable set membership. Killing mutation: drop the
  ``isinstance`` guard at either site (canvas returns an unhandled 500
  outside the LLMError handler; chunk enrichment fails the whole file).
* ``test_feedback_atom_sync_lands_aggregate_on_error_null_row`` — the
  atom-sync guard-identity discriminator (review F-004b): the currency
  predicate must key on ``enrichment_error IS NULL`` (the error COLUMN), not
  on the status column. Killing mutation: replacing the predicate with
  ``AND enrichment_status <> 'error'`` leaves this row frozen at 'error'
  and turns this RED — no other shipped test seeds the ``('error', NULL)``
  pairing.
"""

import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_followup_1073_status_clobber import (  # noqa: E402
    _enrichment_row,
    _make_db,
    _seed_atom_stages,
)
from test_followup_1073_truncation_synonyms import (  # noqa: E402
    _canvas_app_with_client,
    _FakeClient,
    _post_edit_range,
)

from app.models.database import SQLiteConnectionPool  # noqa: E402
from app.services import enrichment_state as est  # noqa: E402
from app.services.background_tasks import (  # noqa: E402
    AtomEnrichmentTaskItem,
    BackgroundProcessor,
)
from app.services.chunk_enrichment import ChunkEnrichmentService  # noqa: E402


class _DictMetricsClient:
    """Client whose metrics sink reports a non-string finish_reason."""

    def __init__(self, finish_reason):
        self._finish_reason = finish_reason

    async def chat_completion(self, messages, **kwargs):
        sink = kwargs.get("metrics_sink")
        if sink is not None:
            sink["finish_reason"] = self._finish_reason
        return '{"summary": "s", "questions": ["q"], "entities": ["e"]}'


class TestNonStringFinishReasonTypeGuards:
    """F-002: unhashable provider values must not raise at either guard."""

    def test_chunk_enrichment_dict_finish_reason_stores_enrichment(self):
        """A dict finish_reason is non-truncating: the reply parses and stores."""
        service = ChunkEnrichmentService(_DictMetricsClient({"reason": "length"}))
        enrichment = asyncio.run(
            service._generate_enrichment("c1", "some text", "T", "S")
        )
        assert not enrichment.error, (
            "a non-string finish_reason must not fail the enrichment "
            f"(base semantics), got error={enrichment.error!r}"
        )
        assert enrichment.summary == "s"

    def test_canvas_edit_range_dict_finish_reason_applies_edit(self):
        """A dict finish_reason must not raise: the edit-range edit applies."""
        client, store = _canvas_app_with_client({"reason": "length"})
        with patch("app.api.routes.canvas.get_canvas_store", lambda: store):
            response = _post_edit_range(client)
        assert response.status_code == 200, (
            "a non-string finish_reason must not turn into an unhandled "
            f"error (base applied the edit), got HTTP {response.status_code}"
        )


class TestAtomSyncGuardIdentity:
    """F-004b: the guard keys on the error COLUMN, not the status column."""

    def test_feedback_atom_sync_lands_aggregate_on_error_null_row(self):
        """An ('error', NULL) row still receives a later atom aggregate.

        The ('error', NULL) pairing is the shared CASE's own all-FAILED_
        PERMANENT vocabulary — the atom sync's currency guard (the error
        COLUMN) must therefore let a later sync (e.g. a new generation's
        stages) advance the row. A status-keyed guard substitute
        (``AND enrichment_status <> 'error'``) freezes the row at 'error'
        and turns this RED (review F-004b: no other shipped test seeds this
        pairing).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fb-guard.db")
            from app.models.database import init_db

            init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("INSERT INTO vaults (name) VALUES ('v')")
                vault_id = conn.execute(
                    "SELECT id FROM vaults LIMIT 1"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO files (file_name, file_path, file_hash, "
                    "file_size, vault_id, status, enrichment_status, "
                    "enrichment_error, enrichment_updated_at) "
                    "VALUES ('a.png', '/x/a.png', 'hash1', 0, ?, 'indexed', "
                    "'error', NULL, '2026-01-01T00:00:00+00:00')",
                    (vault_id,),
                )
                file_id = conn.execute(
                    "SELECT id FROM files LIMIT 1"
                ).fetchone()[0]
                conn.commit()
            finally:
                conn.close()
            pool = SQLiteConnectionPool(db_path)
            try:
                counts = _seed_atom_stages(
                    pool, file_id, [(0, est.SUCCEEDED), (1, est.SUCCEEDED)]
                )
                assert counts[est.SUCCEEDED] == 2
                stub = types.SimpleNamespace(
                    processor=types.SimpleNamespace(pool=pool)
                )
                item = AtomEnrichmentTaskItem(
                    file_id=file_id,
                    vault_id=vault_id,
                    generation_hash="gen1",
                    file_hash="hash1",
                    document_title="a.png",
                    attempt=0,
                )
                BackgroundProcessor._sync_file_enrichment_status(stub, item)
                row = _enrichment_row(pool, file_id)
                assert row["enrichment_status"] == "complete", (
                    "a later atom aggregate must advance an ('error', NULL) "
                    "row — the currency guard keys on the error COLUMN; a "
                    "status-keyed guard freezes the row at 'error' "
                    f"(got enrichment_status={row['enrichment_status']!r})"
                )
                assert row["enrichment_error"] is None
            finally:
                pool.close_all()
