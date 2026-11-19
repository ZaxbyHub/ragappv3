"""Acceptance checks AC4-AC6 (issues #946 / #900 items 1-2, collated trace
1171-900-946-947-residuals-hygiene-sweep): c04 fixture teardown must be
reachable when SETUP fails, not only after a successful yield.

Root cause (04-root-cause.md RC2): the A/B ``fresh_db`` fixtures bind
``conn`` inside the ``try`` (a ``run_migrations`` failure makes the
``finally`` raise UnboundLocalError and skip ``os.unlink``), and the
``seeded_env`` fixtures perform setup with cleanup only after ``yield``
(a pre-yield raise exits the fixture without any teardown).

Method: drive the REAL fixture generator functions directly with a forced
``run_migrations`` failure (the module-level name each fixture module
imported is clobbered wherever it is bound), then assert (a) the ORIGINAL
RuntimeError is the reported error and (b) no temp .db / temp dir remains
(snapshot ``tempfile.gettempdir()`` before/after). The fixture is located
via the test module first and, once AC6's dedup moves it into a shared
home (conftest or a helper module — never another test module), via that
home, so this check survives the dedup refactor.

Frozen Phase 2.5 checks (arm's-length author; no fix code existed when
these were written):

- AC4 — A/B ``fresh_db`` setup failure: original error + no temp DB.
  RED at base (UnboundLocalError masks the error, temp DB left behind),
  GREEN after the fix. ``test_c03b_prompt_resolution_edges`` is the
  already-guarded control param (GREEN at base — it proves the probe
  passes on a correct fixture, so the RED params are real).
- AC5 — ``seeded_env`` setup failure: original error + no temp dir.
  RED at base (temp dir left behind), GREEN after the fix. Covers the
  frozen module test_c04_prompt_created_by.py (teardown-guard-only edit)
  and the non-frozen test_c04_prompt_warning_contract.py.
- AC6 — the duplicated fixtures no longer have local definitions in the
  non-frozen c04 modules (dedup into one shared place).
  RED at base, GREEN after the fix.

Standalone twin: .agents/issue-traces/1171-900-946-947-residuals-
hygiene-sweep/repro/check_fixture_teardown.py drives the A/B shape
without pytest for cheap replay.
"""

import glob
import importlib
import inspect
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

PROBE_MARKER = "forced-setup-failure-946"

# A/B fresh_db shape (AC4). The first three carry the defect the issues
# name; the next four are the plan-critic round-1 sweep sites (same class,
# executed-reproduced by the critic and folded into this PR's scope); the
# c03b sibling is the already-guarded control (proves the probe passes on
# a correct fixture).
AB_FRESH_DB_MODULES = [
    "test_c04_ab_exposure_failure",
    "test_c04_ab_lookup_failure",
    "test_prompt_ab_variants",
    # Plan-critic round-1 additions (executed siblings of the same class):
    "test_prompt_versioning",
    "test_c03_orgless_prompt",
    "test_prompt_org_overrides",
    # Already-guarded control:
    "test_c03b_prompt_resolution_edges",
]

# mkdtemp seeded_env shape (AC5): non-frozen module + frozen module.
SEEDED_ENV_MODULES = [
    "test_c04_prompt_warning_contract",
    "test_c04_prompt_created_by",
]

# AC6: (module, fixture names it must no longer define locally).
# test_c04_prompt_created_by is FROZEN (checkpoint-pinned by the #709
# trace) and is deliberately absent — it keeps its own defs and takes a
# teardown-guard-only edit. test_c04_review_hardening is a #900 AC2-named
# non-frozen module (plan-critic round-1 catch: it was missing from the
# original census).
NONFROZEN_DUPLICATED_DEFS = [
    ("test_c04_prompt_warning_contract", ("seeded_env", "prompts_client")),
    ("test_c04_review_hardening", ("seeded_env", "prompts_client")),
    ("test_c04_ab_exposure_failure", ("fresh_db",)),
    ("test_c04_ab_lookup_failure", ("fresh_db",)),
    ("test_prompt_ab_variants", ("fresh_db",)),
]

