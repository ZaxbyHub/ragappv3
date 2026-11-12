"""Race checks for ``WikiStore.upsert_lint_findings`` reconciliation
(collated wiki lint lifecycle trace, issues #929 + #932 + #935).

At base the reconciliation read its snapshot of ``wiki_lint_findings``
BEFORE opening its ``BEGIN IMMEDIATE`` write transaction, and both its
status-changing UPDATEs (the rule-3 reopen and the resolve pass) carried no
status predicate — so a concurrent user disposition committed in that
window was silently overwritten. At head the snapshot read lives INSIDE
the transaction (the AC5 fix), so the race checks below verify the
dispositions survive the reconciliation on the fixed shape; the AC6/AC7
status predicates themselves are pinned by the AST-scoped source
assertions in C6/C7 (the race oracle passes via snapshot visibility once
AC5 lands).

Harness (the AC8 race-injection pattern): ``store.transaction`` is
patched with a wrapper whose ``__enter__`` first performs writes + COMMIT
on a SECOND sqlite3 connection, then delegates to ONE captured real
context-manager instance. At BASE that injection lands between the
snapshot read and the BEGIN IMMEDIATE (the bug window); at HEAD it lands
just before the BEGIN, so the in-transaction snapshot already sees the
disposition. The real
context manager is created inside ``__enter__`` — creating it at patch time
would emit the BEGIN before the injected writes land, and creating it twice
fails with "cannot start a transaction within a transaction".

Checks in this file (all DISCRIMINATING — RED at base, GREEN after the
fix; AC5 = snapshot inside the BEGIN IMMEDIATE, AC6 = resolve-pass UPDATE
guarded by ``AND status='open'``, AC7 = reopen UPDATE guarded by
``AND status='auto_resolved'``, AC8 = these regressions fail on the
pre-fix code by construction):

- C5 (test_snapshot_sees_concurrent_open_row): a concurrent dismissal of a
  DETECTED open finding must reach the snapshot — rule 2 suppresses the
  fingerprint, so the upsert returns no row for it. At base the stale
  snapshot still sees the row open (rule 1) and returns it as an open
  finding while the DB row is 'dismissed'.
- C6 (test_resolve_pass_respects_concurrent_dismissal): a concurrently
  dismissed finding must not be force-resolved to 'auto_resolved' by the
  resolve pass of a run that detected something else.
- C7 (test_reopen_respects_concurrent_dismissal): a concurrently dismissed
  auto_resolved finding must not be reopened to 'open' by rule 3.
- C8 (test_race_harness_itself): the AC8 self-check — see that test's
  docstring for the non-tautological oracle.
"""

import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402

_WIKI_STORE_SRC = Path(
    os.path.join(os.path.dirname(__file__), "..", "app", "services", "wiki_store.py")
).resolve()


def _upsert_source_segment():
    """Return the source text of upsert_lint_findings (AST-scoped).

    Plan-critic round 1, finding 4: once the snapshot moves inside BEGIN
    IMMEDIATE (AC5) the race tests pass via snapshot visibility alone, so
    the AC6/AC7 status predicates — demanded verbatim by issue #932 — need
    an independent pin. This extracts exactly the function body so the
    predicate assertions cannot be satisfied by unrelated SQL elsewhere in
    the module.
    """
    source = _WIKI_STORE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "upsert_lint_findings"
        ):
            return ast.get_source_segment(source, node)
    raise AssertionError("upsert_lint_findings not found in wiki_store.py")


def _make_env():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.commit()
    return db_path, conn, WikiStore(conn)


class _InterleavedTransaction:
    """Patch target for ``store.transaction`` (the AC8 injection site).

    ``upsert_lint_findings`` opens its write transaction around the whole
    reconciliation (snapshot included, post-AC5). Wrapping that call means
    ``__enter__`` runs just before the production ``BEGIN IMMEDIATE``: the
    second connection writes and COMMITS first, so the in-transaction
    snapshot at head already observes the disposition (at base, where the
    snapshot preceded the transaction, the injection landed in the bug
    window). The ONE captured real context manager is created inside
    ``__enter__`` — creating it twice fails with "cannot start a
    transaction within a transaction".
    """

    def __init__(self, real_transaction, writer):
        self._real_transaction = real_transaction
        self._writer = writer
        self._cm = None

    def __enter__(self):
        self._writer()
        self._cm = self._real_transaction()
        return self._cm.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._cm.__exit__(exc_type, exc, tb)


