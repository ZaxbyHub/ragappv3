"""Frozen issue-tracer checks for issue #749 (documented operator paths).

Acceptance checks C8-C13 of the #749 remediation plan:

- C8: an unopenable SQLite path (a root-owned bind mount seen by the
  uid-1000 ``appuser``) must not abort startup inside
  ``_load_persisted_settings`` — the degraded-start path stays reachable.
- C9: the first-run docs (README / INSTALLATION) must tell native-Linux
  operators to create the data directory owned by uid 1000.
- C10: ``run_migration`` pointed at a missing LanceDB path AND a missing
  SQLite path must refuse (non-zero exit / raise), not silently return
  "Nothing to migrate" against the wrong data paths.
- C11: the stale-embedding health hint must be runnable as given — either
  the image ships ``scripts/migrate_embeddings.py`` or the hint carries
  explicit ``--lancedb-path`` guidance.
- C12: the README Quick Start prerequisites must name the NVIDIA GPU the
  bundled embedding/reranker containers reserve (or a CPU alternative).
- C13: the email "today" stats must window on the UTC day that
  ``files.created_at`` is stamped in, not the host's local date.

Each check is RED at the recorded base tree and turns GREEN when the #749
fix lands. C10 deliberately leaves the #694 absent-index-next-to-a-real-DB
no-op contract untouched (pinned separately in
test_migrate_embeddings_dim.py).
"""

import ast
import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
LIFESPAN_FILE = BACKEND_DIR / "app" / "lifespan.py"
HEALTH_FILE = BACKEND_DIR / "app" / "api" / "routes" / "health.py"
README_FILE = REPO_ROOT / "README.md"
INSTALLATION_FILE = REPO_ROOT / "INSTALLATION.md"
DOCKERFILE_FILE = REPO_ROOT / "Dockerfile"

sys.path.insert(0, str(BACKEND_DIR))

# Load scripts/migrate_embeddings.py the way test_migrate_embeddings_dim.py
# does: by file path under a private module name, so the repo-root script is
# importable from the backend test suite without a package.
_MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate_embeddings.py"
_spec = importlib.util.spec_from_file_location(
    "migrate_embeddings_i02_check", _MIGRATE_SCRIPT
)
migrate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_script)


def test_persisted_settings_load_does_not_abort_startup(tmp_path):
    guarded = 0

    # Arm (a): a directory is a path sqlite3 cannot open as a database; the
    # persisted-settings load must return rather than raise out of it.
    try:
        from app.lifespan import _load_persisted_settings

        _load_persisted_settings(str(tmp_path))
        guarded = 1
    except Exception:
        guarded = 0

    if not guarded:
        # Arm (b): the startup call site sits inside a try statement that
        # has at least one except handler.
        tree = ast.parse(LIFESPAN_FILE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not node.handlers:
                continue
            for statement in node.body:
                for sub in ast.walk(statement):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_load_persisted_settings"
                    ):
                        guarded = 1
    assert guarded == 1


def test_docs_state_data_dir_ownership():
    chown_step = re.compile(r"chown\s+(-R\s+)?1000(:1000)?\b")
    count = 0
    for doc in (README_FILE, INSTALLATION_FILE):
        count += len(chown_step.findall(doc.read_text(encoding="utf-8")))
    assert count >= 1


def test_migrate_embeddings_refuses_wrong_data_paths(tmp_path):
    try:
        migrate_script.run_migration(
            tmp_path / "missing-lancedb", tmp_path / "missing.db"
        )
        outcome = "returned"
    except SystemExit as exc:
        outcome = f"exit {exc.code}" if exc.code else "returned"
    except Exception as exc:
        outcome = type(exc).__name__
    assert outcome != "returned"


def test_stale_embedding_hint_is_runnable():
    ok = 0
    for line in DOCKERFILE_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text.startswith("COPY"):
            continue
        tokens = [token for token in text.split() if not token.startswith("--")]
        sources = tokens[1:-1] if len(tokens) > 2 else tokens[1:]
        if any(
            "scripts/migrate_embeddings.py" in source or "scripts/" in source
            for source in sources
        ):
            ok = 1
    if "--lancedb-path" in HEALTH_FILE.read_text(encoding="utf-8"):
        ok = 1
    assert ok == 1


def test_readme_prerequisites_name_gpu_requirement():
    lines = README_FILE.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("### Prerequisites")),
        None,
    )
    assert start is not None, "README must keep a ### Prerequisites section"
    section_lines = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        section_lines.append(line)
    section = "\n".join(section_lines)
    count = len(re.findall(r"NVIDIA|GPU", section))
    assert count >= 1


def test_email_today_stats_use_utc_day(tmp_path, monkeypatch):
    from app.models.database import SQLiteConnectionPool, init_db

    db_path = str(tmp_path / "email_today_stats.db")
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        conn = pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (id, name) VALUES (1, 'i02-test-vault')"
            )
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name,"
                " file_size, source, created_at)"
                " VALUES (1, ?, ?, 1, 'email', ?)",
                (
                    "email/inbox/0001",
                    "message-0001.eml",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        from datetime import date as real_date

        utc_today = datetime.now(timezone.utc).date()

        class ShiftedLocalDate(real_date):
            """A host whose local calendar day is one behind UTC."""

            @classmethod
            def today(cls):
                return utc_today - timedelta(days=1)

        # email.py does `from datetime import date` inside
        # _get_today_stats, so rebinding the datetime module attribute is
        # what its call-time import resolves. monkeypatch restores it.
        monkeypatch.setattr("datetime.date", ShiftedLocalDate)

        from app.api.routes.email import _get_today_stats

        processed, _failed = _get_today_stats(pool)
        assert processed == 1
    finally:
        pool.close_all()
