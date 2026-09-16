"""Issue #559 cross-stage acceptance check C9 (DISCRIMINATING + NEW-SURFACE):
per-queue lease enablement switches and the rollback/rollout operator surface.

The issue mandates per-queue rollback: "keep the old ... claim code paths
available behind the migration ... Rollback: keep the old in-memory-queue and
SELECT-then-UPDATE claim code paths available behind the migration until each
staged queue's new path has run in production, then remove the old path for
that queue only." This check pins both halves of that surface:

- the three per-queue lease enablement switches exist (wiki/KMS one switch,
  reindex, draft), default enabled, and select the code path at process
  start: with a switch disabled the legacy claim still serves a pending row
  (NEW-SURFACE: the settings fields cannot exist at base);
- the documented surface exists: ``.env.example`` entries,
  ``docker-compose.yml`` forwarding, and a pending release note naming the
  switches and the legacy-path rollback (DISCRIMINATING: the names appear in
  none of those files at base).

Expectation: RED at the base. GREEN post-fix.
"""

import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from app.config import Settings, settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.draft_store import DraftStore
from app.services.kms_store import KMSStore
from app.services.wiki_store import WikiStore

REPO_ROOT = Path(__file__).resolve().parents[2]

SWITCH_ENV_NAMES = (
    "WIKI_KMS_JOB_LEASE_ENABLED",
    "REINDEX_JOB_LEASE_ENABLED",
    "DRAFT_JOB_LEASE_ENABLED",
)

PER_QUEUE_LEASE_SWITCHES = (
    "wiki_kms_job_lease_enabled",
    "reindex_job_lease_enabled",
    "draft_job_lease_enabled",
)


@contextmanager
def _setting_overridden(name, value):
    """Toggle a settings field; a no-op while the field does not exist.

    pydantic models reject setattr of unknown fields, so at the pre-change
    base (fields absent) this yields without touching anything — the
    surrounding check then fails for its own behavioral reason.
    """
    sentinel = object()
    previous = getattr(settings, name, sentinel)
    if previous is sentinel:
        yield
        return
    setattr(settings, name, value)
    try:
        yield
    finally:
        setattr(settings, name, previous)


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_db(prefix):
    db_path = str(Path(mkdtemp(prefix=prefix)) / "s234.db")
    run_migrations(db_path)
    conn = _connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 's234-vault')"
    )
    conn.commit()
    return db_path, conn


def _insert_legacy_job(conn, table, status):
    cur = conn.execute(
        f"INSERT INTO {table} (vault_id, trigger_type, status) "  # nosec B608
        "VALUES (1, 'manual', ?)",
        (status,),
    )
    conn.commit()
    return int(cur.lastrowid)


class TestLeaseEnablementFlags(unittest.TestCase):
    # check: C9 (NEW-SURFACE) — the three per-queue lease switches exist and
    # default to enabled (code-path selection happens at process start).

    def test_three_per_queue_switches_exist_and_default_enabled(self):
        built = Settings()
        for name in PER_QUEUE_LEASE_SWITCHES:
            self.assertIs(
                getattr(built, name, None),
                True,
                f"settings.{name} must exist and default to enabled",
            )


