"""
Tests for main.py catch-all route functionality.

Mix of runtime HTTP assertions (via FastAPI TestClient) and structural checks.

Runtime-testable:
  - The app boots and registered API routes respond.
  - Unmatched API paths return 404 (not 500, not 405).

Structurally-checked (the catch-all ``serve_spa`` route is only registered when
``/app/static`` exists at import time, which it does not in CI, so its behavior
cannot be exercised at runtime there):
  - ``serve_spa`` is defined, returns ``FileResponse(static_dir / "index.html")``.
  - Case-insensitive ``api``/``assets`` guards raise 404.
  - The catch-all is registered after all routers (so it cannot shadow API routes).
  - The assets mount and required imports are present.
"""
import os
import sys
import tempfile

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies for CI
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import unittest


def _read_main_py():
    """Return the source text of app/main.py for structural assertions."""
    main_file = os.path.join(os.path.dirname(__file__), "..", "app", "main.py")
    with open(main_file) as f:
        return f.read()


class TestMainCatchAllStructural(unittest.TestCase):
    """Structural checks for the catch-all route registration in main.py.

    The catch-all ``serve_spa`` is only registered at import time when
    ``/app/static`` exists (it does not in CI), so its body is verified
    structurally here; the runtime-observable behaviors are covered in
    TestMainCatchAllRuntime below.
    """

    def test_fileresponse_import_present(self):
        """FileResponse must be imported so serve_spa can return it."""
        self.assertIn("FileResponse", _read_main_py())

    def test_catchall_route_registered(self):
        """Catch-all route decorator and serve_spa must be present."""
        content = _read_main_py()
        self.assertIn('@app.get("/{full_path:path}")', content)
        self.assertIn("async def serve_spa(full_path: str):", content)

    def test_catchall_serves_index_html(self):
        """serve_spa must return FileResponse(static_dir / 'index.html')."""
        self.assertIn(
            'FileResponse(static_dir / "index.html")', _read_main_py()
        )

    def test_catchall_route_position(self):
        """Catch-all route must be defined after all include_router calls."""
        lines = _read_main_py().splitlines()
        catchall_line = None
        last_router_line = None
        for i, line in enumerate(lines):
            if '@app.get("/{full_path:path}")' in line:
                catchall_line = i
            if "app.include_router" in line:
                last_router_line = i
        self.assertIsNotNone(catchall_line, "Catch-all route not found")
        self.assertIsNotNone(last_router_line, "No routers found")
        self.assertGreater(
            catchall_line,
            last_router_line,
            "Catch-all route should be defined after all routers",
        )

    def test_case_insensitive_api_guard(self):
        """API paths must be blocked case-insensitively."""
        content = _read_main_py()
        self.assertIn("normalized_path = full_path.lower()", content)
        self.assertIn('normalized_path == "api"', content)
        self.assertIn('normalized_path.startswith("api/")', content)

    def test_case_insensitive_assets_guard(self):
        """Assets paths must be blocked case-insensitively."""
        content = _read_main_py()
        self.assertIn('normalized_path == "assets"', content)
        self.assertIn('normalized_path.startswith("assets/")', content)

    def test_assets_mount_configured(self):
        """/assets must be mounted as static files."""
        content = _read_main_py()
        self.assertIn("app.mount(", content)
        self.assertIn('"/assets"', content)
        self.assertIn('StaticFiles(directory=str(static_dir / "assets")', content)

    def test_httpexception_import_present(self):
        """HTTPException must be imported for 404 responses."""
        self.assertIn("HTTPException", _read_main_py())

    def test_catchall_returns_404_for_api_paths(self):
        """serve_spa must raise HTTPException(status_code=404) for api/assets."""
        self.assertIn(
            "raise HTTPException(status_code=404", _read_main_py()
        )

    def test_catchall_serves_index_for_frontend_routes(self):
        """serve_spa must return FileResponse for frontend routes."""
        lines = _read_main_py().split("\n")
        serve_spa_section = False
        has_return_after_check = False
        for line in lines:
            if "async def serve_spa(full_path: str):" in line:
                serve_spa_section = True
            elif serve_spa_section and "return FileResponse" in line:
                has_return_after_check = True
                break
            elif serve_spa_section and (
                line.strip().startswith("def ")
                or line.strip().startswith("@app.get")
            ):
                break
        self.assertTrue(
            has_return_after_check,
            "serve_spa should return FileResponse for frontend routes",
        )


class TestMainCatchAllRuntime(unittest.TestCase):
    """Runtime HTTP assertions against the real app via TestClient.

    These exercise behaviors that hold regardless of whether the catch-all
    ``serve_spa`` is registered (it is only registered when /app/static exists).
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client = TestClient(app)

    def test_unmatched_api_path_returns_404(self):
        """The app must boot and FastAPI routing must be active.

        An unmatched path under /api/ must yield a deterministic 404 (not a
        connection error, not a 500, not a hang). This proves the app object is
        wired and the router is serving — routes that need app.state
        (db_pool / csrf_manager) are exercised in dedicated suites that set up
        that state.
        """
        resp = self.client.get("/api/this-route-does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_top_level_path_returns_404_when_no_static_dir(self):
        """Without /app/static the catch-all is not registered, so unknown
        top-level paths return 404 (FastAPI default) rather than serving a SPA."""
        # In CI /app/static does not exist, so serve_spa is not registered and
        # this path is simply unmatched.
        resp = self.client.get("/some/frontend/route")
        # 404 is the correct outcome when no catch-all is registered.
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
