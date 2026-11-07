"""Semantic regressions for the #749 operator-path fixes (RED at base, GREEN
after the fix — the issues' frozen checks in test_i02_operator_paths.py pin
the same behaviors textually; these assert the SEMANTICS the plan critic's
round-1/round-3 findings required: per-document ownership steps, exit-7/8
message shape and taxonomy, the runnable hint command, and the CPU override's
merged configuration).
"""

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _load_migrate_module():
    """Load scripts/migrate_embeddings.py the way the frozen checks do."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migrate_embeddings_i02b_semantics", REPO / "scripts" / "migrate_embeddings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outcome(callable_):
    """Classify a run_migration call: returned / exit N / raised <type>."""
    try:
        rc = callable_()
    except SystemExit as exc:  # noqa: PERF203 - the shape under test
        code = exc.code
        return f"exit {code}" if isinstance(code, int) and code else "returned 0"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__
    return "returned" if rc == 0 else f"returned {rc}"


class TestExitSevenBothMissing(unittest.TestCase):
    """Exit 7 names BOTH paths and is not overridden by --force/--dry-run."""

    def test_both_missing_names_both_paths(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance, sql = tmp / "missing-lancedb", tmp / "missing.db"
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(lance, sql)
            self.assertEqual(ctx.exception.code, 7)
            # The refusal message must name BOTH missing paths (round-1
            # finding 7: an unactionable refusal cannot pass).
            import contextlib
            import io

            err = io.StringIO()
            with contextlib.redirect_stdout(err):
                try:
                    mod.run_migration(lance, sql)
                except SystemExit:
                    pass
            self.assertIn(str(lance), err.getvalue())
            self.assertIn(str(sql), err.getvalue())

    def test_exit7_not_overridden_by_force(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(
                    tmp / "missing-lancedb", tmp / "missing.db", force=True
                )
            self.assertEqual(ctx.exception.code, 7)

    def test_exit7_not_overridden_by_dry_run(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(
                    tmp / "missing-lancedb", tmp / "missing.db", dry_run=True
                )
            self.assertEqual(ctx.exception.code, 7)


class TestExitEightSentinelSurvival(unittest.TestCase):
    """Exit 8 refuses destructive runs without the application database.

    A sentinel file inside the LanceDB directory must survive every refused
    run (the base behavior wiped it — the plan-critic round-1 executed probe).
    """

    def _stale_lance(self, tmp: Path) -> Path:
        lance = tmp / "lancedb"
        lance.mkdir()
        # force=True proceeds past stored-dimension detection on a
        # non-LanceDB directory; the sentinel doubles as wipe evidence.
        (lance / "sentinel.keep").write_text("do-not-delete", encoding="utf-8")
        return lance

    def test_missing_sqlite_refused_and_sentinel_survives(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._stale_lance(tmp)
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(lance, tmp / "missing-app.db", force=True)
            self.assertEqual(ctx.exception.code, 8)
            self.assertTrue((lance / "sentinel.keep").exists())

    def test_missing_sqlite_refused_even_with_force_on_wipe_path(self):
        # Duplicate-shaped guard: --force must not bypass exit 8 either
        # (wrong database is wrong database, same as exit 7).
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._stale_lance(tmp)
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(lance, tmp / "missing-app.db", force=True)
            self.assertEqual(ctx.exception.code, 8)

    def test_no_files_table_refused_and_sentinel_survives(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._stale_lance(tmp)
            empty_db = tmp / "empty.db"
            conn = sqlite3.connect(str(empty_db))
            conn.close()  # a valid sqlite file with no application schema
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(lance, empty_db, force=True)
            self.assertEqual(ctx.exception.code, 8)
            self.assertTrue((lance / "sentinel.keep").exists())

    def test_no_files_table_refused_in_dry_run(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._stale_lance(tmp)
            empty_db = tmp / "empty.db"
            conn = sqlite3.connect(str(empty_db))
            conn.close()
            with self.assertRaises(SystemExit) as ctx:
                mod.run_migration(lance, empty_db, dry_run=True, force=True)
            self.assertEqual(ctx.exception.code, 8)


class TestApplicationDatabaseIdentity(unittest.TestCase):
    """The exit-8 guard must establish application identity, not just a
    table named ``files`` (pr1081-20261106 PRR-005): an unrelated database
    with a stub ``files`` table, a table without the application's core
    columns, and a backup copy such as ``app.db.bak`` are each refused before
    any reset/wipe, with the LanceDB sentinel surviving every refusal."""

    def _mk(self, tmp: Path) -> Path:
        lance = tmp / "lancedb"
        lance.mkdir()
        (lance / "sentinel.keep").write_text("do-not-delete", encoding="utf-8")
        return lance

    def _refusal_stdout(self, callable_):
        import contextlib
        import io

        err = io.StringIO()
        code = None
        with contextlib.redirect_stdout(err):
            try:
                callable_()
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def test_unrelated_files_table_refused_before_wipe(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            unrelated = tmp / "unrelated.db"
            conn = sqlite3.connect(str(unrelated))
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            code, out = self._refusal_stdout(
                lambda: mod.run_migration(lance, unrelated, force=True)
            )
            self.assertEqual(code, 8, "stub files table must be refused with exit 8")
            self.assertIn("core columns", out)
            self.assertTrue((lance / "sentinel.keep").exists())

    def test_backup_copy_refused_before_wipe(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            backup = tmp / "app.db.bak"  # README's own backup layout
            conn = sqlite3.connect(str(backup))
            conn.execute(
                "CREATE TABLE files (id INTEGER PRIMARY KEY, vault_id INTEGER, "
                "status TEXT)"
            )
            conn.commit()
            conn.close()
            code, out = self._refusal_stdout(
                lambda: mod.run_migration(lance, backup, force=True)
            )
            self.assertEqual(
                code, 8, "a backup copy must be refused with exit 8 even "
                "when the schema looks like the application database"
            )
            self.assertIn("BACKUP", out)
            self.assertTrue((lance / "sentinel.keep").exists())

    def test_zero_claim_db_warns_before_wipe(self):
        """A schema-valid database that holds NO rows claiming vectors
        (indexed/partial) gets the loud wrong-database WARNING before the
        wipe and the destructive run completes: that state is
        indistinguishable from the documented reset-then-crash repair re-run
        and a fresh boot's empty chunks table, so a hard refusal broke both
        contracts (delta review executed probe) — the demotion to WARNING is
        the reviewed resolution."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute(
                "CREATE TABLE files (id INTEGER PRIMARY KEY, vault_id INTEGER, "
                "status TEXT)"
            )
            # one row with a NON-claiming status: the guard's column check
            # passes, but nothing accounts for the index
            conn.execute(
                "INSERT INTO files (id, vault_id, status) VALUES (1, 1, 'archived')"
            )
            conn.commit()
            conn.close()
            code, out = self._refusal_stdout(
                lambda: mod.run_migration(lance, app_db, force=True)
            )
            self.assertIsNone(
                code, "zero-claim DB: destructive run completes (no SystemExit)"
            )
            self.assertIn(
                "NO rows claiming",
                re.sub(r"\s+", " ", out),
                "the wrong-database warning must state the zero-claims evidence",
            )
            self.assertFalse(
                (lance / "sentinel.keep").exists(),
                "the wipe proceeds after the warning (documented repair re-run)",
            )

    def test_zero_claim_db_dry_run_warns_and_wipes_nothing(self):
        """Dry-run twin (batch-3 review): the zero-claim WARNING also prints
        on the dry-run branch — with the repair-re-run sentence — and the
        dry run still wipes nothing (sentinel intact, sentinel file present)."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute(
                "CREATE TABLE files (id INTEGER PRIMARY KEY, vault_id INTEGER, "
                "status TEXT)"
            )
            conn.execute(
                "INSERT INTO files (id, vault_id, status) VALUES (1, 1, 'archived')"
            )
            conn.commit()
            conn.close()
            code, out = self._refusal_stdout(
                # force=True: the sentinel-only index has no detectable
                # stored dimension (exit 3) — --force overrides it so the
                # run reaches the dry-run warning branch under test.
                lambda: mod.run_migration(lance, app_db, dry_run=True, force=True)
            )
            self.assertIsNone(code, "dry run completes (no SystemExit)")
            flattened = re.sub(r"\s+", " ", out)
            self.assertIn(
                "NO rows claiming",
                flattened,
                "the dry-run plan must carry the wrong-database warning",
            )
            self.assertIn(
                "documented repair",
                flattened,
                "the dry-run warning carries the repair-re-run sentence",
            )
            self.assertTrue(
                (lance / "sentinel.keep").exists(),
                "dry run must wipe nothing",
            )
            conn = sqlite3.connect(str(app_db))
            status = conn.execute(
                "SELECT status FROM files WHERE id = 1"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(status, "archived", "dry run resets nothing")

    @unittest.skipIf(os.name == "nt", "symlink creation needs privilege on Windows")
    def test_symlinked_live_name_to_backup_refused(self):
        """A live-looking path that RESOLVES to a backup must be refused:
        the backup-name check runs on the resolved target's name too
        (Multi-Stage batch-2 symlink bypass)."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            backup = tmp / "app.db.bak"
            conn = sqlite3.connect(str(backup))
            conn.execute(
                "CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)"
            )
            conn.commit()
            conn.close()
            live_link = tmp / "app.db"
            live_link.symlink_to(backup)
            code, out = self._refusal_stdout(
                lambda: mod.run_migration(lance, live_link, force=True)
            )
            self.assertEqual(
                code, 8, "live name resolving to a backup must be refused"
            )
            self.assertIn("BACKUP", out)
            self.assertTrue((lance / "sentinel.keep").exists())

    def test_second_connection_failure_is_migration_refused(self):
        """A failure raised by the SECOND connection (the identity PRAGMA)
        must surface as MigrationRefused guidance, not a bare traceback
        (Multi-Stage batch-2 MEDIUM)."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)")
            conn.commit()
            conn.close()

            real_connect = sqlite3.connect
            calls = {"n": 0}

            def flaky_connect(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] >= 2:  # fail only the SECOND connection
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(*args, **kwargs)

            mod.sqlite3.connect = flaky_connect  # type: ignore[attr-defined]
            try:
                with self.assertRaises(mod.MigrationRefused) as ctx:
                    mod.run_migration(lance, app_db, force=True)
                message = str(ctx.exception)
                self.assertIn("not a writer lock", message)
                self.assertIn("unable to open database file", message)
            finally:
                mod.sqlite3.connect = real_connect  # type: ignore[attr-defined]

    def test_claims_evidence_passes_guard_and_wipes(self):
        """The pass-through half: a schema-valid database whose rows DO claim
        vectors (indexed) passes every refusal and the destructive run
        completes (the reset flips the row to pending)."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute(
                "CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)"
            )
            conn.execute(
                "INSERT INTO files (id, status) VALUES (1, 'indexed')"
            )
            conn.commit()
            conn.close()
            outcome = mod.run_migration(lance, app_db, force=True)
            self.assertEqual(
                outcome, 1, "claiming DB: destructive run completes (ret 1)"
            )
            self.assertFalse(
                (lance / "sentinel.keep").exists(),
                "the wipe must have proceeded past every refusal",
            )
            conn = sqlite3.connect(str(app_db))
            status = conn.execute(
                "SELECT status FROM files WHERE id = 1"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(status, "pending", "reset ran before the wipe")

    def _mk_index(self, tmp: Path, file_ids):
        """A REAL lancedb chunks table carrying ``file_ids`` (needs lancedb)."""
        try:
            import lancedb
        except ImportError:  # pragma: no cover - backend dep, present in CI
            self.skipTest("lancedb not installed")
        lance = tmp / "lancedb"
        lance.mkdir()
        db = lancedb.connect(str(lance))
        db.create_table(
            "chunks",
            data=[
                {"id": f"c{i}", "file_id": fid, "embedding": [0.1, 0.2]}
                for i, fid in enumerate(file_ids)
            ],
        )
        return lance

    def _mk_claiming_db(self, tmp: Path) -> Path:
        app_db = tmp / "app.db"
        conn = sqlite3.connect(str(app_db))
        conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO files (id, status) VALUES (1, 'indexed')")
        conn.commit()
        conn.close()
        return app_db

    def test_claims_sharing_no_ids_with_index_refused(self):
        """Batch-5 review (hermes HIGH): a schema-valid database whose
        vector-claiming rows have NO chunks in a non-empty index does not
        describe that index — refused with exit 8 (live AND dry run) before
        the reset or the wipe."""
        import tempfile

        mod = _load_migrate_module()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                lance = self._mk_index(tmp, ["999", "1000"])
                app_db = self._mk_claiming_db(tmp)
                code, out = self._refusal_stdout(
                    lambda: mod.run_migration(
                        lance, app_db, dry_run=dry_run, force=True
                    )
                )
                self.assertEqual(code, 8, "foreign database must be refused")
                self.assertIn("does not describe the index", " ".join(out.split()))
                self.assertTrue(
                    (lance / "chunks.lance").exists(), "index survives the refusal"
                )
                conn = sqlite3.connect(str(app_db))
                status = conn.execute("SELECT status FROM files").fetchone()[0]
                conn.close()
                self.assertEqual(status, "indexed", "no reset on refusal")

    def test_claims_sharing_ids_with_index_passes_and_wipes(self):
        """The pass-through half of the cross-store check: claimed rows that
        DO have chunks in the index pass and the migration completes."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk_index(tmp, ["1", "1"])
            app_db = self._mk_claiming_db(tmp)
            outcome = mod.run_migration(lance, app_db, force=True)
            self.assertEqual(outcome, 1, "matching database: run completes")
            self.assertFalse((lance / "chunks.lance").exists(), "index wiped")
            conn = sqlite3.connect(str(app_db))
            status = conn.execute("SELECT status FROM files").fetchone()[0]
            conn.close()
            self.assertEqual(status, "pending")

    def test_rotated_backup_names_refused_live_and_dry_run(self):
        """Batch-7 review: rotated/stamped backup names (``app.db.bak.1``,
        ``app.db.backup-2024``, ``app_backup.db``) were let through by the
        exact-suffix match. Each is refused with exit 8 before any reset or
        wipe, live and dry run, and the sentinel survives."""
        import tempfile

        mod = _load_migrate_module()
        for name in ("app.db.bak.1", "app.db.backup-2024", "app_backup.db", "app.db~"):
            for dry_run in (False, True):
                with self.subTest(name=name, dry_run=dry_run), tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    lance = self._mk(tmp)
                    backup = tmp / name
                    conn = sqlite3.connect(str(backup))
                    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)")
                    conn.execute("INSERT INTO files (id, status) VALUES (1, 'indexed')")
                    conn.commit()
                    conn.close()
                    code, out = self._refusal_stdout(
                        lambda: mod.run_migration(lance, backup, dry_run=dry_run, force=True)
                    )
                    self.assertEqual(code, 8, f"{name} must be refused")
                    self.assertIn("BACKUP", out)
                    self.assertTrue((lance / "sentinel.keep").exists())

    def test_live_looking_names_are_not_backups(self):
        mod = _load_migrate_module()
        for name in ("app.db", "knowledge.db", "bakery.db", "original_corpus.db", "app.db-wal"):
            with self.subTest(name=name):
                self.assertFalse(mod._looks_like_backup(name), name)

    def test_identity_probe_is_batched_and_stops_at_first_hit(self):
        """Batch-7 review: the identity check must not materialise the index.
        A counting stand-in for lancedb proves the claimed ids are probed in
        bounded batches and the scan stops at the first matching batch."""
        import tempfile
        import types

        mod = _load_migrate_module()
        filters = []

        class _Query:
            def __init__(self):
                self._filter = ""

            def where(self, clause):
                self._filter = clause
                filters.append(clause)
                return self

            def select(self, _cols):
                return self

            def limit(self, _n):
                return self

            def to_arrow(self):
                return types.SimpleNamespace(num_rows=1 if "'2'" in self._filter else 0)

        table = types.SimpleNamespace(
            schema=types.SimpleNamespace(names=["id", "file_id", "embedding"]),
            count_rows=lambda: 1_000_000,
            search=_Query,
        )
        fake = types.SimpleNamespace(
            connect=lambda _p: types.SimpleNamespace(
                table_names=lambda: ["chunks"], open_table=lambda _n: table
            )
        )
        saved_mod = sys.modules.get("lancedb")
        saved_batch = mod._ID_PROBE_BATCH
        try:
            sys.modules["lancedb"] = fake
            mod._ID_PROBE_BATCH = 1
            with tempfile.TemporaryDirectory() as td:
                lance = Path(td)
                (lance / "chunks.lance").mkdir()
                result = mod._index_shares_claimed_ids(lance, {"1", "2", "3", "4"})
        finally:
            mod._ID_PROBE_BATCH = saved_batch
            if saved_mod is None:
                sys.modules.pop("lancedb", None)
            else:
                sys.modules["lancedb"] = saved_mod
        self.assertIs(result, True)
        self.assertEqual(
            filters,
            ["file_id IN ('1')", "file_id IN ('2')"],
            "one bounded batch per query, stopping at the first hit",
        )

    def test_identity_probe_multi_batch_real_index(self):
        """Real lancedb, batch size 1: the only shared id sits in the LAST
        batch (pass-through), and with no shared id every batch misses
        (refusal)."""
        import tempfile

        mod = _load_migrate_module()
        saved_batch = mod._ID_PROBE_BATCH
        try:
            mod._ID_PROBE_BATCH = 1
            with tempfile.TemporaryDirectory() as td:
                lance = self._mk_index(Path(td), ["3", "3"])
                self.assertIs(mod._index_shares_claimed_ids(lance, {"1", "2", "3"}), True)
                self.assertIs(mod._index_shares_claimed_ids(lance, {"1", "2"}), False)
        finally:
            mod._ID_PROBE_BATCH = saved_batch


class TestContainerCmdSemantics(unittest.TestCase):
    """Argv-level companion to the frozen lifecycle checks (batch-5 review):
    the frozen C-checks accept any exec'd program and search the whole CMD
    text for ``--workers 1``. Parse the CMD instead: the shell must exec
    uvicorn itself, as the only command, with ``--workers 1`` in uvicorn's
    own argv. This pins the SHIPPED ``sh -c`` form: moving to an init
    wrapper (tini/dumb-init) is a deliberate topology change that must
    update this test alongside the frozen C-checks."""

    def test_cmd_execs_uvicorn_with_single_worker(self):
        import json

        cmd_lines = [
            line
            for line in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines()
            if line.startswith("CMD ")
        ]
        self.assertTrue(cmd_lines, "Dockerfile must declare a CMD")
        argv = json.loads(cmd_lines[-1][len("CMD ") :])
        self.assertEqual(argv[:2], ["sh", "-c"], "exec-form sh -c wrapper expected")
        self.assertEqual(len(argv), 3, "one shell script argument")
        tokens = shlex.split(argv[2])
        for separator in (";", "&&", "||", "&", "|"):
            self.assertNotIn(separator, tokens, "a single command, nothing after it")
        self.assertEqual(tokens[0], "exec", "the shell must exec, not fork")
        self.assertEqual(tokens[1], "uvicorn", "uvicorn itself replaces the shell")
        uvicorn_argv = tokens[2:]
        self.assertIn("--workers", uvicorn_argv)
        self.assertEqual(
            uvicorn_argv[uvicorn_argv.index("--workers") + 1],
            "1",
            "--workers 1 must be uvicorn's own argument",
        )


class TestCorruptDatabaseKeepsExitFive(unittest.TestCase):
    """The new helper must not reclassify corruption (round-3 finding 3).

    Corrupt files keep the writer probe's MigrationRefused semantics (main()
    maps them to exit 5); OperationalError (a SUBCLASS of DatabaseError) keeps
    the stop-the-writer message, never restore-from-backup advice.
    """

    def test_corrupt_sqlite_is_migration_refused_with_corruption_message(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            corrupt = tmp / "corrupt.db"
            corrupt.write_bytes(b"this is not a database" * 16)
            with self.assertRaises(mod.MigrationRefused) as ctx:
                mod.run_migration(lance, corrupt, force=True)
            self.assertIn("unreadable or corrupt", str(ctx.exception))

    def test_lock_shaped_failure_keeps_stop_the_writer_message(self):
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            real_connect = sqlite3.connect

            def locked_connect(*args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            # Patch the module's sqlite3 attribute table (the module resolved
            # `sqlite3` by global name at call time).
            mod.sqlite3.connect = locked_connect  # type: ignore[attr-defined]
            try:
                with self.assertRaises(mod.MigrationRefused) as ctx:
                    mod.run_migration(lance, app_db, force=True)
                message = str(ctx.exception)
                self.assertIn("write lock", message)
                self.assertNotIn("unreadable or corrupt", message)
            finally:
                mod.sqlite3.connect = real_connect  # type: ignore[attr-defined]

    def test_non_lock_open_failure_gets_not_a_lock_message(self):
        """A non-lock OperationalError (permission/read-only open failure)
        keeps the underlying error but must NOT be advised as a writer lock
        (pr1081-20261106 PRR-006; the delta reviewer's revert-probe nit)."""
        import tempfile

        mod = _load_migrate_module()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lance = self._mk(tmp)
            app_db = tmp / "app.db"
            conn = sqlite3.connect(str(app_db))
            conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            real_connect = sqlite3.connect

            def denied_connect(*args, **kwargs):
                raise sqlite3.OperationalError("unable to open database file")

            mod.sqlite3.connect = denied_connect  # type: ignore[attr-defined]
            try:
                with self.assertRaises(mod.MigrationRefused) as ctx:
                    mod.run_migration(lance, app_db, force=True)
                message = str(ctx.exception)
                self.assertIn("not a writer lock", message)
                self.assertIn("unable to open database file", message)
                self.assertNotIn("write lock", message)
            finally:
                mod.sqlite3.connect = real_connect  # type: ignore[attr-defined]

    def _mk(self, tmp: Path) -> Path:
        lance = tmp / "lancedb"
        lance.mkdir()
        (lance / "sentinel.keep").write_text("do-not-delete", encoding="utf-8")
        return lance


class TestRunnableHealthHint(unittest.TestCase):
    """The stale-embeddings hint must carry a literally runnable command."""

    def test_hint_command_is_runnable_shell_with_real_defaults(self):
        source = (BACKEND / "app" / "api" / "routes" / "health.py").read_text(
            encoding="utf-8"
        )
        # Extract the command from 'Run: python' up to the app.db token.
        start = source.find("Run: python scripts/migrate_embeddings.py")
        self.assertGreater(start, -1, "hint must name the interpreter + script")
        end = source.find("app.db", start)
        self.assertGreater(end, -1, "hint must carry the sqlite path flag value")
        # Join the source lines of the command (it wraps across string
        # literals — strip the quote characters those joints carry, then
        # re-normalize the whitespace the stripping leaves behind).
        segment = re.sub(r"\s+", " ", source[start : end + len("app.db")])
        segment = re.sub(r"\s+", " ", re.sub(r'[\'"]', "", segment))
        self.assertNotIn("<", segment, "no placeholder redirections in the command")
        self.assertNotIn(">", segment, "no placeholder redirections in the command")
        self.assertIn("--lancedb-path ./data/lancedb", segment)
        self.assertIn("--sqlite-path ./data/app.db", segment)
        # And the command must tokenize as a valid shell command (shlex —
        # portable; a subprocess `bash -n` resolves to the broken WSL stub
        # on this Windows box and adds nothing shlex does not catch here).
        command = segment.split("Run: ", 1)[1].split("(from the repo root", 1)[0]
        command = command.strip()
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], "python", "command starts with the interpreter")
        self.assertEqual(tokens[1], "scripts/migrate_embeddings.py")
        self.assertIn("--lancedb-path", tokens)
        self.assertIn("--sqlite-path", tokens)
        # The advertised command must be the SAFE preview: the response is an
        # operator instruction for a destructive script, and the documented
        # procedure is dry-run first (batch-5 review).
        self.assertIn("--dry-run", tokens, "the hint must lead with the dry run")
        self.assertIn("without --dry-run", re.sub(r'[\'"]', "", source))
        # Feed the extracted argv through the script's REAL argument parser
        # (main()) with run_migration stubbed — tokenizing alone proved
        # nothing about the flags the script accepts.
        mod = _load_migrate_module()
        captured = {}

        def fake_run_migration(**kwargs):
            captured.update(kwargs)
            return 0

        saved_argv = sys.argv
        saved_run = mod.run_migration
        try:
            sys.argv = tokens[1:]
            mod.run_migration = fake_run_migration
            mod.main()
        finally:
            sys.argv = saved_argv
            mod.run_migration = saved_run
        self.assertTrue(captured.get("dry_run"), "parsed command must be a dry run")
        self.assertEqual(captured.get("lancedb_path"), Path("./data/lancedb"))
        self.assertEqual(captured.get("sqlite_path"), Path("./data/app.db"))

    def test_hint_lives_in_the_health_response_not_only_the_logger(self):
        """The OPERATOR-facing carrier is the /api/health JSON payload
        (stale_embeddings_detail), not the server-side log line. The
        whole-file scan above is satisfied by the logger literal alone, so
        this assertion scopes to the response-field block: the command text
        must appear BETWEEN the stale_embeddings_detail assignment and the
        following logger call (pr1081-20261106 PRR-009 — reverting only the
        response literal left the whole-file checks green)."""
        source = (BACKEND / "app" / "api" / "routes" / "health.py").read_text(
            encoding="utf-8"
        )
        response_start = source.find('vector_status["stale_embeddings_detail"] = (')
        self.assertGreater(
            response_start, -1, "response-field detail assignment must exist"
        )
        next_logger = source.find("logger.warning", response_start)
        self.assertGreater(next_logger, -1, "logger arm expected after the response")
        response_block = source[response_start:next_logger]
        self.assertIn(
            "Run: python scripts/migrate_embeddings.py",
            re.sub(r"\s+", " ", response_block),
            "the /health response field itself must carry the runnable command "
            "(not only the logger literal)",
        )
        self.assertIn(
            "--sqlite-path",
            response_block,
            "the response-field command must carry the sqlite path flag",
        )


