"""Runtime embedding-identity changes must take the vector store out of
readiness until an admin reindexes (issue #695 acceptance checks).

PUT/POST ``/api/settings`` happily change ``embedding_model`` (and the
``embedding_doc_prefix`` half of the validated prefix hash identity) that the
on-disk chunk index was built against, but nothing in the settings save path
flips ``app.state.vector_store._ready``. The next chat/retrieval request then
queries the old index with the new embedding model — silently wrong retrieval
instead of the 503 "Embedding model mismatch — admin reindex required" gate
that ``require_model_ready`` exists to raise (issue #695).

These acceptance checks are RED until the settings save path marks the live
vector store not-ready on a real embedding-identity change; a redundant save
of the same identity must keep the store ready (a no-op save must not take
chat down).
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

CSRF_TEST_POLICY = "naive"  # harness sends no token header; declared for tests/conftest.py

# Stub missing optional dependencies (same defensive pattern as test_settings.py)
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from fastapi import HTTPException
from fastapi.testclient import TestClient

# Create a temporary database for testing (same pattern as test_settings.py)
TEST_DB_PATH = None
TEST_DATA_DIR = None


def setup_test_db():
    """Set up a temporary test database."""
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"

    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


# Set up test database before importing app
setup_test_db()

from app.api.deps import require_model_ready  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402


@pytest.fixture
def ready_store_client():
    """Admin-authenticated client whose app.state carries a fresh, ready
    vector store, with every settings field snapshotted for restoration.

    TestClient used without a context manager skips lifespan startup, so
    ``app.state.vector_store`` is whatever earlier tests left behind. Install
    a fresh, ready store for determinism and restore the original after. A
    real ``VectorStore`` is used (its ``__init__`` touches no external
    service) because the contract under test is that very class's ``_ready``
    flag / ``mark_ready`` lifecycle.
    """
    settings_snapshot = {
        name: getattr(settings, name) for name in type(settings).model_fields
    }
    orig_users_enabled = settings.users_enabled
    orig_vector_store = getattr(app.state, "vector_store", None)
    had_vector_store = hasattr(app.state, "vector_store")
    settings.users_enabled = False
    store = VectorStore()
    app.state.vector_store = store

    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {settings.admin_secret_token}"}
    )

    from app.api.deps import get_db
    from app.models.database import get_pool

    test_pool = get_pool(str(TEST_DB_PATH))

    def override_get_db():
        conn = test_pool.get_connection()
        try:
            yield conn
        finally:
            test_pool.release_connection(conn)

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield client, store
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.users_enabled = orig_users_enabled
        # Restore every other settings field the save touched (issue #565
        # randomized-order canary: singleton pollution failed pristine-default
        # assertions in other test files under a different seed).
        for _name, _value in settings_snapshot.items():
            setattr(settings, _name, _value)
        # Restore the vector store the earlier test environment had.
        if had_vector_store:
            app.state.vector_store = orig_vector_store
        else:
            try:
                del app.state.vector_store
            except AttributeError:
                pass
        # Remove any persisted row so later tests start from a clean slate.
        conn = test_pool.get_connection()
        try:
            conn.execute("DELETE FROM settings_kv")
            conn.commit()
        finally:
            test_pool.release_connection(conn)


def _gate_status() -> int:
    """Evaluate the readiness gate exactly as a request-scoped dependency
    would: call ``require_model_ready`` on the live store and fold any
    HTTPException into its status code (200 means admitted)."""
    try:
        require_model_ready(app.state.vector_store)
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_put_embedding_model_change_flips_readiness(ready_store_client):
    """PUT /settings with a changed embedding_model must leave the live
    vector store not-ready so the next request hits the 503 reindex gate
    instead of querying a stale index with the new model (issue #695)."""
    client, _store = ready_store_client
    old_model = settings.embedding_model
    new_model = old_model + "-other"
    resp = client.put("/api/settings", json={"embedding_model": new_model})
    assert resp.status_code == 200, resp.text

    status = _gate_status()
    assert status == 503, (
        "changed embedding_model must gate requests with 503 until an "
        "admin reindexes: embedding_model went "
        f"{old_model!r} -> {new_model!r} but require_model_ready returned "
        f"{status} with _ready={app.state.vector_store._ready!r}"
    )
    assert app.state.vector_store._ready is False, (
        "the store itself must carry _ready=False after an embedding "
        f"model change ({old_model!r} -> {new_model!r}); "
        f"got _ready={app.state.vector_store._ready!r}"
    )


def test_post_embedding_model_change_flips_readiness(ready_store_client):
    """POST /settings with a changed embedding_model must flip readiness
    exactly like the PUT path — both handlers share the apply pipeline
    (issue #695)."""
    client, _store = ready_store_client
    old_model = settings.embedding_model
    new_model = old_model + "-other"
    resp = client.post("/api/settings", json={"embedding_model": new_model})
    assert resp.status_code == 200, resp.text

    status = _gate_status()
    assert status == 503, (
        "changed embedding_model via POST must gate requests with 503 "
        "until an admin reindexes: embedding_model went "
        f"{old_model!r} -> {new_model!r} but require_model_ready returned "
        f"{status} with _ready={app.state.vector_store._ready!r}"
    )
    assert app.state.vector_store._ready is False, (
        "the store itself must carry _ready=False after an embedding "
        f"model change ({old_model!r} -> {new_model!r}); "
        f"got _ready={app.state.vector_store._ready!r}"
    )


def test_put_same_embedding_model_keeps_readiness(ready_store_client):
    """A redundant PUT of the same embedding_model must keep the store
    ready — the flip is reserved for a real identity change, and a no-op
    save must not take chat down (issue #695)."""
    client, _store = ready_store_client
    resp = client.put(
        "/api/settings", json={"embedding_model": settings.embedding_model}
    )
    assert resp.status_code == 200, resp.text

    status = _gate_status()
    assert status == 200, (
        "re-saving the same embedding_model "
        f"({settings.embedding_model!r}) must not gate requests: "
        f"require_model_ready returned {status} with "
        f"_ready={app.state.vector_store._ready!r}"
    )
    assert app.state.vector_store._ready is True, (
        "the store must stay _ready=True when the embedding identity did "
        f"not change; got _ready={app.state.vector_store._ready!r}"
    )


def test_put_embedding_prefix_change_flips_readiness(ready_store_client):
    """PUT /settings with a changed embedding_doc_prefix must flip
    readiness too: the prefix feeds the same validated identity via the
    prefix hash, so retrieval semantics changed just as surely as a model
    swap (issue #695)."""
    client, _store = ready_store_client
    new_prefix = (settings.embedding_doc_prefix or "") + "x"
    resp = client.put(
        "/api/settings", json={"embedding_doc_prefix": new_prefix}
    )
    assert resp.status_code == 200, resp.text

    status = _gate_status()
    assert status == 503, (
        "changed embedding_doc_prefix must gate requests with 503 until "
        f"an admin reindexes: prefix became {new_prefix!r} but "
        f"require_model_ready returned {status} with "
        f"_ready={app.state.vector_store._ready!r}"
    )
    assert app.state.vector_store._ready is False, (
        f"the store itself must carry _ready=False after the prefix "
        f"change to {new_prefix!r}; got "
        f"_ready={app.state.vector_store._ready!r}"
    )
