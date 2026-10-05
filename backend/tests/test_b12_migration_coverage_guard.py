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
guard counts.

Amended (CHECK_WRONG, PR #856 feedback round): the original substring scan
counted a file that merely MENTIONED the migration's name (a comment-only
file satisfied it) while its failure message claimed the guard proved the
swap "cannot silently regress" — more than a text scan can prove. The
guard now parses each candidate module and counts it only when it actually
CALLS the migration (an AST Call node, name or attribute form), and the
message below states exactly what the guard does and does not verify.
"""
import ast
from pathlib import Path

MIGRATION = "migrate_relax_draft_claims_span_not_null"


def _calls_migration(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == MIGRATION)
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == MIGRATION
            )
        )
        for node in ast.walk(tree)
    )


def test_relax_draft_claims_legacy_rebuild_is_tested():
    tests_dir = Path(__file__).resolve().parent
    counted = []
    for candidate in sorted(tests_dir.rglob("*.py")):
        if candidate.name.startswith("test_b12_"):
            # This slot's own check files must not satisfy the guard.
            continue
        source = candidate.read_text(encoding="utf-8", errors="replace")
        if "draft_claims_old" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        if _calls_migration(tree):
            counted.append(candidate.name)
    assert counted, (
        "no test module outside test_b12_* both exercises the "
        "draft_claims_old legacy shape and actually calls "
        "migrate_relax_draft_claims_span_not_null. This guard verifies only "
        "that such a call site exists in CI; the depth of that coverage "
        "(which recovery branches, which shapes) is pinned by the coverage "
        "tests themselves, not here"
    )
