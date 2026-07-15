"""Guard for issue #302's PRIMARY fix: lifespan must seed the application DB
pool BEFORE migrate_uploads runs, so the get_pool() first-call-wins singleton
cannot be silently cached at the default size (5) by _lookup_vault_id.

This test follows the repo's established source-text-inspection precedent
(test_issue_263_eviction_off_hot_path.py, test_lifespan_model_assertion.py)
for guarding lifespan wiring/ordering that is invisible to behavior tests.
test_db_pool_seeding_order.py exercises the get_pool/migrate_uploads calls in
isolation but does NOT run lifespan, so reverting lifespan.py to the buggy
order would leave that test green — this test closes that hole.
"""
import os
from pathlib import Path


def _lifespan_source() -> str:
    lifespan_path = Path(__file__).resolve().parent.parent / "app" / "lifespan.py"
    return lifespan_path.read_text()


def test_lifespan_seeds_pool_before_migrate_uploads():
    """The pool-sizing call (get_pool with settings.db_pool_max_size) must
    appear textually BEFORE the migrate_uploads call in lifespan startup.

    get_pool is a first-call-wins singleton keyed on the sqlite_path. If
    migrate_uploads (whose _lookup_vault_id helper calls get_pool) runs first
    AND a legacy uploads file exists, the singleton caches at the default 5 and
    the later max_size=db_pool_max_size is silently ignored (#302). Ordering is
    the primary fix; this guard prevents a silent reorder regression.
    """
    source = _lifespan_source()

    pool_idx = source.find("app.state.db_pool = get_pool(")
    migrate_idx = source.find("migrate_uploads")

    assert pool_idx != -1, (
        "lifespan.py must seed app.state.db_pool via get_pool(...) at startup. "
        "Expected `app.state.db_pool = get_pool(` not found."
    )
    # migrate_uploads appears in the import line AND the call; find the call
    # (the one inside asyncio.to_thread / wait_for, after the seeding).
    migrate_call_idx = source.find("migrate_uploads", pool_idx)
    assert migrate_call_idx != -1, (
        "lifespan.py must call migrate_uploads at startup (after pool seeding)."
    )
    assert pool_idx < migrate_call_idx, (
        f"lifespan.py must seed the DB pool (get_pool at char {pool_idx}) BEFORE "
        f"calling migrate_uploads (at char {migrate_call_idx}). Reverting this "
        f"ordering re-opens #302: get_pool is a first-call-wins singleton, so an "
        f"early _lookup_vault_id caller would cache the pool at the default 5."
    )


def test_lifespan_pool_seeding_reads_config():
    """The pool-sizing call must read settings.db_pool_max_size (not a hardcoded
    literal), so the config knob is actually wired and cannot become dead."""
    source = _lifespan_source()
    pool_call_idx = source.find("app.state.db_pool = get_pool(")
    assert pool_call_idx != -1, "pool-sizing line not found"
    # The call spans multiple lines; grab a window covering the full statement
    # (get_pool(sqlite_path, max_size=...)) — more than enough for both args.
    call_text = source[pool_call_idx : pool_call_idx + 300]
    assert "settings.db_pool_max_size" in call_text, (
        "lifespan pool-sizing must pass max_size=settings.db_pool_max_size so "
        "the config is honored (not a hardcoded literal)."
    )
