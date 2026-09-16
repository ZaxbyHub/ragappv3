"""Guardrail (issue #559): the ``JobLease`` table/fragment allowlists reject
disallowed values BEFORE any SQL is built.

Added per the stage 2-4 plan-critic advisory A5 (review round 2): extending
``_TABLE_SHAPES`` to a third physical table must be a deliberate act — a
caller (or a future migration) passing an arbitrary table name, claim order
or claim-time ``started_at`` fragment fails fast with ``ValueError`` instead
of silently interpolating text into SQL.
"""

import sqlite3
import unittest

from app.services.job_lease import JobLease


class TestJobLeaseAllowlistGuard(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_disallowed_table_rejected(self):
        with self.assertRaises(ValueError):
            JobLease(self.conn, table="users")

    def test_disallowed_claim_order_rejected(self):
        with self.assertRaises(ValueError):
            JobLease(self.conn, claim_order="1; DROP TABLE jobs")

    def test_disallowed_claim_started_at_rejected(self):
        with self.assertRaises(ValueError):
            JobLease(self.conn, claim_started_at="(SELECT 1)")

    def test_allowed_draft_parameterization_constructs(self):
        lease = JobLease(self.conn, table="draft_jobs")
        self.assertEqual(lease._table, "draft_jobs")  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
