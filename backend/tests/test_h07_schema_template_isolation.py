"""Schema-template isolation guards for the production-schema route fixture
(issues #743/#744, plan-critic round 1 REV-2).

user_route_helpers.setup_test_db hands every test a copy of a session-cached
init_db + run_migrations template. These tests prove the three properties the
fixture's correctness depends on:

1. copies are INDEPENDENT — rows written through one connection never appear
   in another test's database, and repeated setup calls start from the same
   pristine production shape;
2. the template build is HERMETIC — pinning settings.data_dir during the build
   means the ambient data dir and the settings object are restored afterwards,
   and no stray app.db / vault tree appears anywhere outside the temp dirs;
3. the cache rebuild path (reset_route_schema_template) produces the same
   production shape as the first build.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from user_route_helpers import (  # noqa: E402
    reset_route_schema_template,
    setup_test_db,
)


def _table_names(db_path: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


class TestSchemaTemplateIsolation:
    def test_copies_are_independent_writable_databases(self, tmp_path):
        db_a = str(tmp_path / "a.db")
        db_b = str(tmp_path / "b.db")

        conn_a = setup_test_db(db_a)
        conn_b = setup_test_db(db_b)
        try:
            conn_a.execute(
                "INSERT INTO users (username, hashed_password, role) "
                "VALUES ('isolation-a', 'x', 'member')"
            )
            conn_a.commit()

            leaked = conn_b.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'isolation-a'"
            ).fetchone()[0]
            assert leaked == 0, "rows written via copy A leaked into copy B"

            # Copy B must still be writable and hold its own rows.
            conn_b.execute(
                "INSERT INTO users (username, hashed_password, role) "
                "VALUES ('isolation-b', 'x', 'member')"
            )
            conn_b.commit()
            only_b = conn_a.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'isolation-b'"
            ).fetchone()[0]
            assert only_b == 0, "rows written via copy B leaked into copy A"
        finally:
            conn_a.close()
            conn_b.close()

    def test_repeated_setups_start_from_the_pristine_production_shape(self, tmp_path):
        first = str(tmp_path / "first.db")
        conn = setup_test_db(first)
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, role) "
                "VALUES ('polluter', 'x', 'member')"
            )
            conn.commit()
        finally:
            conn.close()

        second = str(tmp_path / "second.db")
        conn = setup_test_db(second)
        try:
            polluted = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'polluter'"
            ).fetchone()[0]
            assert polluted == 0, "cache hit leaked earlier test rows"
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()

        assert "security_audit_log" in tables
        assert "user_sessions" in tables

    def test_template_build_restores_data_dir_and_touches_nothing_ambient(self):
        from user_route_helpers import _build_template_db

        from app.config import settings

        ambient_data_dir = settings.data_dir
        ambient_listing_before = (
            sorted(p.name for p in Path(ambient_data_dir).glob("*"))
            if Path(ambient_data_dir).exists()
            else []
        )

        template = _build_template_db()

        assert settings.data_dir == ambient_data_dir, (
            "settings.data_dir must be restored after the template build"
        )
        assert template.exists()
        tables = _table_names(str(template))
        assert "security_audit_log" in tables and "user_sessions" in tables

        # The build happened inside its own temp dir, never the ambient tree.
        ambient_listing_after = (
            sorted(p.name for p in Path(ambient_data_dir).glob("*"))
            if Path(ambient_data_dir).exists()
            else []
        )
        assert ambient_listing_after == ambient_listing_before, (
            "template build created/removed entries in the ambient data dir"
        )
        # The only app.db anywhere near this build lives inside the build dir.
        assert list(template.parent.glob("**/app.db")) == []

    def test_cache_rebuild_reproduces_the_production_shape(self, tmp_path):
        before = str(tmp_path / "before.db")
        conn = setup_test_db(before)
        conn.close()
        shape_before = _table_names(before)

        reset_route_schema_template()

        after = str(tmp_path / "after.db")
        conn = setup_test_db(after)
        conn.close()
        shape_after = _table_names(after)

        assert shape_after == shape_before, (
            "rebuilt template drifted from the first build's table set"
        )
        assert "security_audit_log" in shape_after

    def test_copies_carry_no_wal_sidecar_residue(self, tmp_path):
        db_path = str(tmp_path / "copy.db")
        conn = setup_test_db(db_path)
        conn.close()
        assert not Path(db_path + "-wal").exists(), (
            "a fresh copy must not inherit WAL sidecar state from the template"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        pytest.main([__file__, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"])
    )
