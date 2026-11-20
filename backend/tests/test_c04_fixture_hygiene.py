"""Acceptance checks AC4-AC6 (issues #946 / #900 items 1-2, collated trace
1171-900-946-947-residuals-hygiene-sweep): c04 fixture teardown must be
reachable when SETUP fails, not only after a successful yield.

Root cause (04-root-cause.md RC2): the A/B ``fresh_db`` fixtures bind
``conn`` inside the ``try`` (a ``run_migrations`` failure makes the
``finally`` raise UnboundLocalError and skip ``os.unlink``), and the
``seeded_env`` fixtures perform setup with cleanup only after ``yield``
(a pre-yield raise exits the fixture without any teardown).

Method: drive the REAL fixture generator functions directly with a forced
setup failure (the failure context clobbers the module-level name the
fixture calls at the intended failure point), then assert (a) the ORIGINAL
RuntimeError is the reported error and (b) every temp artifact the fixture
itself created (recorded via the tempfile-constructor wrapper below) is
removed. The fixture is located via the test module first and, once AC6's
dedup moved the fixtures into the shared home, via that home.

Feedback FB-001 (PR-review run pr1174-review-202611191037, critic-merged
IA-001+CC-001+TF-001+RP-001): leftover assertions scoped by RECORDING the
exact tempfile.mkstemp/mkdtemp paths the fixture creates — never a
before/after glob of the shared system tempdir. The PR's own CI round 1
proved the glob-diff class is xdist-unsafe (foreign temp files flag as
phantom leaks; the unscoped scrub deletes other workers' live artifacts);
the recording pattern was already proven in this file for two probes and is
now the ONLY pattern here — the glob snapshot helpers are gone, and every
scrub deletes exactly the recorded paths.

Frozen Phase 2.5 checks (arm's-length author; amendment waves recorded in
repro/checkpoint.manifest):

- AC4 — A/B ``fresh_db`` setup failure (run_migrations and
  sqlite3.connect legs, sweep sites, class fixture): original error + no
  recorded artifact left. RED at base (UnboundLocalError masks the error,
  temp DB left behind), GREEN after the fix.
  ``test_c03b_prompt_resolution_edges`` is the already-guarded control
  param (GREEN at base — it proves the probe passes on a correct fixture).
- AC5 — ``seeded_env`` setup failure (migration, pre-migration pool-cache,
  issue645 ancestor, setup_db migration AND seed legs): original error +
  no recorded dir left. RED at base, GREEN after the fix.
- AC6 — the duplicated fixtures no longer have local definitions in the
  non-frozen c04 modules (dedup into one shared home). RED at base, GREEN
  after the fix.

Standalone twin: .agents/issue-traces/1171-900-946-947-residuals-
hygiene-sweep/repro/check_fixture_teardown.py drives the A/B shape without
pytest for cheap replay.
"""

import importlib
import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

PROBE_MARKER = "forced-setup-failure-946"

