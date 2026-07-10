"""Pytest configuration for backend tests."""

import sys
import types
from pathlib import Path

import pytest

# Add backend directory to path so tests can import app modules
sys.path.insert(0, str(Path(__file__).parent))

# Stub problematic optional dependencies BEFORE any test imports
# This must happen before pytest collection to prevent import errors

# Stub lancedb only when the real package is unavailable. lancedb is excluded
# from CI requirements (requirements-ci.txt); this stub only activates when the
# real lancedb package is unavailable (e.g., local dev without lancedb installed).
try:
    import lancedb  # noqa: F401
except ImportError:
    _lancedb = types.ModuleType("lancedb")
    _lancedb.index = types.ModuleType("lancedb.index")
    # Add fake IvfPq, FTS classes to prevent import errors
    _lancedb.index.IvfPq = type("IvfPq", (), {})
    _lancedb.index.FTS = type("FTS", (), {})
    _lancedb.expr = types.ModuleType("lancedb.expr")

    # Minimal col/lit stubs so vector_store.py can import them for tests
    class _ExprStub:
        def __init__(self, sql):
            self._sql = sql

        def eq(self, other):
            if isinstance(other, _ExprStub):
                return _ExprStub(f"{self._sql} = {other._sql}")
            return _ExprStub(f"{self._sql} = {other!r}")

        def to_sql(self):
            return self._sql

        def __and__(self, other):
            if isinstance(other, _ExprStub):
                return _ExprStub(f"({self._sql}) AND ({other._sql})")
            return _ExprStub(f"({self._sql}) AND ({other})")

        def __rand__(self, other):
            if isinstance(other, _ExprStub):
                return _ExprStub(f"({other._sql}) AND ({self._sql})")
            return _ExprStub(f"({other}) AND ({self._sql})")

    _lancedb.expr.col = lambda name: _ExprStub(name)
    _lancedb.expr.lit = lambda value: _ExprStub(repr(value))

    # Minimal connect/create_table stub for tests that use the real LanceDB API
    class _StubTable:
        """Stub for a LanceDB table."""

        def __init__(self, name, schema=None):
            self.name = name
            self._schema = schema

        async def schema(self):
            return self._schema

        def __repr__(self):
            return f"<_StubTable {self.name}>"

    class _StubDB:
        """Stub for a LanceDB connection (returned by lancedb.connect)."""

        def __init__(self, uri):
            self.uri = uri
            self._tables = {}

        async def table_names(self):
            return list(self._tables.keys())

        def create_table(self, name, schema=None, exist_ok=False):
            """Synchronous create_table (matches real LanceDB API)."""
            table = _StubTable(name, schema)
            self._tables[name] = table
            return table

        async def open_table(self, name):
            return self._tables.get(name, _StubTable(name))

        def __repr__(self):
            return f"<_StubDB {self.uri}>"

    def _stub_connect(uri):
        return _StubDB(uri)

    _lancedb.connect = _stub_connect
    sys.modules["lancedb"] = _lancedb
    sys.modules["lancedb.index"] = _lancedb.index
    sys.modules["lancedb.expr"] = _lancedb.expr

# Stub pyarrow to a rich, test-safe implementation. CI installs the real package,
# but many test files fall back to an attribute-less `types.ModuleType("pyarrow")`
# when pyarrow is unavailable, which overrides any real/stub pyarrow already in
# `sys.modules` and breaks tests that call `pa.schema(...)` or use pa type helpers.
# Always installing this expanded stub (with explicit schema/type helpers and a
# `__getattr__` fallback) keeps the test environment consistent in both local and
# CI runs.
class _PyArrowStubMeta(type):
    def __instancecheck__(cls, instance):
        return False

class _PyArrowType:
    def __init__(self, name, value_type=None, list_size=None):
        self.name = name
        self.value_type = value_type
        self.list_size = list_size

class _PyArrowField:
    def __init__(self, name, type_, nullable=True):
        self.name = name
        self.type = type_
        self.nullable = nullable

class _PyArrowSchema:
    def __init__(self, fields, metadata=None):
        self._fields = list(fields)
        self.metadata = metadata or {}

    def __len__(self):
        return len(self._fields)

    def field(self, i_or_name):
        if isinstance(i_or_name, str):
            for f in self._fields:
                if f.name == i_or_name:
                    return f
            raise KeyError(i_or_name)
        return self._fields[i_or_name]

    def names(self):
        return [f.name for f in self._fields]