class TestLegacyPathWithSwitchDisabled(unittest.IsolatedAsyncioTestCase):
    # check: C9 (NEW-SURFACE) — with a per-queue switch disabled, the legacy
    # path still functions for that queue (the legacy claim still serves a
    # pending row) — the issue's per-queue rollback requirement.

    def _require_flag(self, name):
        self.assertTrue(
            hasattr(settings, name),
            f"settings.{name} must exist (per-queue lease enablement switch)",
        )

    async def test_disabled_reindex_switch_keeps_legacy_claim_serving(self):
        self._require_flag("reindex_job_lease_enabled")
        db_path, conn = _make_db("s234-c9-reindex-")
        self.addCleanup(conn.close)
        pool = SQLiteConnectionPool(db_path, max_size=2)
        self.addCleanup(pool.close_all)
        cur = conn.execute(
            "INSERT INTO document_reindex_jobs (vault_id, trigger_type, "
            "status, input_json) VALUES (NULL, 'api', 'pending', '{}')"
        )
        conn.commit()
        job_id = int(cur.lastrowid)

        with _setting_overridden("reindex_job_lease_enabled", False):
            processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
            # The legacy claim path: _process_reindex_job's own pending->
            # running transition, then a zero-file completion.
            await processor._process_reindex_job(job_id)  # noqa: SLF001

        status = conn.execute(
            "SELECT status FROM document_reindex_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]
        self.assertEqual(
            status,
            "completed",
            "with the switch disabled the legacy reindex claim must still "
            "serve a pending row (zero files -> completed)",
        )

    async def test_disabled_draft_switch_keeps_legacy_store_claim_serving(self):
        self._require_flag("draft_job_lease_enabled")
        temp = Path(mkdtemp(prefix="s234-c9-draft-"))
        db_path = str(temp / "draft.db")
        init_db(db_path)
        run_migrations(db_path)
        conn = _connect(db_path)
        self.addCleanup(conn.close)
        conn.executescript(
            """
            INSERT OR IGNORE INTO users (id, username, hashed_password)
                VALUES (1, 'u', 'x');
            INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'v');
            INSERT OR IGNORE INTO drafts (id, vault_id, created_by, title, mode)
                VALUES (1, 1, 1, 'd', 'rewrite');
            INSERT INTO draft_inputs (draft_id, role, authority, original_name,
                stored_name, extension, media_type, size_bytes, content_sha256,
                storage_relpath)
                VALUES (1, 'reference', 'unknown', 'a.txt', 'a.txt', '.txt',
                'text/plain', 5, 'sha-c9', 'inputs/a.txt');
            """
        )
        conn.commit()
        job = DraftStore(conn).enqueue_parse_job(
            draft_id=1, owner_id=1, input_id=1, timeout_seconds=60
        )

        with _setting_overridden("draft_job_lease_enabled", False):
            claimed = DraftStore(conn).claim_next_parse_job()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.status, "running")

    async def test_disabled_wiki_kms_switch_keeps_legacy_claims_serving(self):
        self._require_flag("wiki_kms_job_lease_enabled")
        db_path, conn = _make_db("s234-c9-wikikms-")
        self.addCleanup(conn.close)
        wiki_id = _insert_legacy_job(conn, "wiki_compile_jobs", "pending")
        kms_id = _insert_legacy_job(conn, "kms_compile_jobs", "pending")

        with _setting_overridden("wiki_kms_job_lease_enabled", False):
            wiki_job = WikiStore(conn).claim_next_pending_job()
            kms_job = KMSStore(conn).claim_next_pending_job()

        self.assertIsNotNone(wiki_job)
        self.assertEqual(wiki_job.id, wiki_id)
        self.assertEqual(wiki_job.status, "running")
        self.assertIsNotNone(kms_job)
        self.assertEqual(kms_job.id, kms_id)
        self.assertEqual(kms_job.status, "running")


class TestRollbackOperatorSurface(unittest.TestCase):
    # check: C9 (DISCRIMINATING) — the documented per-queue rollback surface
    # (env example, compose forwarding, release note) exists.

    def test_env_example_documents_the_three_switches(self):
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in SWITCH_ENV_NAMES:
            self.assertIn(
                f"{name}=",
                env_example,
                f".env.example must document {name} (env-only per-queue "
                "lease switch)",
            )

    def test_docker_compose_forwards_the_three_switches(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for name in SWITCH_ENV_NAMES:
            self.assertIn(
                name,
                compose,
                f"docker-compose.yml must forward {name} to the backend "
                "service (bool fields use the short '- KEY' form)",
            )

    def test_pending_release_note_documents_rollback(self):
        pending = REPO_ROOT / "docs" / "releases" / "pending"
        self.assertTrue(
            any(p.name.startswith("559-") for p in pending.glob("559-*.md")),
            "a pending release note for the stages 2-4 lease migration "
            "must exist under docs/releases/pending/",
        )
        combined = "\n".join(
            p.read_text(encoding="utf-8") for p in pending.glob("559-*.md")
        )
        for name in SWITCH_ENV_NAMES:
            self.assertIn(
                name,
                combined,
                f"the release notes must document the {name} rollback switch",
            )
        self.assertIn(
            "rollback",
            combined.lower(),
            "the release notes must document the legacy-path rollback",
        )


if __name__ == "__main__":
    unittest.main()