class TestPerDocumentOwnershipSteps(unittest.TestCase):
    """Each first-run document must carry mkdir + chown (default AND custom)."""

    def _assert_doc(self, name: str, text: str):
        self.assertRegex(text, r"mkdir -p data\b", f"{name}: default mkdir")
        self.assertRegex(
            text, r"chown -R 1000:1000 data\b", f"{name}: default chown"
        )
        self.assertRegex(
            text,
            r"mkdir -p /path/to/your/data",
            f"{name}: custom-path mkdir example",
        )
        self.assertRegex(
            text,
            r"chown -R 1000:1000 /path/to/your/data",
            f"{name}: custom-path chown example",
        )

    def test_readme_has_both_forms(self):
        self._assert_doc(
            "README", (REPO / "README.md").read_text(encoding="utf-8")
        )

    def test_installation_has_both_forms(self):
        self._assert_doc(
            "INSTALLATION", (REPO / "INSTALLATION.md").read_text(encoding="utf-8")
        )


class TestCpuOverrideStructure(unittest.TestCase):
    """The CPU override must clear inherited GPU devices under merge."""

    def test_override_pins_cpu_images_and_reset_devices(self):
        import yaml

        text = (REPO / "docker-compose.cpu.yml").read_text(encoding="utf-8")
        self.assertIn("devices: !reset []", text)
        # `!reset` is a compose custom tag — parse with a tolerant SUBCLASS
        # (never mutate the global SafeLoader other tests rely on).
        class _ResetLoader(yaml.SafeLoader):
            pass

        _ResetLoader.add_constructor("!reset", lambda loader, node: {"__reset__": True})
        parsed = yaml.load(text, Loader=_ResetLoader)
        for service in ("harrier-embed", "reranker"):
            svc = parsed["services"][service]
            self.assertIn("cpu-latest", svc["image"], f"{service} swaps image")
            devices = (
                svc.get("deploy", {})
                .get("resources", {})
                .get("reservations", {})
                .get("devices")
            )
            self.assertEqual(
                devices,
                {"__reset__": True},
                f"{service} must reset inherited devices (plain [] RETAINS them)",
            )
        # The base file is what supplies the healthchecks the override keeps.
        base = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
        for service in ("harrier-embed", "reranker"):
            self.assertIn("healthcheck", base["services"][service])

    def test_readme_documents_the_two_file_invocation(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d",
            text,
        )

    def test_merged_compose_has_no_gpu_devices(self):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("docker CLI unavailable (text pins cover the override)")
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker-compose.cpu.yml",
                "config",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0, f"compose merge failed: {proc.stderr[-400:]}"
        )
        import yaml

        merged = yaml.safe_load(proc.stdout)
        for service in ("harrier-embed", "reranker"):
            svc = merged["services"][service]
            self.assertIn("cpu-latest", svc["image"], f"{service} cpu image")
            reservations = (
                svc.get("deploy", {}).get("resources", {}).get("reservations", {})
            )
            self.assertEqual(
                reservations.get("devices", []),
                [],
                f"{service} merged reservations must have no devices",
            )
            self.assertIn("healthcheck", svc, f"{service} keeps its healthcheck")


if __name__ == "__main__":
    unittest.main()
