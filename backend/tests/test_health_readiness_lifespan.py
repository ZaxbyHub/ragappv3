"""Lifespan wiring guard for the readiness migration flag (issue #550).

The AC1 behavior (healthz 503 when migrations_ok is False) is pinned by the
frozen check in test_health_readiness.py, which sets the flag directly. This
guard locks the OTHER half of the wiring: lifespan must actually record the
outcome of its run_migrations call on app.state, or the readiness signal is
dead code. Source-text inspection follows the repo convention for lifespan
wiring claims that are invisible to behavior tests (see
test_lifespan_pool_seeding_order.py): running the real lifespan() needs heavy
optional deps (lancedb, LLM clients) impractical in unit CI. The guards match
literal code identifiers, so reverting the fix fails them.
"""

import os
import sys
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BACKEND = Path(__file__).resolve().parents[1]
LIFESPAN = BACKEND / "app" / "lifespan.py"
HEALTH = BACKEND / "app" / "api" / "routes" / "health.py"


class TestLifespanMigrationFlagWiring(unittest.TestCase):
    """Issue #550: the startup migration outcome must reach the probe."""

    def test_lifespan_records_migration_outcome(self):
        source = LIFESPAN.read_text(encoding="utf-8")

        call_at = source.find("run_migrations(str(settings.sqlite_path))")
        self.assertGreater(call_at, -1, "lifespan must call run_migrations")

        set_true_at = source.find("app.state.migrations_ok = True")
        set_false_at = source.find("app.state.migrations_ok = False")
        self.assertGreater(
            set_true_at, -1, "lifespan must pre-set migrations_ok = True"
        )
        self.assertGreater(
            set_false_at,
            -1,
            "lifespan must record a failed migration as migrations_ok = False",
        )
        self.assertLess(
            set_true_at,
            call_at,
            "migrations_ok = True must be set before the migration call",
        )
        self.assertGreater(
            set_false_at,
            call_at,
            "migrations_ok = False must be recorded in the failure path after the call",
        )

    def test_healthz_reads_the_recorded_flag(self):
        source = HEALTH.read_text(encoding="utf-8")
        self.assertIn(
            'getattr(state, "migrations_ok", None) is False',
            source,
            "healthz must gate on the lifespan-recorded migrations_ok flag",
        )


if __name__ == "__main__":
    unittest.main()
