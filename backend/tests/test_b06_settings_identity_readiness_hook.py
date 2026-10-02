"""Issue #695 hook-level coverage beyond the frozen acceptance module.

The frozen module (test_b06_runtime_model_change_readiness.py) pins the
PUT/POST embedding_model flips, the prefix sibling, and the no-op guard; it
cannot be edited post-freeze, so the plan critic's remaining gaps live here:

* ordering pin — the readiness flip must happen BEFORE ``_hot_rebind_llm_clients``
  (persist has already committed; that hook is not exception-tolerant, so a
  rebind failure must not leave the silent-mismatch window open);
* POST + ``embedding_doc_prefix`` (the frozen prefix check covers PUT only);
* ``embedding_query_prefix``-only PUT (the third identity field);
* missing-store tolerance — with no ``app.state.vector_store`` the save still
  succeeds and the hook no-ops;
* no-op and boundary guards — POST same-value save, same-prefix save,
  whitespace-padded model (validator strips to the current value), a
  ``null``-filtered prefix field, an unrelated field, a store lacking the
  ``_ready`` attribute, and ``embedding_dim`` not being settings-updatable.
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
    vector store, with every settings field snapshotted for restoration
    (same hygiene as the frozen module: issue #565 singleton-pollution
    canary, settings_kv cleanup, app.state.vector_store save/restore)."""
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
        for _name, _value in settings_snapshot.items():
            setattr(settings, _name, _value)
        if had_vector_store:
            app.state.vector_store = orig_vector_store
        else:
            try:
                del app.state.vector_store
            except AttributeError:
                pass
        conn = test_pool.get_connection()
        try:
            conn.execute("DELETE FROM settings_kv")
            conn.commit()
        finally:
            test_pool.release_connection(conn)


def _gate_status() -> int:
    """Evaluate the readiness gate as a request-scoped dependency would."""
    try:
        require_model_ready(app.state.vector_store)
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_flip_survives_failing_hot_rebind(ready_store_client, monkeypatch):
    """The readiness flip must already be applied when a LATER post-apply
    hook raises: persist has committed, so the handler aborts with 500 but
    the store must not stay ready over a mismatched index (issue #695)."""
    client, store = ready_store_client

    def _explode(app, update):
        raise RuntimeError("simulated rebind failure")

    monkeypatch.setattr(
        "app.api.routes.settings._hot_rebind_llm_clients", _explode
    )
    failing_client = TestClient(app, raise_server_exceptions=False)
    failing_client.headers.update(
        {"Authorization": f"Bearer {settings.admin_secret_token}"}
    )

    resp = failing_client.put(
        "/api/settings", json={"embedding_model": settings.embedding_model + "-x"}
    )
    assert resp.status_code == 500, resp.text
    assert store._ready is False, (
        "the flip must precede _hot_rebind_llm_clients so a failing rebind "
        f"cannot leave _ready={store._ready!r} over a mismatched index"
    )
    assert _gate_status() == 503


def test_post_doc_prefix_change_flips_readiness(ready_store_client):
    """POST with a changed embedding_doc_prefix must flip readiness — the
    frozen prefix check covers PUT only, and both handlers share the hook
    (issue #695)."""
    client, store = ready_store_client
    new_prefix = (settings.embedding_doc_prefix or "") + "y"
    resp = client.post("/api/settings", json={"embedding_doc_prefix": new_prefix})
    assert resp.status_code == 200, resp.text
    assert store._ready is False
    assert _gate_status() == 503


def test_query_prefix_only_change_flips_readiness(ready_store_client):
    """A query-prefix-only PUT changes the validated prefix hash and must
    flip readiness just like a doc-prefix change (issue #695)."""
    client, store = ready_store_client
    new_prefix = (settings.embedding_query_prefix or "") + "z"
    resp = client.put("/api/settings", json={"embedding_query_prefix": new_prefix})
    assert resp.status_code == 200, resp.text
    assert store._ready is False
    assert _gate_status() == 503