_TESTS_DIR = Path(__file__).resolve().parent


def _snapshot_db_files() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "*.db")))


def _snapshot_c04_dirs() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "c04_*")))


@contextmanager
def _forced_run_migrations_failure():
    """Clobber run_migrations with a raising probe EVERYWHERE it is bound
    (app.models.database itself plus every module holding the original
    function object), restoring all bindings afterwards."""
    from app.models import database as db_mod

    original = db_mod.run_migrations

    def boom(*args, **kwargs):
        raise RuntimeError(PROBE_MARKER)

    patched = []
    try:
        db_mod.run_migrations = boom
        patched.append((db_mod, original))
        for module in list(sys.modules.values()):
            if getattr(module, "run_migrations", None) is original:
                module.run_migrations = boom
                patched.append((module, original))
        yield
    finally:
        for module, old in patched:
            module.run_migrations = old


@contextmanager
def _forced_sqlite_connect_failure():
    """Clobber sqlite3.connect with a raising probe (plan-critic round 1:
    AC4 names BOTH the run_migrations and the sqlite3.connect failure
    legs — a guard that only survives the first is half-fixed).

    Plan-critic round 2: sqlite3.connect is ALSO used inside run_migrations
    (its internal connection carries kwargs like timeout=), so this leg
    no-ops run_migrations everywhere it is bound — the fixture's own bare
    ``sqlite3.connect(path)`` call is then the ONLY failure point. The probe
    records every call's args/kwargs so the drive can assert the fixture's
    positional-only connect was actually reached."""
    import sqlite3 as sqlite3_mod

    from app.models import database as db_mod

    connect_original = sqlite3_mod.connect
    migrations_original = db_mod.run_migrations
    calls: list = []

    def connect_boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError(PROBE_MARKER)

    def migrations_noop(*args, **kwargs):
        return None

    patched = [(db_mod, "run_migrations", migrations_original, migrations_noop)]
    try:
        for module in list(sys.modules.values()):
            if getattr(module, "run_migrations", None) is migrations_original:
                setattr(module, "run_migrations", migrations_noop)
                patched.append((module, "run_migrations", migrations_original, migrations_noop))
        sqlite3_mod.connect = connect_boom
        yield calls
    finally:
        sqlite3_mod.connect = connect_original
        for module, name, old, _new in patched:
            setattr(module, name, old)


@contextmanager
def _forced_pool_cache_failure():
    """Clobber app.models.database._pool_cache so the seeded_env setup's
    pool-cache drain raises BEFORE run_migrations (plan-critic round 1: AC5
    must exercise pre-migration setup too — the try/finally must wrap from
    mkdtemp, not from run_migrations).

    Plan-critic round 2: the probe delegates to the ORIGINAL dict for every
    operation except the FIRST items() call, so fixture teardown's normal
    pool-cache cleanup still works — the single injected failure is the
    realistic semantics, and any cleanup crash is the fixture's own defect,
    not the probe's."""
    from app.models import database as db_mod

    original = db_mod._pool_cache
    state = {"failed": False}

    class _Boom:
        def items(self):
            if not state["failed"]:
                state["failed"] = True
                raise RuntimeError(PROBE_MARKER)
            return original.items()

        def __contains__(self, key):
            return key in original

        def __iter__(self):
            return iter(original)

        def __getitem__(self, key):
            return original[key]

        def __delitem__(self, key):
            del original[key]

    try:
        db_mod._pool_cache = _Boom()
        yield
    finally:
        db_mod._pool_cache = original


def _unwrap_fixture(fn):
    """A @pytest.fixture-decorated function refuses direct calls; the raw
    generator lives in __wrapped__ (pytest FixtureFunctionDefinition)."""
    return getattr(fn, "__wrapped__", fn)


