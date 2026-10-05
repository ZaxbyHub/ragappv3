"""Issue #701 (Workstream B PR 12) — legacy-rebuild coverage guard.

T1-06-K2-07 / T1-06-K-03: ``migrate_relax_draft_claims_span_not_null``
renames the live table, recreates it, copies rows, and drops the backup
through four recovery branches, relying on a ``legacy_alter_table=ON``
pragma its own docstring marks CRITICAL (without it, SQLite rewrites
``draft_claim_sources.claim_id`` to point at the dropped backup table).
At least one behavioral test module outside this guard must call the
migration directly against a legacy ``draft_claims`` / ``draft_claims_old``
shape so that swap path cannot silently regress in CI.

At the issue's master re-check (af4914d6) no such test existed; PR #850
(issue #699) later added ``test_b10_journal_noise_guard.py``, which this
guard now counts. The behavioral companion pins (legacy NOT NULL shape,
child-FK target, high-water mark) live in ``test_b12_dedup_migrations.py``
and in the b10 recovery-branch tests.
"""
from pathlib import Path


def test_relax_draft_claims_legacy_rebuild_is_tested():
    tests_dir = Path(__file__).resolve().parent
    counted = []
    for candidate in sorted(tests_dir.rglob("*.py")):
        if candidate.name.startswith("test_b12_"):
            # This slot's own check files must not satisfy the guard.
            continue
        source = candidate.read_text(encoding="utf-8", errors="replace")
        if (
            "migrate_relax_draft_claims_span_not_null(" in source
            and "draft_claims_old" in source
        ):
            counted.append(candidate.name)
    assert counted, (
        "no test module outside test_b12_* calls "
        "migrate_relax_draft_claims_span_not_null( against a legacy "
        "draft_claims_old shape; the CRITICAL legacy_alter_table=ON swap "
        "has zero CI coverage"
    )
