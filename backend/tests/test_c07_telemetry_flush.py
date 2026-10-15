"""Frozen check for issue #712 (C07) — telemetry shard-flush isolation.

AC3: a RuntimeError raised while serializing the telemetry shard (e.g.
``json.dumps`` failing with "dictionary changed size during iteration")
must not escape the recorder. Telemetry recording happens on hot request
paths, so any non-OSError exception from ``_flush_shard`` breaks the call
it was supposed to be measuring.
"""

import os
import sys
import types

# Stub optional heavy dependencies not installed in the test env.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

for _mod in ('lancedb', 'pyarrow'):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            _stub = types.ModuleType(_mod)
            sys.modules[_mod] = _stub

# lancedb.index must exist before vector_store.py is imported
if 'lancedb.index' not in sys.modules:
    _ldb_idx = types.ModuleType('lancedb.index')
    _ldb_idx.FTS = type('FTS', (), {})
    _ldb_idx.IvfPq = type('IvfPq', (), {})
    sys.modules['lancedb.index'] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ('partition', 'partition.auto', 'chunking', 'chunking.title',
                 'documents', 'documents.elements'):
        sys.modules.setdefault(f'unstructured.{_sub}', types.ModuleType(f'unstructured.{_sub}'))
    sys.modules.setdefault('unstructured', types.ModuleType('unstructured'))

from app.services.telemetry import Telemetry


def test_flush_runtime_error_does_not_escape_recorder(tmp_path, monkeypatch):
    """AC3: record_embedding_cache must not raise when json.dumps blows up."""
    def _explode(*args, **kwargs):
        raise RuntimeError("dictionary changed size during iteration")

    # Patch the json reference the telemetry module uses; the monkeypatch
    # fixture restores it after the test.
    monkeypatch.setattr("app.services.telemetry.json.dumps", _explode)

    telemetry = Telemetry(enabled=True, registry_dir=tmp_path)

    raised = False
    try:
        telemetry.record_embedding_cache(True)
    except RuntimeError:
        raised = True

    assert raised is False
