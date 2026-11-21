"""Regression test for issue #791 — Workstream O, PR 1.

Pins the accepted KMS contract: update_entry on a missing id returns
None and leaves no open transaction on the connection.
"""

import unittest

from _wiki_lint_o01_support import make_env


class TestKMSEntryUpdateTransaction(unittest.TestCase):
    """KMSStore.update_entry on a missing id closes its transaction."""

    def setUp(self):
        self.conn = make_env(self)

    def tearDown(self):
        self.conn.close()

    def test_update_entry_not_found_leaves_no_open_transaction(self):
        from app.services.kms_store import KMSStore

        kms = KMSStore(self.conn)
        res = kms.update_entry(99999, 1, title="x")
        assert res is None
        assert self.conn.in_transaction is False