def _resolve_fixture(test_module_name: str, fixture_name: str):
    """Locate the REAL fixture generator for a test module: its own
    namespace first, then — once AC6's dedup lands — a shared home under
    backend/tests (conftest or a helper module; never another test
    module, so a frozen module keeping its own def cannot shadow the
    shared one)."""
    mod = importlib.import_module(test_module_name)
    fn = getattr(mod, fixture_name, None)
    if fn is not None:
        return _unwrap_fixture(fn)
    candidates = []
    for module in list(sys.modules.values()):
        cand = getattr(module, fixture_name, None)
        if not callable(cand):
            continue
        mod_name = getattr(module, "__name__", "")
        mod_file = getattr(module, "__file__", "") or ""
        if not mod_file:
            continue
        if _TESTS_DIR not in Path(mod_file).resolve().parents:
            continue
        if mod_name.startswith("test_"):
            continue
        candidates.append((mod_name, _unwrap_fixture(cand)))
    homes = {name for name, _ in candidates}
    if len(homes) != 1:
        pytest.fail(
            f"fixture {fixture_name!r} consumed by {test_module_name} has "
            f"no single shared home under backend/tests (found: {sorted(homes)})"
        )
    return candidates[0][1]


def _drive_setup_failure(
    fixture, monkeypatch, ac_prefix: str, module_name: str, failure_ctx=None
):
    """Run the fixture generator to (and past) its forced setup failure,
    requiring the ORIGINAL probe error to be the reported error. The
    failure context defaults to the run_migrations clobber; plan-critic
    round 1 added the sqlite3.connect and pre-migration pool-cache legs."""
    kwargs = {}
    for param_name in inspect.signature(fixture).parameters:
        if param_name == "monkeypatch":
            kwargs["monkeypatch"] = monkeypatch
    with (failure_ctx or _forced_run_migrations_failure()):
        gen = fixture(**kwargs)
        try:
            next(gen)
        except RuntimeError as exc:
            if PROBE_MARKER not in str(exc):
                pytest.fail(
                    f"{ac_prefix}-ORIGINAL-ERROR-MASKED: {module_name} "
                    f"reported an unexpected RuntimeError without the probe "
                    f"marker: {exc}"
                )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"{ac_prefix}-ORIGINAL-ERROR-MASKED: {module_name} setup "
                f"error was masked by {type(exc).__name__}: {exc}"
            )
        else:
            gen.close()
            pytest.fail(
                f"{ac_prefix}-NO-SETUP-FAILURE: {module_name} setup "
                f"unexpectedly survived the forced run_migrations failure"
            )