# A/B fresh_db shape (AC4). The first five carry the defect the issues name
# or the plan-critic sweep found; the c03b sibling is the already-guarded
# control (proves the probe passes on a correct fixture).
AB_FRESH_DB_MODULES = [
    "test_c04_ab_exposure_failure",
    "test_c04_ab_lookup_failure",
    "test_prompt_ab_variants",
    "test_prompt_versioning",
    "test_c03_orgless_prompt",
    "test_prompt_org_overrides",
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


@contextmanager
def _recording_temp_artifacts():
    """Record the EXACT temp artifacts (mkstemp files, mkdtemp dirs) created
    during the drive, so leftover assertions are scoped to artifacts the
    fixture itself created — never a before/after glob of the shared system
    tempdir (feedback FB-001). tempfile is a process-global module, so the
    constructor patch intercepts every ``tempfile.mkstemp``/``mkdtemp`` call
    the fixture makes regardless of which module object it reached through;
    both originals are restored in ``finally`` (exception-safe).
    """
    original_mkstemp = tempfile.mkstemp
    original_mkdtemp = tempfile.mkdtemp
    created: list = []

    def recording_mkstemp(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        created.append(("file", path))
        return fd, path

    def recording_mkdtemp(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        created.append(("dir", path))
        return path

    try:
        tempfile.mkstemp = recording_mkstemp
        tempfile.mkdtemp = recording_mkdtemp
        yield created
    finally:
        tempfile.mkstemp = original_mkstemp
        tempfile.mkdtemp = original_mkdtemp


def _scrub_created(created):
    """Remove recorded artifacts that still exist (feedback FB-012: a probe
    must not persist the very leak it flags on a RED replay). Recorded paths
    belong to the fixture the probe drove, so the scrub is scoped by
    construction — unlike the pre-fix unscoped glob-diff delete."""
    for kind, path in created:
        if not os.path.exists(path):
            continue
        if kind == "dir":
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def _assert_created_gone(created, ac_prefix: str, context: str):
    leftovers = [(kind, p) for kind, p in created if os.path.exists(p)]
    assert leftovers == [], (
        f"{ac_prefix}-LEFTOVER: {context} left temp artifact(s) behind on "
        f"setup failure: {leftovers}"
    )


def _assert_created_nonempty(created, ac_prefix: str, context: str):
    assert created, (
        f"sanity: {context} created no temp artifacts during the drive "
        f"(the recording wrapper is not intercepting — probe vacuous)"
    )


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
    """Clobber sqlite3.connect with a raising probe (AC4 names BOTH the
    run_migrations and the sqlite3.connect failure legs — a guard that only
    survives the first is half-fixed).

    sqlite3.connect is ALSO used inside run_migrations (its internal
    connection carries kwargs like timeout=), so this leg no-ops
    run_migrations everywhere it is bound — the fixture's own bare
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
                patched.append(
                    (module, "run_migrations", migrations_original, migrations_noop)
                )
        sqlite3_mod.connect = connect_boom
        yield calls
    finally:
        sqlite3_mod.connect = connect_original
        for module, name, old, _new in patched:
            setattr(module, name, old)


@contextmanager
def _forced_pool_cache_failure():
    """Clobber app.models.database._pool_cache so the seeded_env setup's
    pool-cache drain raises BEFORE run_migrations (AC5 must exercise
    pre-migration setup too — the try/finally must wrap from mkdtemp, not
    from run_migrations). The probe delegates to the ORIGINAL dict for every
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
    namespace first, then the shared home (c04_fixtures) the AC6 dedup
    moved the fixtures into — never another test module, so a frozen module
    keeping its own def cannot shadow the shared one."""
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
    failure context defaults to the run_migrations clobber."""
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
                f"unexpectedly survived the forced setup failure"
            )


# ---------------------------------------------------------------------------
# AC4 — A/B fresh_db: setup failure must clean up the temp DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", AB_FRESH_DB_MODULES)
def test_ab_fresh_db_setup_failure_cleans_up(module_name, monkeypatch):
    """AC4 (#946): a run_migrations failure during the A/B fresh_db
    fixture's setup must surface the ORIGINAL error and remove the temp
    database. RED at base, GREEN after the fix. The leftover assertion is
    recording-scoped (FB-001), not a shared-tempdir glob."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture, monkeypatch, "AC4", f"{module_name}.fresh_db"
            )
            _assert_created_nonempty(created, "AC4", f"{module_name}.fresh_db")
            _assert_created_gone(created, "AC4", f"{module_name}.fresh_db")
        finally:
            _scrub_created(created)


# ---------------------------------------------------------------------------
# AC5 — mkdtemp seeded_env: setup failure must remove the temp dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", SEEDED_ENV_MODULES)
def test_seeded_env_setup_failure_cleans_up(module_name, monkeypatch):
    """AC5 (#946/#900): a setup failure in a mkdtemp seeded_env fixture
    (before yield) must surface the ORIGINAL error and remove the temp
    directory. RED at base, GREEN after the fix. Recording-scoped
    (FB-001)."""
    fixture = _resolve_fixture(module_name, "seeded_env")
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture, monkeypatch, "AC5", f"{module_name}.seeded_env"
            )
            _assert_created_nonempty(
                created, "AC5", f"{module_name}.seeded_env"
            )
            _assert_created_gone(created, "AC5", f"{module_name}.seeded_env")
        finally:
            _scrub_created(created)


# ---------------------------------------------------------------------------
# AC4/AC5 amendment legs: the OTHER failure points
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
    that only survives the first is half-fixed. run_migrations is no-op'd
    during this leg so the fixture's OWN bare sqlite3.connect(path) call is
    the failure point (the recorded calls must include a positional-only
    connect — the migration path carries a timeout kwarg). RED at base,
    GREEN after the fix. Recording-scoped (FB-001)."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    with _recording_temp_artifacts() as created:
        try:
            with _forced_sqlite_connect_failure() as calls:
                gen = fixture()
                try:
                    next(gen)
                except RuntimeError as exc:
                    assert PROBE_MARKER in str(exc), (
                        f"AC4-ORIGINAL-ERROR-MASKED: {module_name}.fresh_db "
                        f"reported an unexpected error on connect failure: "
                        f"{exc}"
                    )
                else:
                    gen.close()
                    pytest.fail(
                        f"AC4-NO-SETUP-FAILURE: {module_name}.fresh_db "
                        f"survived the forced sqlite3.connect failure"
                    )
            positional = [c for c in calls if not c[1]]
            assert positional, (
                "AC4-CONNECT-NOT-REACHED: the fixture's own positional "
                "sqlite3.connect(path) was never reached (the failure fired "
                f"elsewhere): calls={calls}"
            )
            _assert_created_nonempty(
                created, "AC4", f"{module_name}.fresh_db(connect)"
            )
            _assert_created_gone(
                created, "AC4", f"{module_name}.fresh_db(connect)"
            )
        finally:
            _scrub_created(created)


@pytest.mark.parametrize("module_name", SEEDED_ENV_MODULES)
def test_seeded_env_pre_migration_failure_cleans_up(module_name, monkeypatch):
    """AC5 amendment (#946/#900): a failure DURING the pre-migration setup
    (the pool-cache drain runs after mkdtemp and before run_migrations)
    must also remove the temp dir — the try/finally must wrap from
    mkdtemp, not from run_migrations. RED at base, GREEN after the fix.
    Recording-scoped (FB-001)."""
    fixture = _resolve_fixture(module_name, "seeded_env")
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture,
                monkeypatch,
                "AC5",
                f"{module_name}.seeded_env(pre-migration)",
                failure_ctx=_forced_pool_cache_failure(),
            )
            _assert_created_nonempty(
                created, "AC5", f"{module_name}.seeded_env(pre-migration)"
            )
            _assert_created_gone(
                created, "AC5", f"{module_name}.seeded_env(pre-migration)"
            )
        finally:
            _scrub_created(created)


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
    original-error + no-leftover proof as the issue-named files. RED at
    base, GREEN after the fix. Recording-scoped (FB-001)."""
    fixture = _resolve_fixture(module_name, "fresh_db")
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(fixture, monkeypatch, "AC4", f"{module_name}.fresh_db")
            _assert_created_nonempty(created, "AC4", f"{module_name}.fresh_db")
            _assert_created_gone(created, "AC4", f"{module_name}.fresh_db")
        finally:
            _scrub_created(created)


def test_multi_org_db_class_fixture_setup_failure_cleans_up(monkeypatch):
    """Plan-critic round 2 (#946 class): the multi_org_db fixture defined
    on TestMultiOrgPerQueryResolution in test_prompt_org_overrides is the
    second defect site in that module — the same guarded-teardown
    contract applies. RED at base, GREEN after the fix.
    Recording-scoped (FB-001)."""
    import types

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestMultiOrgPerQueryResolution")
    fixture = types.MethodType(_unwrap_fixture(holder.multi_org_db), holder())
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture, monkeypatch, "AC4", "test_prompt_org_overrides.multi_org_db"
            )
            _assert_created_nonempty(
                created, "AC4", "test_prompt_org_overrides.multi_org_db"
            )
            _assert_created_gone(
                created, "AC4", "test_prompt_org_overrides.multi_org_db"
            )
        finally:
            _scrub_created(created)


def test_issue645_mkddir_fixture_setup_failure_cleans_up(monkeypatch):
    """Plan-critic round 2 (#946 class): the mkdtemp harness ANCESTOR in
    test_issue645_converted_route_http.py has the same pre-yield exposure
    (its temp prefix is issue645_routes_*, outside the c04_* glob, so it
    gets its own probe). RED at base, GREEN after the fix.
    Recording-scoped (FB-001): the leftover assertion and the finally scrub
    both use the exact paths tempfile.mkdtemp created during the drive —
    the pre-fix shared-tempdir glob diff is gone."""
    module = importlib.import_module("test_issue645_converted_route_http")
    fixture = _unwrap_fixture(module.seeded_env)
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture,
                monkeypatch,
                "AC5",
                "test_issue645_converted_route_http.seeded_env",
            )
            _assert_created_nonempty(
                created, "AC5", "test_issue645_converted_route_http.seeded_env"
            )
            _assert_created_gone(
                created, "AC5", "test_issue645_converted_route_http.seeded_env"
            )
        finally:
            _scrub_created(created)


def test_org_overrides_setup_db_failure_cleans_up(monkeypatch):
    """Feedback FB-001 scope extension (#946 class): the autouse class
    fixture setup_db in test_prompt_org_overrides
    (TestPromptOverrideAPIFixtures) is the module's third mkdtemp site —
    the same original-error + no-leftover contract applies to the migration
    leg (the SEED phase gets its own leg below). RED at the pre-fix tree,
    GREEN after the fix. Recording-scoped — the probe asserts the EXACT
    mkdtemp dir the fixture created is gone (never a shared-tempdir glob)."""
    import types

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestPromptOverrideAPIFixtures")
    fixture = types.MethodType(_unwrap_fixture(holder.setup_db), holder())
    with _recording_temp_artifacts() as created:
        try:
            _drive_setup_failure(
                fixture,
                monkeypatch,
                "AC5",
                "test_prompt_org_overrides.setup_db",
            )
            _assert_created_nonempty(
                created, "AC5", "test_prompt_org_overrides.setup_db"
            )
            _assert_created_gone(
                created, "AC5", "test_prompt_org_overrides.setup_db"
            )
        finally:
            _scrub_created(created)


@contextmanager
def _forced_hash_failure(module):
    """Clobber the module's hash_password so the seed phase (which opens
    the sqlite connection FIRST, then hashes) fails AFTER
    sqlite3.connect — the leg that makes the nested conn.close() load
    bearing."""
    original = module.hash_password

    def boom(*args, **kwargs):
        raise RuntimeError(PROBE_MARKER)

    try:
        module.hash_password = boom
        yield
    finally:
        module.hash_password = original


@contextmanager
def _capturing_sqlite_connect():
    """Capture every sqlite3.connect return value created during the drive
    (feedback FB-002): the seed connection is the LAST one opened before the
    forced hash failure, and asserting it is CLOSED afterwards is the
    platform-independent discriminator for the nested conn.close() — the
    dir-removal signal alone is Windows-only (POSIX rmtree succeeds with the
    fd open)."""
    import sqlite3 as sqlite3_mod

    original = sqlite3_mod.connect
    captured: list = []

    def recording_connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        captured.append(conn)
        return conn

    try:
        sqlite3_mod.connect = recording_connect
        yield captured
    finally:
        sqlite3_mod.connect = original


def test_org_overrides_setup_db_seed_failure_closes_conn(monkeypatch):
    """Feedback FB-002 (#946 class): the SEED phase of the same setup_db
    fixture — hash_password raises AFTER sqlite3.connect — must close the
    connection in the fixture's nested finally BEFORE the outer rmtree. The
    discriminator is PLATFORM-INDEPENDENT: after the drive, the captured
    seed connection must be closed (execute raises ProgrammingError); a
    deleted nested close leaves it open on EVERY platform, so the mutant
    that round-6's review asked about goes RED here on Linux CI too (the
    dir-removal signal alone is Windows-only). RED without the nested
    close, GREEN with it. Recording-scoped (FB-001)."""
    import types

    module = importlib.import_module("test_prompt_org_overrides")
    holder = getattr(module, "TestPromptOverrideAPIFixtures")
    fixture = types.MethodType(_unwrap_fixture(holder.setup_db), holder())
    with _recording_temp_artifacts() as created:
        try:
            with _capturing_sqlite_connect() as conns:
                with _forced_hash_failure(module):
                    _drive_setup_failure(
                        fixture,
                        monkeypatch,
                        "AC5",
                        "test_prompt_org_overrides.setup_db(seed)",
                        failure_ctx=_forced_hash_failure(module),
                    )
            assert conns, (
                "sanity: the seed phase opened a sqlite connection "
                "(the capture wrapper is not intercepting — probe vacuous)"
            )
            _assert_created_nonempty(
                created, "AC5", "test_prompt_org_overrides.setup_db(seed)"
            )
            _assert_created_gone(
                created, "AC5", "test_prompt_org_overrides.setup_db(seed)"
            )
            seed_conn = conns[-1]
            try:
                seed_conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                pass  # closed — the fixture's nested finally ran
            else:
                pytest.fail(
                    "AC5-CONN-LEFT-OPEN: the seed connection was not closed "
                    "before teardown — the nested conn.close() is load "
                    "bearing on every platform (a deleted close ships green "
                    "through CI's dir-removal signal)"
                )
        finally:
            _scrub_created(created)


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
    # Feedback UR-001 disposition evidence: this census IS the mechanical
    # tripwire for duplicate-fixture re-introduction in the F811-ignored
    # modules — the lint ignore is necessary (parameter shadowing) and the
    # behavioral guard lives here.
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
