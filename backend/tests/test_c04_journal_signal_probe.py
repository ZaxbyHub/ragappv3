"""Probe (not a committed test — orchestrator verification script shape):
journal rows for the redaction migration are visible to the #699 startup
signal, and clean boots are journal-silent. Lives in tests/ only so the
conftest env bootstrap applies; excluded from collection scope checks?
No — it IS collected; keep it a real test so the probe stays honest."""
import os
import sqlite3

import pytest

from app.models.database import run_migrations
from app.models.migration_journal import latest_outcomes_with_signal

_LEGACY = "c04-signal-legacy-token"


def test_redaction_journal_rows_reach_startup_signal(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.admin_secret_token", _LEGACY)
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {_LEGACY: ["admin:config"]},
    )
    db_path = str(tmp_path / "c04_signal.db")
    run_migrations(db_path)  # clean boot

    outcomes, unresolved = latest_outcomes_with_signal(db_path)
    redact_rows = [
        r for r in outcomes if r["migration_name"] == "migrate_redact_prompt_created_by"
    ]
    # Clean boot: journal-silent no-op.
    assert redact_rows == [], f"clean boot wrote journal rows: {redact_rows}"
    assert unresolved is None

    # Real work remains -> start + succeeded rows, no attention flag.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO prompt_versions (version, content, is_active, created_by) "
        "VALUES ('probe', 'c', 0, ?)",
        (_LEGACY,),
    )
    conn.commit()
    conn.close()
    run_migrations(db_path)

    outcomes, unresolved = latest_outcomes_with_signal(db_path)
    redact_rows = [
        (r["phase"], r["outcome"])
        for r in outcomes
        if r["migration_name"] == "migrate_redact_prompt_created_by"
    ]
    assert ("start", "ok") in redact_rows
    assert ("succeeded", "ok") in redact_rows
    assert not any(p == "failed" for p, _ in redact_rows)
    assert unresolved is None

    # Failure path: the failed row must reach the startup signal.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TRIGGER fail_redact BEFORE UPDATE ON prompt_versions "
        "BEGIN SELECT RAISE(ABORT, 'forced'); END"
    )
    conn.execute(
        "INSERT INTO prompt_versions (version, content, is_active, created_by) "
        "VALUES ('probe2', 'c', 0, 'legacy-token-two')"
    )
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        run_migrations(db_path)

    outcomes, unresolved = latest_outcomes_with_signal(db_path)
    redact_rows = [
        (r["phase"], r["outcome"])
        for r in outcomes
        if r["migration_name"] == "migrate_redact_prompt_created_by"
    ]
    # The failure surfaces in the startup summary: either the recent window
    # itself carries the failed row (the lifespan consumer reads the window)
    # or, once it ages out, the unresolved-failure slot picks it up.
    assert ("failed", "error") in redact_rows or (
        unresolved is not None
        and unresolved["migration_name"] == "migrate_redact_prompt_created_by"
    ), f"failed redaction invisible to the startup signal: {redact_rows}, {unresolved}"