# ---------------------------------------------------------------------------
# AC4 — A/B fresh_db: setup failure must clean up the temp DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", AB_FRESH_DB_MODULES)
def test_ab_fresh_db_setup_failure_cleans_up(module_name, monkeypatch):
    """AC4 (#946): a run_migrations failure during the A/B fresh_db
    fixture's setup must surface the ORIGINAL error and remove the temp
    database. RED at base, GREEN after the fix."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    before = _snapshot_db_files()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture, monkeypatch, "AC4", f"{module_name}.fresh_db"
        )
        leftovers = sorted(_snapshot_db_files() - before)
        assert leftovers == [], (
            f"AC4-LEFTOVER-DB: {module_name}.fresh_db left temp DB file(s) "
            f"behind on setup failure: {leftovers}"
        )
    finally:
        # Best-effort scrub of anything the (unfixed) fixture leaked, so a
        # RED base run does not snowball temp files across replays.
        for path in leftovers or (_snapshot_db_files() - before):
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# AC5 — mkdtemp seeded_env: setup failure must remove the temp dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", SEEDED_ENV_MODULES)
def test_seeded_env_setup_failure_cleans_up(module_name, monkeypatch):
    """AC5 (#946/#900): a setup failure in a mkdtemp seeded_env fixture
    (before yield) must surface the ORIGINAL error and remove the temp
    directory. RED at base, GREEN after the fix."""
    fixture = _resolve_fixture(module_name, "seeded_env")
    before = _snapshot_c04_dirs()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture, monkeypatch, "AC5", f"{module_name}.seeded_env"
        )
        leftovers = sorted(_snapshot_c04_dirs() - before)
        assert leftovers == [], (
            f"AC5-LEFTOVER-DIR: {module_name}.seeded_env left temp "
            f"dir(s) behind on setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_c04_dirs() - before):
            shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC4/AC5 amendment legs (plan-critic round 1): the OTHER failure points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        "test_c04_ab_exposure_failure",
        "test_c04_ab_lookup_failure",
        "test_prompt_ab_variants",
    ],
)
def test_ab_fresh_db_connect_failure_cleans_up(module_name, monkeypatch):
    """AC4 amendment (#946): AC1's issue text names BOTH the
    run_migrations and the sqlite3.connect failure legs; a conn=None guard
    that only survives the first is half-fixed. Plan-critic round 2:
    run_migrations is no-op'd during this leg so the fixture's OWN bare
    sqlite3.connect(path) call is the failure point (the recorded calls
    must include a positional-only connect — the migration path carries a
    timeout kwarg). RED at base, GREEN after the fix."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    before = _snapshot_db_files()
    leftovers = []
    try:
        with _forced_sqlite_connect_failure() as calls:
            gen = fixture()
            try:
                next(gen)
            except RuntimeError as exc:
                assert PROBE_MARKER in str(exc), (
                    f"AC4-ORIGINAL-ERROR-MASKED: {module_name}.fresh_db "
                    f"reported an unexpected error on connect failure: {exc}"
                )
            else:
                gen.close()
                pytest.fail(
                    f"AC4-NO-SETUP-FAILURE: {module_name}.fresh_db survived "
                    f"the forced sqlite3.connect failure"
                )
        positional = [c for c in calls if not c[1]]
        assert positional, (
            "AC4-CONNECT-NOT-REACHED: the fixture's own positional "
            "sqlite3.connect(path) was never reached (the failure fired "
            f"elsewhere): calls={calls}"
        )
        leftovers = sorted(_snapshot_db_files() - before)
        assert leftovers == [], (
            f"AC4-LEFTOVER-DB: {module_name}.fresh_db left temp DB file(s) "
            f"behind on sqlite3.connect failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_db_files() - before):
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.mark.parametrize(
    "module_name",
    ["test_c04_prompt_warning_contract", "test_c04_prompt_created_by"],
)
def test_seeded_env_pre_migration_failure_cleans_up(module_name, monkeypatch):
    """AC5 amendment (#946/#900): a failure DURING the pre-migration setup
    (the pool-cache drain runs after mkdtemp and before run_migrations)
    must also remove the temp dir — the try/finally must wrap from
    mkdtemp, not from run_migrations. Plan-critic round 2: parametrized
    over the frozen created_by module too (it is edited independently).
    RED at base, GREEN after the fix."""
    fixture = _resolve_fixture(module_name, "seeded_env")
    before = _snapshot_c04_dirs()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture,
            monkeypatch,
            "AC5",
            f"{module_name}.seeded_env",
            failure_ctx=_forced_pool_cache_failure(),
        )
        leftovers = sorted(_snapshot_c04_dirs() - before)
        assert leftovers == [], (
            f"AC5-LEFTOVER-DIR: {module_name}.seeded_env left temp dir(s) "
            f"behind on pre-migration setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_c04_dirs() - before):
            shutil.rmtree(path, ignore_errors=True)


@pytest.mark.parametrize(
    "module_name",
    [
        "test_prompt_versioning",
        "test_c03_orgless_prompt",
        "test_prompt_org_overrides",
    ],
)
def test_sweep_site_fresh_db_setup_failure_cleans_up(module_name, monkeypatch):
    """Plan-critic round-1 sweep sites (#946 class): the three additional
    modules with conn-inside-try fresh_db fixtures get the same behavioral
    original-error + no-leftover proof as the issue-named files (the C4
    main param already drives them; this leg exists so every fixed site
    has a named, greppable probe). RED at base, GREEN after the fix."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    before = _snapshot_db_files()
    leftovers = []
    try:
        _drive_setup_failure(fixture, monkeypatch, "AC4", f"{module_name}.fresh_db")
        leftovers = sorted(_snapshot_db_files() - before)
        assert leftovers == [], (
            f"AC4-LEFTOVER-DB: {module_name}.fresh_db left temp DB file(s) "
            f"behind on setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_db_files() - before):
            try:
                os.unlink(path)
            except OSError:
                pass


def test_multi_org_db_class_fixture_setup_failure_cleans_up(monkeypatch):
    """Plan-critic round 2 (#946 class): the CLASS-scoped multi_org_db
    fixture in test_prompt_org_overrides (TestMultiOrgPerQueryResolution)
    is the second defect site in that module — the same guarded-teardown
    contract applies. RED at base, GREEN after the fix.

    Plan-critic round 3: the unwrapped class-attribute function still
    requires ``self``; bind it to an instance via types.MethodType before
    driving (the production fixture signature is NOT altered)."""
    import types

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestMultiOrgPerQueryResolution")
    fixture = types.MethodType(_unwrap_fixture(holder.multi_org_db), holder())
    before = _snapshot_db_files()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture, monkeypatch, "AC4", "test_prompt_org_overrides.multi_org_db"
        )
        leftovers = sorted(_snapshot_db_files() - before)
        assert leftovers == [], (
            f"AC4-LEFTOVER-DB: multi_org_db left temp DB file(s) behind on "
            f"setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_db_files() - before):
            try:
                os.unlink(path)
            except OSError:
                pass


def _snapshot_issue645_dirs() -> set:
    return set(
        glob.glob(os.path.join(tempfile.gettempdir(), "issue645_routes_*"))
    )


def _snapshot_tmp_artifacts() -> set:
    """setup_db uses a bare tempfile.mkdtemp() (no prefix) — snapshot the
    generic tmp* namespace around its probe."""
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "tmp*")))


def test_org_overrides_setup_db_failure_cleans_up(monkeypatch):
    """Implementation-review round 5 (#946 class): the autouse class fixture
    setup_db in test_prompt_org_overrides (TestPromptOverrideAPIFixtures) is
    the module's third mkdtemp site — the same original-error + no-leftover
    contract applies to the migration leg (round-6 reviewer question: the
    SEED phase — a raise after sqlite3.connect — gets its own leg below,
    driving hash_password to fail so the nested conn.close() must run before
    the rmtree). RED at the pre-fix tree, GREEN after the fix.
    """
    import types

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestPromptOverrideAPIFixtures")
    fixture = types.MethodType(_unwrap_fixture(holder.setup_db), holder())

    def _snap() -> set:
        return _snapshot_c04_dirs() | _snapshot_tmp_artifacts()

    before = _snap()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture,
            monkeypatch,
            "AC5",
            "test_prompt_org_overrides.setup_db",
        )
        leftovers = sorted(_snap() - before)
        assert leftovers == [], (
            f"AC5-LEFTOVER-DIR: setup_db left temp dir(s)/file(s) behind on "
            f"setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snap() - before):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def test_org_overrides_setup_db_seed_failure_closes_conn(monkeypatch):
    """Round-6 reviewer question (#946 class): the SEED phase of the same
    setup_db fixture — hash_password raises AFTER sqlite3.connect, so the
    nested conn.close() in the finally must run before the outer rmtree
    (an open handle can block directory removal on Windows and
    ignore_errors would hide it). RED if the nested close is deleted,
    GREEN with it.
    """
    import types
    from contextlib import contextmanager

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestPromptOverrideAPIFixtures")
    fixture = types.MethodType(_unwrap_fixture(holder.setup_db), holder())

    @contextmanager
    def _forced_hash_failure():
        original = module.hash_password

        def boom(*args, **kwargs):
            raise RuntimeError(PROBE_MARKER)

        try:
            module.hash_password = boom
            yield
        finally:
            module.hash_password = original

    def _snap() -> set:
        return _snapshot_c04_dirs() | _snapshot_tmp_artifacts()

    before = _snap()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture,
            monkeypatch,
            "AC5",
            "test_prompt_org_overrides.setup_db(seed)",
            failure_ctx=_forced_hash_failure(),
        )
        leftovers = sorted(_snap() - before)
        assert leftovers == [], (
            f"AC5-LEFTOVER-DIR: setup_db left temp dir(s)/file(s) behind on "
            f"seed failure (conn not closed before rmtree): {leftovers}"
        )
    finally:
        for path in leftovers or (_snap() - before):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def test_issue645_mkddir_fixture_setup_failure_cleans_up(monkeypatch):
    """Plan-critic round 2 (#946 class): the mkdtemp harness ANCESTOR in
    test_issue645_converted_route_http.py has the same pre-yield exposure
    (its temp prefix is issue645_routes_*, outside the c04_* glob, so it
    gets its own snapshot). RED at base, GREEN after the fix."""
    module = importlib.import_module("test_issue645_converted_route_http")
    fixture = _unwrap_fixture(module.seeded_env)
    before = _snapshot_issue645_dirs()
    leftovers = []
    try:
        _drive_setup_failure(
            fixture,
            monkeypatch,
            "AC5",
            "test_issue645_converted_route_http.seeded_env",
        )
        leftovers = sorted(_snapshot_issue645_dirs() - before)
        assert leftovers == [], (
            f"AC5-LEFTOVER-DIR: issue645 seeded_env left temp dir(s) behind "
            f"on setup failure: {leftovers}"
        )
    finally:
        for path in leftovers or (_snapshot_issue645_dirs() - before):
            shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6 — the duplicated fixtures live in ONE shared place
# ---------------------------------------------------------------------------


def test_nonfrozen_c04_modules_do_not_define_duplicated_fixtures():
    """AC6 (#946/#900): the non-frozen c04 modules must CONSUME the
    shared fixtures, not define their own copies (the duplication is the
    amplifying condition of RC2), and the shared home must still exist.
    RED at base, GREEN after the fix."""
    offenders = []
    for module_name, fixture_names in NONFROZEN_DUPLICATED_DEFS:
        source = inspect.getsource(importlib.import_module(module_name))
        for fixture_name in fixture_names:
            if f"def {fixture_name}(" in source:
                offenders.append(f"{module_name}:def {fixture_name}")
    assert offenders == [], (
        f"AC6-DUPLICATE-FIXTURE-DEF: non-frozen c04 modules still define "
        f"their own copies of the shared fixtures (dedup not landed): "
        f"{offenders}"
    )
    holders = [
        p.name
        for p in _TESTS_DIR.rglob("*.py")
        if "def seeded_env(" in p.read_text(encoding="utf-8")
    ]
    assert holders, (
        "AC6-SHARED-HOME-MISSING: no module under backend/tests defines "
        "seeded_env any more (the fixture was deleted rather than shared)"
    )
    # Plan-critic round 1: the shared home must be c04_fixtures.py
    # SPECIFICALLY (the frozen created_by module also defines seeded_env,
    # so the holders check alone cannot prove the shared home exists), it
    # must provide all THREE shared fixtures, and the converted modules'
    # imported objects must ORIGINATE there (an import from anywhere else
    # would satisfy the no-local-def census without a single home).
    import c04_fixtures as shared_home

    for fixture_name in ("fresh_db", "seeded_env", "prompts_client"):
        assert hasattr(shared_home, fixture_name), (
            f"AC6-SHARED-HOME-MISSING: backend/tests/c04_fixtures.py does "
            f"not provide {fixture_name}"
        )
    consumers = [
        "test_c04_prompt_warning_contract",
        "test_c04_review_hardening",
        "test_c04_ab_exposure_failure",
        "test_c04_ab_lookup_failure",
        "test_prompt_ab_variants",
    ]
    for module_name in consumers:
        mod = importlib.import_module(module_name)
        for fixture_name in ("fresh_db", "seeded_env", "prompts_client"):
            imported = getattr(mod, fixture_name, None)
            if imported is None:
                continue  # not every module consumes every fixture
            origin = getattr(
                getattr(imported, "__wrapped__", imported), "__module__", ""
            )
            assert origin == "c04_fixtures", (
                f"AC6-SHARED-HOME-BYPASS: {module_name}.{fixture_name} "
                f"resolves from {origin!r}, not the shared home "
                f"c04_fixtures (an import from anywhere else is not a "
                f"dedup)"
            )