def test_identity_save_without_vector_store_still_succeeds():
    """With no app.state.vector_store installed, an identity-changing save
    must still succeed (200) and the hook must no-op — nothing else in the
    handler needs the store (issue #695)."""
    settings_snapshot = {
        name: getattr(settings, name) for name in type(settings).model_fields
    }
    orig_users_enabled = settings.users_enabled
    orig_vector_store = getattr(app.state, "vector_store", None)
    had_vector_store = hasattr(app.state, "vector_store")
    settings.users_enabled = False
    if had_vector_store:
        del app.state.vector_store

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
    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {settings.admin_secret_token}"}
    )
    try:
        resp = client.put(
            "/api/settings",
            json={"embedding_model": settings.embedding_model + "-nostore"},
        )
        assert resp.status_code == 200, resp.text
        assert not hasattr(app.state, "vector_store")
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.users_enabled = orig_users_enabled
        for _name, _value in settings_snapshot.items():
            setattr(settings, _name, _value)
        if had_vector_store:
            app.state.vector_store = orig_vector_store
        conn = test_pool.get_connection()
        try:
            conn.execute("DELETE FROM settings_kv")
            conn.commit()
        finally:
            test_pool.release_connection(conn)


def test_post_same_embedding_model_keeps_readiness(ready_store_client):
    """A redundant POST of the same embedding_model must keep the store ready
    — the POST handler's no-op guard is a separate code copy from the PUT
    one, so it is pinned on its own (issue #695)."""
    client, store = ready_store_client
    resp = client.post(
        "/api/settings", json={"embedding_model": settings.embedding_model}
    )
    assert resp.status_code == 200, resp.text
    assert store._ready is True
    assert _gate_status() == 200


def test_put_same_doc_prefix_keeps_readiness(ready_store_client):
    """Re-saving the current embedding_doc_prefix must not flip readiness —
    the no-op guard covers every identity field, not just the model
    (issue #695)."""
    client, store = ready_store_client
    resp = client.put(
        "/api/settings",
        json={"embedding_doc_prefix": settings.embedding_doc_prefix or ""},
    )
    assert resp.status_code == 200, resp.text
    assert store._ready is True
    assert _gate_status() == 200


def test_put_whitespace_padded_model_keeps_readiness(ready_store_client):
    """A whitespace-padded model name strips to the current value before it
    is applied, so the effective identity is unchanged and the store must
    stay ready (issue #695)."""
    client, store = ready_store_client
    resp = client.put(
        "/api/settings",
        json={"embedding_model": f"  {settings.embedding_model}  "},
    )
    assert resp.status_code == 200, resp.text
    assert store._ready is True
    assert _gate_status() == 200


def test_unrelated_field_save_keeps_readiness(ready_store_client):
    """Saving a field outside the embedding identity must never flip the
    store (canary against an over-eager identity comparison, issue #695)."""
    client, store = ready_store_client
    resp = client.put("/api/settings", json={"auto_scan_enabled": True})
    assert resp.status_code == 200, resp.text
    assert store._ready is True
    assert _gate_status() == 200


def test_identity_save_with_store_lacking_ready_attr(ready_store_client):
    """A store object without a ``_ready`` attribute must be left untouched:
    the hook's guard no-ops silently instead of raising or pinning a new
    attribute onto it (issue #695)."""
    client, _real_store = ready_store_client

    class _BareStore:
        pass

    bare = _BareStore()
    app.state.vector_store = bare
    resp = client.put(
        "/api/settings", json={"embedding_model": settings.embedding_model + "-x"}
    )
    assert resp.status_code == 200, resp.text
    assert not hasattr(bare, "_ready")


def test_null_prefix_field_does_not_flip_readiness(ready_store_client):
    """A ``null`` prefix field is filtered out before the apply (only
    non-None values are validated/applied), so a payload of null + the
    unchanged model must not flip the store. The current prefix is made
    non-empty first so the assertion is discriminating: if the None filter
    were ever removed, ``None`` would coerce to ``""`` (via the identity
    tuple's ``str(x or "")``), differ from the configured prefix, and flip
    the store — failing this test (issue #695)."""
    client, store = ready_store_client
    settings.embedding_doc_prefix = "review-pfx"
    resp = client.put(
        "/api/settings",
        json={"embedding_doc_prefix": None, "embedding_model": settings.embedding_model},
    )
    assert resp.status_code == 200, resp.text
    assert store._ready is True
    assert _gate_status() == 200


def test_embedding_dim_is_not_settings_updatable():
    """Structural pin for the identity tuple: ``embedding_dim`` is compared
    by ``validate_schema`` but deliberately absent from
    ``_effective_embedding_identity`` — safe only while it cannot be changed
    through the settings API. If this pin ever fails, the identity tuple in
    ``_effective_embedding_identity`` must grow the dimension (issue #695
    review follow-up)."""
    from app.api.routes.settings import ALLOWED_FIELDS, SettingsUpdate

    assert "embedding_dim" not in ALLOWED_FIELDS
    assert "embedding_dim" not in SettingsUpdate.model_fields