class _PyArrowModule(types.ModuleType):
    def __init__(self, name):
        super().__init__(name)
        self.__version__ = "0.0.0"
        self._stub_cls = _PyArrowStubMeta("_PyArrowStub", (), {})
        self.Array = self._stub_cls
        self.ChunkedArray = self._stub_cls
        self.schema = lambda fields, metadata=None: _PyArrowSchema(fields, metadata)
        self.field = lambda name, type_, nullable=True: _PyArrowField(name, type_, nullable)
        self.string = lambda: _PyArrowType("string")
        self.int32 = lambda: _PyArrowType("int32")
        self.float32 = lambda: _PyArrowType("float32")
        self.list_ = lambda value_type, list_size=None: _PyArrowType("list", value_type, list_size)
        self.binary = lambda: _PyArrowType("binary")

    def __getattr__(self, name):
        return self._stub_cls

_pa_stub = _PyArrowModule("pyarrow")
sys.modules["pyarrow"] = _pa_stub

# Stub numpy when not installed. vector_store.py imports numpy at the top level;
# tests that only exercise pure-Python logic (e.g. RAGEngine._raw_rag_required)
# do not need the real array implementation.
try:
    import numpy  # noqa: F401
except ImportError:
    _np_stub = types.ModuleType("numpy")
    _np_stub.__version__ = "0.0.0"
    _np_stub.float32 = float
    _np_stub.ndarray = list
    _np_stub.array = lambda *a, **kw: list(a[0]) if a else []
    sys.modules["numpy"] = _np_stub

# Stub jwt (PyJWT) — system-level jwt has a broken C extension in this env.
# Only stub when the real import fails so a working install is not replaced.
try:
    # Attempt to import the actual jwt; if the C extension is broken it raises.
    _jwt_check = __import__("jwt")
    _jwt_check.ExpiredSignatureError  # attribute probe to catch partially-broken installs
except Exception:
    _jwt_stub = types.ModuleType("jwt")
    _jwt_stub.encode = lambda *a, **kw: "stub-token"
    _jwt_stub.decode = lambda *a, **kw: {}
    _jwt_stub.ExpiredSignatureError = type("ExpiredSignatureError", (Exception,), {})
    _jwt_stub.InvalidTokenError = type("InvalidTokenError", (Exception,), {})
    sys.modules["jwt"] = _jwt_stub

# Stub unstructured
_unstructured = types.ModuleType("unstructured")
_unstructured.partition = types.ModuleType("unstructured.partition")
_unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
_unstructured.partition.auto.partition = lambda *args, **kwargs: []
_unstructured.chunking = types.ModuleType("unstructured.chunking")
_unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
_unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
_unstructured.documents = types.ModuleType("unstructured.documents")
_unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
_unstructured.documents.elements.Element = type("Element", (), {})
_unstructured.file_utils = types.ModuleType("unstructured.file_utils")
_unstructured.file_utils.filetype = types.ModuleType("unstructured.file_utils.filetype")
sys.modules["unstructured"] = _unstructured
sys.modules["unstructured.partition"] = _unstructured.partition
sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
sys.modules["unstructured.chunking"] = _unstructured.chunking
sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
sys.modules["unstructured.documents"] = _unstructured.documents
sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements
sys.modules["unstructured.file_utils"] = _unstructured.file_utils
sys.modules["unstructured.file_utils.filetype"] = _unstructured.file_utils.filetype


# Opt-in fixture: provides a ready vector_store on app.state.
# Tests that explicitly set app.state.vector_store are unaffected because this
# fixture only runs when the attribute is absent.
@pytest.fixture
def ready_vector_store():
    from unittest.mock import MagicMock

    from app.main import app

    if not hasattr(app.state, "vector_store"):
        vs = MagicMock()
        vs._ready = True
        app.state.vector_store = vs
        app.state._vector_store_fixture_set = True

    yield

    if getattr(app.state, "_vector_store_fixture_set", False):
        if hasattr(app.state, "vector_store"):
            delattr(app.state, "vector_store")
        if hasattr(app.state, "_vector_store_fixture_set"):
            delattr(app.state, "_vector_store_fixture_set")


