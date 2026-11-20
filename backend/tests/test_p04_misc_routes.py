"""Arm's-length checks for quality-report route integrity (issue #800).

Round-3 shape (plan v2 wave 2, F8b). Legs run under subTest so a base run
reports every leg's outcome individually.

C11 pins submit idempotency: a duplicate POST of the SAME
(session_id, message_id, category) identity must not create a second
quality_reports row, an independent category on the same message must
still be recorded, and a request that loses a unique-index race to a
concurrent winner (dedup pre-select misses, INSERT raises
sqlite3.IntegrityError, recovery re-select after rollback returns the
pre-seeded winner) must return 200 with the EXISTING row id — never 500,
never a new row.

C12 pins convert idempotency: converting the same report twice must not
create a second quality_eval_cases row, and a racing convert on its own
INDEPENDENT report identity must recover the pre-seeded winner case the
same way.

C13 pins config_ref recovery: a transient release-id lookup failure must
not poison the process cache — the next call must pick up the recovered
value.

C14 pins the submit route's event-loop hygiene at the source level: the
inline blocking ``_config_ref()`` call inside submit_quality_report must be
offloaded (e.g. ``await asyncio.to_thread(_config_ref)``), never called
directly on the event loop.

The route checks mirror the test_quality_reports_api.py harness (unittest +
TestClient over a temporary SQLite database with dependency_overrides for
the auth/db/policy/CSRF dependencies).
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

from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
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


class _ShadowCursor:
    """Cursor proxy whose result set is hidden (fetchone/fetchall empty).

    Simulates the dedup pre-select MISSING an identity a concurrent writer
    already committed: the row EXISTS in the table, but this reader's
    first SELECT sees nothing.
    """

    def __init__(self, real):
        self._real = real

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __getattr__(self, name):
        return getattr(self._real, name)


class _RaceSimConnection(sqlite3.Connection):
    """Connection simulating a lost unique-index race on one table.

    ``arm_integrity_race(conn, table)`` arms TWO independent one-shot
    triggers scoped to <table>:

    - the next SELECT touching <table> returns an empty result (the dedup
      pre-select misses the concurrent winner — recorded as
      "select-shadowed"), and
    - the next ``INSERT INTO <table>`` raises sqlite3.IntegrityError (the
      winner commits between the pre-select and the insert — recorded as
      "insert-attempted").

    Both attempts are recorded in ``race_events`` so a test can prove the
    racing INSERT actually fired in its shape. The recovery re-select —
    which the route must perform after rolling the failed insert back —
    runs for real and therefore sees the pre-seeded winner row.
    """

    @classmethod
    def arm_integrity_race(cls, conn, table):
        conn._race_table = table
        conn._race_shadow_select = table is not None
        conn._race_raise_insert = table is not None
        conn._race_events = []

    @property
    def race_events(self):
        return list(getattr(self, "_race_events", None) or [])

    def execute(self, sql, params=()):
        table = getattr(self, "_race_table", None)
        if table is not None:
            stripped = sql.lstrip()
            if (
                getattr(self, "_race_shadow_select", False)
                and stripped.upper().startswith("SELECT")
                and ("FROM %s" % table) in sql
            ):
                self._race_shadow_select = False
                self._race_events.append("select-shadowed")
                return _ShadowCursor(super().execute(sql, params))
            if getattr(self, "_race_raise_insert", False) and stripped.startswith(
                "INSERT INTO %s " % table
            ):
                self._race_raise_insert = False
                self._race_events.append("insert-attempted")
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: simulated concurrent winner"
                )
        return super().execute(sql, params)


class QualityReportIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        db_path = str(Path(self._tmpdir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(
            db_path, check_same_thread=False, factory=_RaceSimConnection
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Never let a one-shot race trigger leak past a test.
        _RaceSimConnection.arm_integrity_race(self.conn, None)
        self.addCleanup(_RaceSimConnection.arm_integrity_race, self.conn, None)

        vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('p04', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'P04', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, 'user', 'What is the"
            " project deadline?', 'turn-1', 1, 'done', CURRENT_TIMESTAMP)",
            (self.session_id,),
        )
        self.assistant_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources,"
            " turn_id, seq, status, created_at) VALUES"
            " (?, 'assistant', 'The deadline is October 15 [S1].', ?,"
            " 'turn-1', 2, 'done', CURRENT_TIMESTAMP)",
            (
                self.session_id,
                '[{"label": "S1", "file_id": 11, "file_hash": "sha-s1-001"}]',
            ),
        ).lastrowid
        # Second session/message pair giving the C12 race arm an
        # INDEPENDENT report identity (own session + message + category),
        # so no identity collides once submit idempotency lands.
        self.session2_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'P04B', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid
        self.question_rollback = "What does the rollback window cover?"
        self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id,"
            " seq, status, created_at) VALUES (?, 'user', ?, 'turn-r2', 1,"
            " 'done', CURRENT_TIMESTAMP)",
            (self.session2_id, self.question_rollback),
        )
        self.assistant2_msg_id = self.conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, sources,"
            " turn_id, seq, status, created_at) VALUES"
            " (?, 'assistant', 'The rollback window covers 48 hours [S1].',"
            " ?, 'turn-r2', 2, 'done', CURRENT_TIMESTAMP)",
            (
                self.session2_id,
                '[{"label": "S1", "file_id": 21, "file_hash": "sha-r2-001"}]',
            ),
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
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        for dep in (get_db, get_current_active_user, get_evaluate_policy, csrf_protect):
            self.addCleanup(app.dependency_overrides.pop, dep, None)

    def _report(self, category="incorrect_answer"):
        return {
            "session_id": self.session_id,
            "message_id": self.assistant_msg_id,
            "category": category,
            "note": "wrong date",
        }

    def _count_reports(self, category=None, session_id=None, message_id=None):
        session_id = session_id if session_id is not None else self.session_id
        message_id = (
            message_id if message_id is not None else self.assistant_msg_id
        )
        if category is None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM quality_reports WHERE session_id = ?"
                " AND message_id = ?",
                (session_id, message_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM quality_reports WHERE session_id = ?"
                " AND message_id = ? AND category = ?",
                (session_id, message_id, category),
            ).fetchone()
        return row[0]

    def test_duplicate_quality_report_submit_is_idempotent(self):
        # Primary leg: the identity (session_id, message_id, category)
        # names ONE report; resubmitting it must not create a second row.
        with self.subTest(leg="duplicate-identity-single-row"):
            first = self.client.post(
                "/api/quality/reports", json=self._report()
            )
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post(
                "/api/quality/reports", json=self._report()
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(self._count_reports("incorrect_answer"), 1)

        # Independent leg: a DIFFERENT category on the same message is a
        # separate report and must never be suppressed by the check above.
        with self.subTest(leg="independent-category-not-suppressed"):
            third = self.client.post(
                "/api/quality/reports",
                json=self._report(category="missing_source"),
            )
            self.assertEqual(third.status_code, 200, third.text)
            self.assertEqual(self._count_reports("missing_source"), 1)
            self.assertEqual(self._count_reports(), 2)

        # Concurrent-winner race arm: the stale_source identity already
        # exists (pre-seeded below — the concurrent winner). This
        # request's dedup pre-select is SHADOWED (misses the winner), its
        # INSERT raises sqlite3.IntegrityError, and the recovery — after
        # rolling the failed insert back — must re-select and return the
        # EXISTING row: 200 with the winner's id, never a 500 and never a
        # new row. The race_events assertions prove both the shadowed
        # pre-select and the racing INSERT actually fired in this shape.
        with self.subTest(leg="integrity-error-returns-existing-row"):
            winner_id = self.conn.execute(
                "INSERT INTO quality_reports (session_id, message_id,"
                " category, note, turn_id, seq, config_ref,"
                " source_file_hashes) VALUES (?, ?, 'stale_source', 'raced',"
                " 'turn-1', 2, 'unknown', '[]')",
                (self.session_id, self.assistant_msg_id),
            ).lastrowid
            self.conn.commit()
            _RaceSimConnection.arm_integrity_race(self.conn, "quality_reports")
            raced = self.client.post(
                "/api/quality/reports",
                json=self._report(category="stale_source"),
            )
            self.assertIn(
                "select-shadowed",
                self.conn.race_events,
                "the dedup pre-select never ran against quality_reports",
            )
            self.assertIn(
                "insert-attempted",
                self.conn.race_events,
                "the racing INSERT never fired in this shape",
            )
            self.assertEqual(raced.status_code, 200, raced.text)
            self.assertEqual(raced.json()["id"], winner_id)
            self.assertEqual(self._count_reports("stale_source"), 1)

    def test_duplicate_convert_is_idempotent(self):
        report = self.client.post(
            "/api/quality/reports", json=self._report()
        ).json()

        # Primary leg: converting the same report twice yields ONE case.
        with self.subTest(leg="double-convert-single-case"):
            url = "/api/quality/reports/%d/convert" % report["id"]
            first = self.client.post(
                url, json={"expected_outcome": "The deadline is October 15"}
            )
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post(
                url, json={"expected_outcome": "The deadline is October 15"}
            )
            self.assertEqual(second.status_code, 200, second.text)
            count = self.conn.execute(
                "SELECT COUNT(*) FROM quality_eval_cases WHERE report_id = ?",
                (report["id"],),
            ).fetchone()[0]
            self.assertEqual(count, 1)

        # Concurrent-winner race arm on an INDEPENDENT identity (own
        # session/message/category — session2 + bad_extraction — used by
        # no other leg): the winning writer already inserted the case for
        # this report (pre-seeded below). This convert's dedup pre-select
        # is SHADOWED (misses the winner), its INSERT raises
        # sqlite3.IntegrityError, and the recovery — after rolling the
        # failed insert back — must re-select and return the EXISTING
        # case: 200 with the winner's id, never a 500 and never a new
        # row. The race_events assertions prove the racing INSERT fired.
        with self.subTest(leg="integrity-error-returns-existing-case"):
            raced_report = self.client.post(
                "/api/quality/reports",
                json={
                    "session_id": self.session2_id,
                    "message_id": self.assistant2_msg_id,
                    "category": "bad_extraction",
                    "note": "raced convert",
                },
            ).json()
            winner_case_id = self.conn.execute(
                "INSERT INTO quality_eval_cases (report_id, query,"
                " expected_outcome, provenance) VALUES (?, ?, ?, '{}')",
                (
                    raced_report["id"],
                    self.question_rollback,
                    "The rollback window covers 48 hours",
                ),
            ).lastrowid
            self.conn.commit()
            _RaceSimConnection.arm_integrity_race(
                self.conn, "quality_eval_cases"
            )
            raced = self.client.post(
                "/api/quality/reports/%d/convert" % raced_report["id"],
                json={
                    "expected_outcome": "The rollback window covers 48 hours"
                },
            )
            self.assertIn(
                "select-shadowed",
                self.conn.race_events,
                "the dedup pre-select never ran against quality_eval_cases",
            )
            self.assertIn(
                "insert-attempted",
                self.conn.race_events,
                "the racing INSERT never fired in this shape",
            )
            self.assertEqual(raced.status_code, 200, raced.text)
            self.assertEqual(raced.json()["id"], winner_case_id)
            count = self.conn.execute(
                "SELECT COUNT(*) FROM quality_eval_cases WHERE report_id = ?",
                (raced_report["id"],),
            ).fetchone()[0]
            self.assertEqual(count, 1)


class ConfigRefTransientFailureRecoveryTest(unittest.TestCase):
    def test_config_ref_recovers_after_transient_failure(self):
        import app.services.eval_adapter as eval_adapter
        from app.api.routes import quality_reports as qr

        qr._release_id_cache.clear()
        self.addCleanup(qr._release_id_cache.clear)

        calls = {"count": 0}

        def flaky_get_release_id():
            calls["count"] += 1
            # First lookup fails (transient subprocess/env failure), the
            # second succeeds — the recovery the cache must not swallow.
            return None if calls["count"] == 1 else "abc123"

        original = eval_adapter._get_release_id
        eval_adapter._get_release_id = flaky_get_release_id
        self.addCleanup(
            setattr, eval_adapter, "_get_release_id", original
        )

        qr._config_ref()
        second = qr._config_ref()
        self.assertEqual(second, "abc123")


class SubmitRouteConfigRefOffloadTest(unittest.TestCase):
    def test_submit_quality_report_has_no_inline_config_ref(self):
        route_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "api"
            / "routes"
            / "quality_reports.py"
        )
        tree = ast.parse(route_path.read_text(encoding="utf-8"))

        submit_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "submit_quality_report"
        )

        def _calls_named(node, name):
            if isinstance(node.func, ast.Name):
                return node.func.id == name
            if isinstance(node.func, ast.Attribute):
                return node.func.attr == name
            return False

        def _is_to_thread_call(node):
            if isinstance(node.func, ast.Attribute):
                return node.func.attr == "to_thread"
            if isinstance(node.func, ast.Name):
                return node.func.id == "to_thread"
            return False

        parents = {}
        for parent in ast.walk(submit_fn):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        # A _config_ref(...) Call is offloaded only when its direct parent
        # is an asyncio.to_thread(...) call (covering both
        # ``asyncio.to_thread(_config_ref)`` — a bare Name, not a Call, so
        # never counted — and ``asyncio.to_thread(_config_ref())``). Every
        # other _config_ref Call, including a plain ``await _config_ref()``,
        # runs inline on the event loop and counts.
        inline = []
        for node in ast.walk(submit_fn):
            if not (
                isinstance(node, ast.Call) and _calls_named(node, "_config_ref")
            ):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and _is_to_thread_call(parent):
                continue
            inline.append(node)

        self.assertEqual(
            len(inline),
            0,
            "submit_quality_report calls _config_ref inline (not offloaded"
            " through asyncio.to_thread) %d time(s)" % len(inline),
        )


if __name__ == "__main__":
    unittest.main()
