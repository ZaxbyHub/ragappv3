"""
Tests for main.py catch-all route functionality - simplified version.
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import unittest


class TestMainCatchAllRoute(unittest.TestCase):
    """Test suite for catch-all route in main.py."""

    def test_fileresponse_import_present(self):
        """Test that FileResponse is imported in main.py."""
        # Read the main.py file and check for FileResponse import
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for FileResponse import (can be with other imports)
        self.assertIn('FileResponse', content,
                      "FileResponse import not found in main.py")

    def test_catchall_route_registered(self):
        """Test that catch-all route is registered for unmatched paths."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for catch-all route definition
        self.assertIn('@app.get("/{full_path:path}")', content,
                      "Catch-all route decorator not found in main.py")
        self.assertIn('async def serve_spa(full_path: str):', content,
                      "serve_spa function not found in main.py")

    def test_catchall_serves_index_html(self):
        """Test that catch-all route serves index.html."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check that serve_spa returns FileResponse with index.html
        self.assertIn('FileResponse(static_dir / "index.html")', content,
                      "serve_spa does not return FileResponse with index.html")

    def test_catchall_route_position(self):
        """Test that catch-all route is defined after other routes."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            lines = f.readlines()

        # Find line numbers for key elements
        catchall_line = None
        last_router_line = None

        for i, line in enumerate(lines):
            if '@app.get("/{full_path:path}")' in line:
                catchall_line = i
            if 'app.include_router' in line:
                last_router_line = i

        self.assertIsNotNone(catchall_line, "Catch-all route not found")
        self.assertIsNotNone(last_router_line, "No routers found")
        self.assertTrue(catchall_line > last_router_line,
                        "Catch-all route should be defined after all routers")

    def test_case_insensitive_api_guard(self):
        """Test that API paths are blocked case-insensitively."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for case-insensitive normalization
        self.assertIn('normalized_path = full_path.lower()', content,
                      "Case-insensitive normalization not found")
        self.assertIn('normalized_path == "api"', content,
                      "API path check not found")
        self.assertIn('normalized_path.startswith("api/")', content,
                      "API prefix check not found")

    def test_case_insensitive_assets_guard(self):
        """Test that assets paths are blocked case-insensitively."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for case-insensitive assets blocking
        self.assertIn('normalized_path == "assets"', content,
                      "Assets path check not found")
        self.assertIn('normalized_path.startswith("assets/")', content,
                      "Assets prefix check not found")

    def test_assets_mount_configured(self):
        """Test that /assets is mounted as static files."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for assets mount configuration
        self.assertIn('app.mount(', content,
                      "Assets mount not found")
        self.assertIn('"/assets"', content,
                      "Assets mount path not found")
        self.assertIn('StaticFiles(directory=str(static_dir / "assets")', content,
                      "Assets directory configuration not found")

    def test_httpexception_import_present(self):
        """Test that HTTPException is imported for 404 responses."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for HTTPException import
        self.assertIn('HTTPException', content,
                      "HTTPException import not found in main.py")

    def test_catchall_returns_404_for_api_paths(self):
        """Test that catch-all raises 404 for API paths."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check for HTTPException with 404 status
        self.assertIn('raise HTTPException(status_code=404', content,
                      "404 HTTPException not raised for API/Assets paths")

    def test_catchall_serves_index_for_frontend_routes(self):
        """Test that catch-all serves index.html for frontend routes."""
        main_file = os.path.join(os.path.dirname(__file__), '..', 'app', 'main.py')
        with open(main_file, 'r') as f:
            content = f.read()

        # Check that serve_spa returns FileResponse (not HTTPException)
        # The return statement should come after the if block that raises for API/assets
        lines = content.split('\n')
        serve_spa_section = False
        has_return_after_check = False

        for i, line in enumerate(lines):
            if 'async def serve_spa(full_path: str):' in line:
                serve_spa_section = True
            elif serve_spa_section and 'return FileResponse' in line:
                has_return_after_check = True
                break
            elif serve_spa_section and (line.strip().startswith('def ') or line.strip().startswith('@app.get')):
                break

        self.assertTrue(has_return_after_check,
                        "serve_spa should return FileResponse for frontend routes")


if __name__ == "__main__":
    unittest.main()