class TestUpsertLintReconcileRace(unittest.TestCase):
    """C5/C6/C7/C8: concurrent user dispositions committed between the
    snapshot read and the write transaction must survive reconciliation."""

    def setUp(self):
        self.db_path, self.conn, self.store = _make_env()
        self.conn2 = sqlite3.connect(self.db_path)
        self.conn2.execute("PRAGMA foreign_keys = ON;")

    def tearDown(self):
        self.conn.close()
        self.conn2.close()
        # Temp-hygiene (swarm-pr-review round 2, N-07): the fixture db
        # lives alone in its mkdtemp dir.
        shutil.rmtree(Path(self.db_path).parent, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _spec(self, title):
        return {
            "finding_type": "orphan",
            "severity": "low",
            "title": title,
            "details": "",
            "related_page_ids": [],
            "related_claim_ids": [],
        }

    def _seed_open(self, title):
        self.store.upsert_lint_findings(1, [self._spec(title)], origin="linter")
        return self.conn.execute(
            "SELECT id FROM wiki_lint_findings WHERE title = ?", (title,)
        ).fetchone()["id"]

    def _status(self, finding_id, which="conn"):
        conn = self.conn if which == "conn" else self.conn2
        return conn.execute(
            "SELECT status FROM wiki_lint_findings WHERE id = ?", (finding_id,)
        ).fetchone()[0]

    def _set_status_conn1(self, finding_id, status):
        self.conn.execute(
            "UPDATE wiki_lint_findings SET status = ? WHERE id = ?",
            (status, finding_id),
        )
        self.conn.commit()

    def _install_race(self, writer):
        """Patch store.transaction; return a cleanup that restores it."""
        real = self.store.transaction

        def _factory():
            return _InterleavedTransaction(real, writer)

        self.store.transaction = _factory

        def _cleanup():
            del self.store.transaction  # un-shadow the class method

        self.addCleanup(_cleanup)

    def _dismiss_on_conn2(self, finding_ids, recorder):
        def _writer():
            for fid in finding_ids:
                self.conn2.execute(
                    "UPDATE wiki_lint_findings SET status = 'dismissed',"
                    " updated_at = datetime('now') WHERE id = ?",
                    (fid,),
                )
            self.conn2.commit()
            # Read back on the second connection: proves the injected
            # dispositions were DURABLY committed BEFORE the production
            # BEGIN IMMEDIATE runs (the AC8 harness property).
            for fid in finding_ids:
                recorder[fid] = self._status(fid, which="conn2")

        return _writer

    # -- checks ------------------------------------------------------------

    def test_snapshot_sees_concurrent_open_row(self):
        # check: C5 (AC5, DISCRIMINATING) — RED at base, GREEN after the
        # fix (snapshot moves inside the BEGIN IMMEDIATE).
        #
        # Shape chosen (documented per the check contract): a DETECTED
        # fingerprint whose seeded open row is concurrently DISMISSED in
        # the snapshot→BEGIN window. Post-fix the in-transaction snapshot
        # sees 'dismissed', rule 2 suppresses the fingerprint, and the
        # upsert returns NO row for it. At base the pre-transaction
        # snapshot still sees the row open, rule 1 "keeps" it, and the
        # upsert returns it as an OPEN finding — reporting a dismissed
        # issue as open to the caller.
        fid = self._seed_open("Race snapshot probe")
        recorder = {}
        self._install_race(self._dismiss_on_conn2([fid], recorder))

        result = self.store.upsert_lint_findings(
            1, [self._spec("Race snapshot probe")], origin="linter"
        )
        # Harness guard: the injection fired inside this call — the
        # dismissal was durably committed before the production BEGIN.
        self.assertEqual(recorder.get(fid), "dismissed")
        # Supporting: the DB row itself keeps the user's disposition in
        # both worlds (rule 1 does not touch status).
        self.assertEqual(self._status(fid), "dismissed")
        # Discriminating: the returned run must reflect the dismissal —
        # a suppressed fingerprint contributes nothing. At base this fails
        # with 1 != 0 (the stale-open row is returned).
        self.assertEqual(
            len(result), 0,
            "concurrently-dismissed fingerprint must suppress (rule 2), "
            "not be returned as an open finding",
        )

    def test_resolve_pass_respects_concurrent_dismissal(self):
        # check: C6 (AC6, DISCRIMINATING) — RED at base (row becomes
        # 'auto_resolved'; orchestrator probe verified), GREEN after the
        # fix (resolve UPDATE guarded by AND status='open', or the
        # in-transaction snapshot no longer sees the row open).
        fid = self._seed_open("Race resolve probe")
        recorder = {}
        self._install_race(self._dismiss_on_conn2([fid], recorder))

        # This run detects a DIFFERENT fingerprint — the seeded row goes
        # through the resolve pass, which at base overwrites the
        # concurrently committed dismissal with 'auto_resolved'.
        result = self.store.upsert_lint_findings(
            1, [self._spec("Race resolve other probe")], origin="linter"
        )
        # Harness guard: the injection fired inside this call.
        self.assertEqual(recorder.get(fid), "dismissed")
        self.assertEqual(len(result), 1)
        self.assertEqual(
            self._status(fid), "dismissed",
            "resolve pass must not overwrite a concurrent user dismissal "
            "with 'auto_resolved'",
        )
        # Independent predicate pin (plan-critic round 1, finding 4): after
        # AC5 lands, this scenario never reaches the resolve UPDATE (the
        # in-transaction snapshot filters the dismissed row), so the
        # predicate itself is defense-in-depth that the race oracle above
        # cannot prove — issue #932 AC6 demands it verbatim.
        self.assertIn(
            "AND status = 'open'",
            _upsert_source_segment(),
            "the resolve-pass UPDATE must carry the AC6 status predicate "
            "AND status = 'open' inside upsert_lint_findings",
        )

    def test_reopen_respects_concurrent_dismissal(self):
        # check: C7 (AC7, DISCRIMINATING) — RED at base (row becomes
        # 'open'; orchestrator probe verified), GREEN after the fix
        # (reopen UPDATE guarded by AND status='auto_resolved').
        fid = self._seed_open("Race reopen probe")
        self._set_status_conn1(fid, "auto_resolved")
        recorder = {}
        self._install_race(self._dismiss_on_conn2([fid], recorder))

        result = self.store.upsert_lint_findings(
            1, [self._spec("Race reopen probe")], origin="linter"
        )
        # Harness guard: the injection fired inside this call.
        self.assertEqual(recorder.get(fid), "dismissed")
        self.assertEqual(
            self._status(fid), "dismissed",
            "rule-3 reopen must not overwrite a concurrent user dismissal "
            "with 'open'",
        )
        # Independent predicate pin (plan-critic round 1, finding 4): same
        # rationale as C6's pin — the race oracle passes via snapshot
        # visibility once AC5 lands, so the AC7 predicate is pinned against
        # the upsert function's own source, verbatim per issue #932.
        self.assertIn(
            "AND status = 'auto_resolved'",
            _upsert_source_segment(),
            "the rule-3 reopen UPDATE must carry the AC7 status predicate "
            "AND status = 'auto_resolved' inside upsert_lint_findings",
        )
        # At base the row was returned as reopened-open; post-fix the
        # dismissed fingerprint suppresses (rule 2) and nothing is
        # returned for it.
        self.assertEqual(
            len(result), 0,
            "a concurrently-dismissed fingerprint must not be returned "
            "as an open (reopened) finding",
        )

    def test_race_harness_itself(self):
        # check: C8 (AC8, DISCRIMINATING) — RED at base, GREEN after the
        # fix. This is the harness self-check that makes the AC8 "fail on
        # pre-fix code" clause itself the tested property.
        #
        # Oracle (non-tautological, not a one-to-one duplicate of C6/C7):
        # ONE upsert races against the dismissal of TWO findings — one the
        # resolve pass owns (open, undetected) and one rule 3 owns
        # (auto_resolved, detected). The assertion is the COMPOSITE
        # post-fix outcome no other check asserts: BOTH dispositions
        # survive in one race, each fingerprint still holds exactly ONE
        # row, and that row is the ORIGINAL row (id unchanged) — a fix
        # that "honors" the race by re-inserting rows or by deleting and
        # recreating them would pass C6/C7 but fail here. The recorder
        # pre-assertions additionally pin the harness property itself:
        # the injected dispositions were durably committed BEFORE the
        # production BEGIN ran, so the red-at-base composite proves the
        # unguarded production UPDATEs really executed inside the window.
        # Seed BOTH fingerprints in ONE upsert run: separate seeding runs
        # would let the second run's resolve pass auto-resolve the first
        # finding (each upsert resolves every undetected open linter row),
        # degenerating the resolve leg of this race.
        self.store.upsert_lint_findings(
            1,
            [self._spec("Race harness resolve leg"),
             self._spec("Race harness reopen leg")],
            origin="linter",
        )
        fid1 = self.conn.execute(
            "SELECT id FROM wiki_lint_findings "
            "WHERE title = 'Race harness resolve leg'"
        ).fetchone()["id"]
        fid2 = self.conn.execute(
            "SELECT id FROM wiki_lint_findings "
            "WHERE title = 'Race harness reopen leg'"
        ).fetchone()["id"]
        self._set_status_conn1(fid2, "auto_resolved")
        recorder = {}
        self._install_race(self._dismiss_on_conn2([fid1, fid2], recorder))

        # The race: detect ONLY fid2's fingerprint, so fid1 traverses the
        # resolve pass and fid2 traverses rule 3 in one transaction.
        result = self.store.upsert_lint_findings(
            1, [self._spec("Race harness reopen leg")], origin="linter"
        )
        # Harness proof (holds in both worlds — the AC8 property): the
        # second connection's dispositions landed on disk before the
        # production transaction began (read back post-commit on conn2
        # while the production BEGIN had not yet run).
        self.assertEqual(
            recorder, {fid1: "dismissed", fid2: "dismissed"},
            "injection harness must commit the concurrent dispositions "
            "before the production BEGIN IMMEDIATE",
        )

        # Composite oracle (RED at base: {fid1: 'auto_resolved',
        # fid2: 'open'}).
        observed = {fid: self._status(fid) for fid in (fid1, fid2)}
        self.assertEqual(
            observed, {fid1: "dismissed", fid2: "dismissed"},
            "both concurrently-dismissed findings must keep their user "
            "disposition through one reconciled race",
        )
        # Row identity + per-fingerprint cardinality: exactly one row per
        # fingerprint, and it is the ORIGINAL row (no delete/re-insert,
        # no duplicate growth through the race window).
        for title, fid in (
            ("Race harness resolve leg", fid1),
            ("Race harness reopen leg", fid2),
        ):
            rows = self.conn.execute(
                "SELECT id, status FROM wiki_lint_findings "
                "WHERE finding_type = 'orphan' AND title = ?",
                (title,),
            ).fetchall()
            self.assertEqual(
                len(rows), 1,
                f"race must not grow rows for fingerprint {title!r}",
            )
            self.assertEqual(rows[0]["id"], fid)
        # Nothing the user dismissed mid-flight may surface as open.
        self.assertEqual(len(result), 0)
        open_count = self.conn.execute(
            "SELECT COUNT(*) FROM wiki_lint_findings WHERE status = 'open'"
        ).fetchone()[0]
        self.assertEqual(open_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
