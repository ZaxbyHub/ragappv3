"""Frozen AC3 check for issue #787 (TQ-sweep-B02-01): swallowing a corrupted
compile JSON body must be observable in the logs.

``DraftStore.get_job_json_field`` decodes the large compile JSON bodies and
returns ``{}`` for a value that fails ``json.loads`` — a corrupted
``brief_snapshot_json`` is therefore indistinguishable from an absent one on
every read, and nothing anywhere records that the corruption happened. The
``{}``-for-absent public contract must NOT change; what must change is that
the swallow leaves a trace: at least one WARNING-or-worse record naming both
the job id and the field whose body was discarded.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from draft_room import test_draft_pipeline as tdp  # noqa: E402


class _CollectingHandler(logging.Handler):
    """Append every record that reaches the root logger to a list."""

    def __init__(self, sink):
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


class TestN03JobJsonField(tdp.PipelineTestBase):
    longMessage = False

    def test_corrupted_job_json_field_is_logged(self):
        self.store.set_job_json_field(
            job_id=self.job_id, field="brief_snapshot_json", value={"a": 1}
        )
        # Corrupt the stored body behind the accessor's back.
        self.conn.execute(
            "UPDATE draft_jobs SET brief_snapshot_json = '{not json' "
            "WHERE id = ?",
            (self.job_id,),
        )
        self.conn.commit()

        records = []
        handler = _CollectingHandler(records)
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            decoded = self.store.get_job_json_field(
                job_id=self.job_id, field="brief_snapshot_json"
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        n = sum(
            1
            for record in records
            if record.levelno >= logging.WARNING
            and str(self.job_id) in record.getMessage()
            and "brief_snapshot_json" in record.getMessage()
        )
        self.assertEqual(
            decoded, {}, "the {} public contract for absent values must not change"
        )
        self.assertGreaterEqual(n, 1, f"assert {n} >= 1")
