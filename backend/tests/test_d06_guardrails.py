"""Issue #724 guardrails (Workstream D PR 6 of 8) — beyond the frozen checks.

Companion to test_d06_csrf_store_and_admin.py (the frozen acceptance
checks). This file pins the recurrence-class surfaces the frozen checks do
not reach:

  (a) the mutation-route CSRF census over the admin + service-accounts
      routers (every POST/PUT/PATCH/DELETE invokes csrf_protect — any new
      unguarded mutation in these routers fails this census);
  (b) the rotation grace TTL expiry leg (the old key dies at the grace
      bound even if the new key is never used);
  (c) the legacy-row upgrade lookup for the hashed fallback store (hashed
      row preserves expires_at; the raw row is gone in the same step);
  (d) the failover round-trip (Redis returning flips the manager back);
  (e) the deps.py Path-2 validator grace leg (previous key accepted through
      the shared lookup helper used by the document/search/tag routes).
"""

import os
import sqlite3
import sys
import tempfile
import time as time_mod
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# This module manages csrf explicitly (the census introspects it), so the
# conftest bypass is off — every HTTP leg installs its own override.
CSRF_TEST_POLICY = "manages"

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_or_service_account
from app.api.routes import admin as admin_module
from app.api.routes import service_accounts as sa_module
from app.models.database import init_db, lookup_service_account
from app.security import CSRFManager, _SQLiteCSRFStore, csrf_protect


def _iter_api_routes(router):
    """Yield concrete APIRoutes, flattening FastAPI lazy _IncludedRouter."""
    for route in router.routes:
        inner = getattr(route, "app", None)
        if inner is not None and hasattr(inner, "routes"):
            yield from _iter_api_routes(inner)
        elif getattr(route, "methods", None):
            yield route


class TestMutationRouteCsrfCensus(unittest.TestCase):
    """(a) every mutating route on the two routers enforces csrf_protect."""

    _MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

    def _invokes_csrf(self, route) -> bool:
        def walk(deps):
            for dep in deps:
                call = getattr(dep, "call", None)
                if call is not None and getattr(call, "__name__", "") == "csrf_protect":
                    return True
                if walk(getattr(dep, "dependencies", [])):
                    return True
            return False

        return walk(route.dependant.dependencies)

    def test_admin_router_mutations_all_invoke_csrf_protect(self):
        missing = [
            (sorted(r.methods & self._MUTATING), r.path)
            for r in _iter_api_routes(admin_module.router)
            if r.methods & self._MUTATING and not self._invokes_csrf(r)
        ]
        self.assertEqual(
            missing,
            [],
            "every mutating admin route must invoke csrf_protect "
            f"(issue #724 class guardrail); missing: {missing}",
        )

    def test_service_accounts_router_mutations_all_invoke_csrf_protect(self):
        missing = [
            (sorted(r.methods & self._MUTATING), r.path)
            for r in _iter_api_routes(sa_module.router)
            if r.methods & self._MUTATING and not self._invokes_csrf(r)
        ]
        self.assertEqual(missing, [], f"missing csrf_protect on: {missing}")


class TestRotationGraceTTL(unittest.TestCase):
    """(b) the old key dies at the grace bound even if the new key is unused."""

    def test_previous_key_rejected_after_grace_expiry(self):
        import hashlib

        td = tempfile.mkdtemp()
        db = str(Path(td) / "grace.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            old_hash = hashlib.sha256(b"old-key-material").hexdigest()
            new_hash = hashlib.sha256(b"new-key-material").hexdigest()
            # ISO-8601 in the past, matching what rotate writes: the expiry
            # COMPARISON branch must be the thing under test (a float epoch
            # would die in the fromisoformat ValueError arm instead).
            expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at, "
                "previous_key_hash, previous_key_expires_at) "
                "VALUES ('g', ?, 'documents:read', datetime('now'), ?, ?)",
                (new_hash, old_hash, expired),
            )
            conn.commit()
            row, via_previous = lookup_service_account(conn, old_hash)
            self.assertIsNone(row, "an expired grace must read as no match")
            self.assertFalse(via_previous)
            # The NEW key still works
            row2, via2 = lookup_service_account(conn, new_hash)
            self.assertIsNotNone(row2)
            self.assertFalse(via2)
        finally:
            conn.close()
            import shutil

            shutil.rmtree(td, ignore_errors=True)


