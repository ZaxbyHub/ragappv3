"""Pytest isolation shim for the frozen issue-#513 acceptance checks.

The check modules in this directory are frozen standalone scripts (blob ids
recorded in a manifest; run as ``python backend/tests/issue513_checks/…``).
As standalone scripts they are entitled to mutate process-global state, and
they never restore it. Under pytest that leaks into unrelated tests sharing
the same worker process (``-n auto``):

* module-level ``os.environ[...] = ...`` assignments leak at COLLECTION time
  (before any test runs, in every xdist worker that collects this directory),
  and ``_hermetic_env()`` calls inside ``main()`` leak at RUNTIME — e.g.
  ``DATA_DIR`` breaks ``Settings`` default tests, ``ADMIN_SECRET_TOKEN`` and
  friends change what later config/auth tests observe;
* ``logging.disable(logging.CRITICAL)`` in ``main()`` is never re-enabled,
  suppressing every subsequent log record in the process (that alone fails
  the 9 ``test_admin_token_warning`` assertions on emitted CRITICAL logs);
* checks c9/c28 exercise the REAL vector store (``lancedb.connect_async`` +
  ``pyarrow`` schemas), which ``backend/conftest.py`` deliberately stubs out
  for the suite (CI does not install lancedb) — under the stubs those checks
  die with ``AttributeError: module 'lancedb' has no attribute
  'connect_async'``.

This conftest is NOT frozen and does NOT change what any check asserts. It
only isolates the checks' side effects around each test:

1. snapshot/restore ``os.environ`` and the ``logging`` disable level around
   every check test (autouse fixture), and restore the environment once this
   directory finishes collecting (undoing module-level leaks before later
   test modules are imported);
2. when the real lancedb/pyarrow are importable (local dev — the standalone
   execution environment of the checks), temporarily install them over the
   suite stubs for the duration of each check test, re-importing
   ``app.services.vector_store`` against them, then restore the stubs —
   exactly the state ``backend/conftest.py`` enforces for the rest of the
   suite. When the real packages are unavailable (CI), the stubs are left in
   place and behavior is unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

# Environment snapshot, taken lazily at the first collection event: by then
# tests/conftest.py::pytest_configure has set the suite baseline (it may run
# AFTER this conftest is imported when pytest is pointed directly at this
# directory), while no check module has been imported yet (their module-level
# os.environ assignments are the pollution this conftest undoes).
_ENV_SNAPSHOT: dict[str, str] | None = None


def _module_graph(name: str) -> dict[str, object]:
    """Snapshot every sys.modules entry for ``name`` and its submodules."""
    return {
        k: v for k, v in sys.modules.items() if k == name or k.startswith(name + ".")
    }


def _drop_module_graph(name: str) -> None:
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def _load_real_graph(name: str) -> dict[str, object] | None:
    """Import the REAL ``name`` with its current sys.modules entry removed.

    Returns the real module graph for later temporary installation, restoring
    the previous (stub) graph afterwards. Returns None when the real package
    is not importable (CI keeps the stubs and current behavior).
    """
    saved = _module_graph(name)
    _drop_module_graph(name)
    try:
        __import__(name)
    except ImportError:
        _drop_module_graph(name)
        sys.modules.update(saved)
        return None
    real = _module_graph(name)
    _drop_module_graph(name)
    sys.modules.update(saved)
    return real


# The suite stubs (installed by backend/conftest.py before collection).
_STUB_GRAPHS = {name: _module_graph(name) for name in ("lancedb", "pyarrow")}

# Real pyarrow first: lancedb binds pyarrow at import time and must bind the
# real one, not the suite stub.
_REAL_PYARROW = _load_real_graph("pyarrow")
_REAL_LANCEDB = None
if _REAL_PYARROW is not None:
    _saved_pyarrow = _module_graph("pyarrow")
    _drop_module_graph("pyarrow")
    sys.modules.update(_REAL_PYARROW)
    try:
        _REAL_LANCEDB = _load_real_graph("lancedb")
    finally:
        _drop_module_graph("pyarrow")
        sys.modules.update(_saved_pyarrow)

_REAL_GRAPHS_AVAILABLE = _REAL_LANCEDB is not None and _REAL_PYARROW is not None


def _install_real_optional_deps() -> None:
    """Swap the suite stubs for the real lancedb/pyarrow (checks only)."""
    if not _REAL_GRAPHS_AVAILABLE:
        return
    for name, real in (("pyarrow", _REAL_PYARROW), ("lancedb", _REAL_LANCEDB)):
        _drop_module_graph(name)
        sys.modules.update(real)
    # app.services.vector_store binds lancedb/pyarrow at import time; drop it
    # so the check's (lazy) import re-binds it against the real packages.
    sys.modules.pop("app.services.vector_store", None)


def _restore_stub_optional_deps() -> None:
    """Put the suite stubs back and forget the real-bound re-import."""
    if not _REAL_GRAPHS_AVAILABLE:
        return
    for name in ("lancedb", "pyarrow"):
        _drop_module_graph(name)
        sys.modules.update(_STUB_GRAPHS.get(name) or {})
    # Drop the real-bound vector_store so later imports re-bind against the
    # stubs — exactly the state backend/conftest.py enforces per test.
    sys.modules.pop("app.services.vector_store", None)


def _restore_env() -> None:
    if _ENV_SNAPSHOT is None:
        return
    os.environ.clear()
    os.environ.update(_ENV_SNAPSHOT)


def pytest_collectstart(collector) -> None:  # noqa: ANN001
    """Capture the pre-check environment on the first collection event."""
    global _ENV_SNAPSHOT
    if _ENV_SNAPSHOT is None:
        _ENV_SNAPSHOT = dict(os.environ)


def pytest_collection_finish(session) -> None:  # noqa: ANN001
    """Undo module-level env leaks after collection completes.

    The frozen check modules assign ``os.environ`` at import time (collection),
    polluting every later-imported test module and every test in the worker.
    Restoring here — after every test module has been imported but before any
    test runs — neutralizes those leaks without touching the check files.
    """
    _restore_env()


@pytest.fixture(autouse=True)
def _isolated_check_process_state():
    """Isolate process-global state around every acceptance check test.

    Restores (in teardown): os.environ, the logging disable level, and the
    suite's lancedb/pyarrow stubs — none of which the frozen checks restore
    themselves. The check body itself runs completely unmodified.
    """
    saved_env = dict(os.environ)
    saved_disable = logging.root.manager.disable
    _install_real_optional_deps()
    try:
        yield
    finally:
        _restore_stub_optional_deps()
        logging.disable(saved_disable)
        os.environ.clear()
        os.environ.update(saved_env)
