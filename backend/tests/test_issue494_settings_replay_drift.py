"""
Issue #494 acceptance checks — AC12 (CONFIG-003): settings persistence drift.

Root cause (verified at base a543361):
- ``app/api/routes/settings.py`` ALLOWED_FIELDS lists every field the
  settings API persists into the ``settings_kv`` table.
- ``app/lifespan.py`` ``_load_persisted_settings`` replays only
  ``legacy_keys`` (6 keys) + ``NEW_DIRECT_KEYS`` (63 keys at base) back onto
  the settings singleton at startup.
- The two hand-maintained lists have drifted: fields saved through
  PUT/POST /api/settings are silently reverted to their defaults on the next
  startup because the replay loop never looks at them.

Nodes:
- AC12a (DISCRIMINATING): mechanical drift check — every ALLOWED_FIELDS
  entry (minus an explicit, currently-empty exclusion list) must be covered
  by the startup replay set.
- AC12b (DISCRIMINATING): end-to-end roundtrip — save sentinel values via
  the real settings API, then run the real ``_load_persisted_settings``
  against a fresh Settings instance and assert the values survived.
"""

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors the other backend test files)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto

from fastapi.testclient import TestClient  # noqa: E402

# Set up a temporary database before importing app (same shape as test_settings.py)
TEST_DB_PATH = None
TEST_DATA_DIR = None


def _setup_test_db():
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"
    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


_setup_test_db()

from app.api.routes.settings import ALLOWED_FIELDS  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# Repo backend root (backend/), used to read the production lifespan module.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _startup_replay_set():
    """Return the set of settings_kv keys ``_load_persisted_settings`` replays.

    ``NEW_DIRECT_KEYS`` is a function-local list and ``legacy_keys`` a
    function-local dict inside ``app/lifespan.py::_load_persisted_settings``,
    so they cannot be imported directly. The faithful mechanical extraction
    is to read the real module source with ``ast`` and collect the names
    assigned to those identifiers, wherever in the module they are assigned.
    If a fix renames/removes these lists (e.g. moves to a generic replay),
    this extraction must be updated in the same change.
    """
    source_path = _BACKEND_ROOT / "app" / "lifespan.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    replay = set()
    assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not (isinstance(target, ast.Name)
                        and target.id in ("NEW_DIRECT_KEYS", "legacy_keys")):
                    continue
                assigned.add(target.id)
                if isinstance(node.value, ast.List):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            replay.add(elt.value)
                elif isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            replay.add(key.value)
    if "NEW_DIRECT_KEYS" not in assigned and "legacy_keys" not in assigned:
        raise AssertionError(
            "startup replay extraction failed: no legacy_keys/NEW_DIRECT_KEYS "
            "assignment found in app/lifespan.py — update this check alongside "
            "the refactor"
        )
    return replay


# Explicitly excluded ALLOWED_FIELDS entries that are intentionally NOT
# replayed at startup. Empty today: every ALLOWED_FIELDS entry is a
# functional, user-saveable setting (the settings API persists exactly these
# keys), so every one of them must survive a restart.
REPLAY_EXCLUSIONS = ()


class TestSettingsReplayDrift(unittest.TestCase):
    """AC12a — DISCRIMINATING drift check between save-path and replay-path."""

    def test_ac12a_startup_replay_covers_every_saveable_field(self):
        """Every ALLOWED_FIELDS entry must be covered by the startup replay set.

        At the pre-fix base this fails listing the drifted field names
        (currently: ingestion_llm_mode, instant_skip_* x4, wiki_lint_enabled,
        and the wiki_llm_curator_* family).
        """
        allowed = [f for f in ALLOWED_FIELDS if f not in REPLAY_EXCLUSIONS]
        replay = _startup_replay_set()

        missing = sorted(set(allowed) - replay)
        extra = sorted(replay - set(ALLOWED_FIELDS))
        # Sanity: the extraction found the real lists (they overlap heavily).
        self.assertGreater(len(replay & set(ALLOWED_FIELDS)), 50)

        print("AC12a CHECK: FAIL")
        self.assertEqual(
            missing,
            [],
            f"{len(missing)} ALLOWED_FIELDS entries are saved by the settings "
            f"API (settings_kv) but never replayed at startup by "
            f"_load_persisted_settings — they silently revert to defaults on "
            f"restart: {missing}",
        )
        self.assertEqual(
            extra,
            [],
            f"startup replay set contains keys that are not saveable via the "
            f"API (stale replay entries): {extra}",
        )


class TestSettingsStartupRoundtrip(unittest.TestCase):
    """AC12b — DISCRIMINATING end-to-end roundtrip through the real API + replay."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        from app.api.deps import get_db
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        self._get_db = get_db
        # Snapshot the mutable singleton so this test restores it.
        self._snapshot = {f: getattr(settings, f) for f in ALLOWED_FIELDS}

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        for field, value in self._snapshot.items():
            setattr(settings, field, value)
        self._test_pool.close_all()

    def test_ac12b_saved_fields_survive_startup_replay(self):
        """Fields saved via PUT /api/settings must survive a startup replay.

        Sentinels (both non-default):
        - ingestion_llm_mode = "thinking" (default "instant")
        - instant_skip_query_transformation = False (default True)

        At the pre-fix base neither key is replayed, so the fresh instance
        keeps its defaults and this check fails.
        """
        payload = {
            "ingestion_llm_mode": "thinking",
            "instant_skip_query_transformation": False,
        }
        resp = self.client.put("/api/settings", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["ingestion_llm_mode"], "thinking")
        # Persisted rows really are in settings_kv for both keys.
        conn = self._test_pool.get_connection()
        try:
            rows = {
                r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM settings_kv").fetchall()
            }
        finally:
            self._test_pool.release_connection(conn)
        self.assertIn("ingestion_llm_mode", rows)
        self.assertIn("instant_skip_query_transformation", rows)

        # Fresh Settings instance + the real startup replay against it.
        import app.lifespan as lifespan_mod
        from app.config import Settings
        from app.lifespan import _load_persisted_settings

        fresh = Settings()
        # Sanity: the sentinels are genuinely non-default on a fresh instance,
        # so a revert is observable.
        self.assertEqual(fresh.ingestion_llm_mode, "instant")
        self.assertTrue(fresh.instant_skip_query_transformation)

        with patch.object(lifespan_mod, "settings", fresh):
            _load_persisted_settings(str(TEST_DB_PATH))

        print("AC12b CHECK: FAIL")
        self.assertEqual(
            fresh.ingestion_llm_mode,
            "thinking",
            "startup replay did not restore ingestion_llm_mode saved via the "
            "settings API (field not in the replay set — silent revert)",
        )
        self.assertIs(
            fresh.instant_skip_query_transformation,
            False,
            "startup replay did not restore instant_skip_query_transformation "
            "saved via the settings API (field not in the replay set — silent "
            "revert)",
        )


if __name__ == "__main__":
    unittest.main()