class TestLegacyRowUpgrade(unittest.TestCase):
    """(c) the hashed store upgrades pre-fix raw rows on lookup."""

    def test_legacy_raw_row_upgraded_preserving_expiry(self):
        td = tempfile.mkdtemp()
        db_path = str(Path(td) / "csrf.db")
        store = _SQLiteCSRFStore(db_path, ttl=900)
        raw_key = "csrf:legacy-token-value"
        original_expiry = time_mod.time() + 600
        # A pre-fix row: raw key at rest, long remaining TTL.
        store._conn.execute(
            "INSERT INTO csrf_tokens (token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?)",
            (raw_key, time_mod.time(), original_expiry),
        )
        store._conn.commit()

        self.assertEqual(store.get(raw_key), "1")

        rows = store._conn.execute(
            "SELECT token_hash, expires_at FROM csrf_tokens"
        ).fetchall()
        self.assertEqual(len(rows), 1, "the raw row must be gone after upgrade")
        stored_key, stored_expiry = rows[0]
        self.assertNotIn("legacy-token-value", stored_key)
        self.assertEqual(stored_key, _SQLiteCSRFStore._hashed(raw_key))
        self.assertAlmostEqual(
            stored_expiry,
            original_expiry,
            delta=1.0,
            msg="the upgrade must preserve the original expires_at",
        )
        # And delete removes the hashed row.
        store.delete(raw_key)
        self.assertIsNone(store.get(raw_key))
        self.assertEqual(
            store._conn.execute("SELECT COUNT(*) FROM csrf_tokens").fetchone()[0], 0
        )

    def test_expired_legacy_row_is_not_resurrected(self):
        td = tempfile.mkdtemp()
        store = _SQLiteCSRFStore(str(Path(td) / "csrf.db"), ttl=900)
        raw_key = "csrf:expired-legacy"
        store._conn.execute(
            "INSERT INTO csrf_tokens (token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?)",
            (raw_key, time_mod.time() - 1000, time_mod.time() - 100),
        )
        store._conn.commit()
        self.assertIsNone(store.get(raw_key))
        # The expired raw row stays untouched (not upgraded, not deleted) —
        # the periodic expiry cleanup owns it.
        self.assertEqual(
            store._conn.execute(
                "SELECT COUNT(*) FROM csrf_tokens WHERE token_hash = ?",
                (raw_key,),
            ).fetchone()[0],
            1,
        )


class TestFailoverRoundTrip(unittest.TestCase):
    """(d) Redis returning flips the manager back (recovery edge intact)."""

    def test_fallback_then_redis_recovery_flips_back(self):
        import redis as redis_mod

        class _FlappyRedis:
            def __init__(self):
                self.dead = False

            def ping(self):
                if self.dead:
                    raise redis_mod.ConnectionError("down")
                return True

            def setex(self, *a, **k):
                if self.dead:
                    raise redis_mod.ConnectionError("down")

            def get(self, *a, **k):
                if self.dead:
                    raise redis_mod.ConnectionError("down")

        stub = _FlappyRedis()
        td = tempfile.mkdtemp()
        with patch("app.security.redis.from_url", return_value=stub):
            mgr = CSRFManager(
                redis_url="redis://stub",
                db_path=str(Path(td) / "csrf.db"),
            )
            stub.dead = True
            token = mgr.generate_token()  # fails over, no 503
            self.assertTrue(mgr._use_fallback)
            stub.dead = False
            mgr._next_recovery_ping_at = 0.0  # bypass the backoff window
            token2 = mgr.generate_token()  # recovery edge flips back
            self.assertFalse(mgr._use_fallback)
        self.assertNotEqual(token, token2)


class TestDepsPath2GraceLeg(unittest.TestCase):
    """(e) the deps.py validator path accepts a previous key via the helper."""

    def test_path2_lookup_accepts_previous_key(self):
        import hashlib
        from datetime import datetime, timedelta, timezone

        td = tempfile.mkdtemp()
        db = str(Path(td) / "p2.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            old_hash = hashlib.sha256(b"p2-old").hexdigest()
            new_hash = hashlib.sha256(b"p2-new").hexdigest()
            grace = (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at, "
                "previous_key_hash, previous_key_expires_at) "
                "VALUES ('p2', ?, 'documents:read', datetime('now'), ?, ?)",
                (new_hash, old_hash, grace),
            )
            conn.commit()
            row, via = lookup_service_account(conn, old_hash)
            self.assertIsNotNone(row, "the previous key must resolve (grace)")
            self.assertTrue(via)
            self.assertEqual(row[2], "documents:read")
        finally:
            conn.close()
            import shutil

            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
