"""Tests for wiki settings reload behavior in lifespan.py.

Validates that:
1. wiki_enabled and related wiki_* flags are present in NEW_DIRECT_KEYS
2. When these keys have persisted values in the database, they are loaded into settings at startup
3. The behavior matches the KMS keys (kms_enabled, kms_compile_on_ingest)
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestWikiKeysInNewDirectKeys:
    """Test that wiki keys are present in NEW_DIRECT_KEYS list in lifespan.py."""

    def test_wiki_enabled_in_new_direct_keys(self):
        """wiki_enabled should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        # Find the NEW_DIRECT_KEYS list
        assert 'NEW_DIRECT_KEYS' in source, "NEW_DIRECT_KEYS should exist in lifespan.py"

        # Check wiki_enabled is in the list
        assert '"wiki_enabled"' in source or "'wiki_enabled'" in source, \
            "wiki_enabled should be in NEW_DIRECT_KEYS"

    def test_wiki_compile_on_ingest_in_new_direct_keys(self):
        """wiki_compile_on_ingest should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        assert '"wiki_compile_on_ingest"' in source or "'wiki_compile_on_ingest'" in source, \
            "wiki_compile_on_ingest should be in NEW_DIRECT_KEYS"

    def test_wiki_compile_on_query_in_new_direct_keys(self):
        """wiki_compile_on_query should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        assert '"wiki_compile_on_query"' in source or "'wiki_compile_on_query'" in source, \
            "wiki_compile_on_query should be in NEW_DIRECT_KEYS"

    def test_wiki_compile_after_indexing_in_new_direct_keys(self):
        """wiki_compile_after_indexing should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        assert '"wiki_compile_after_indexing"' in source or "'wiki_compile_after_indexing'" in source, \
            "wiki_compile_after_indexing should be in NEW_DIRECT_KEYS"


class TestKmsKeysInNewDirectKeys:
    """Verify KMS keys are also in NEW_DIRECT_KEYS for behavior comparison."""

    def test_kms_enabled_in_new_direct_keys(self):
        """kms_enabled should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        assert '"kms_enabled"' in source or "'kms_enabled'" in source, \
            "kms_enabled should be in NEW_DIRECT_KEYS"

    def test_kms_compile_on_ingest_in_new_direct_keys(self):
        """kms_compile_on_ingest should be listed in NEW_DIRECT_KEYS for persistence reload."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        assert '"kms_compile_on_ingest"' in source or "'kms_compile_on_ingest'" in source, \
            "kms_compile_on_ingest should be in NEW_DIRECT_KEYS"


class TestLoadPersistedSettingsBehavior:
    """Test _load_persisted_settings correctly loads wiki and KMS keys."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database with settings_kv table."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test_settings.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        yield str(db_path)
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def mock_settings(self):
        """Create a mock settings object with wiki and KMS fields."""
        from app.config import Settings
        return Settings()

    def test_load_wiki_enabled_true(self, temp_db, mock_settings):
        """_load_persisted_settings should load wiki_enabled=true from DB."""
        # Insert persisted value
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('wiki_enabled', 'true')")
        conn.commit()
        conn.close()

        # Mock settings to only have the fields we care about
        mock_settings.wiki_enabled = False  # Default

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.wiki_enabled is True

    def test_load_wiki_enabled_false(self, temp_db, mock_settings):
        """_load_persisted_settings should load wiki_enabled=false from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('wiki_enabled', 'false')")
        conn.commit()
        conn.close()

        mock_settings.wiki_enabled = True  # Default

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.wiki_enabled is False

    def test_load_wiki_compile_on_ingest_true(self, temp_db, mock_settings):
        """_load_persisted_settings should load wiki_compile_on_ingest=true from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('wiki_compile_on_ingest', 'true')")
        conn.commit()
        conn.close()

        mock_settings.wiki_compile_on_ingest = False

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.wiki_compile_on_ingest is True

    def test_load_wiki_compile_on_query_false(self, temp_db, mock_settings):
        """_load_persisted_settings should load wiki_compile_on_query=false from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('wiki_compile_on_query', 'false')")
        conn.commit()
        conn.close()

        mock_settings.wiki_compile_on_query = True

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.wiki_compile_on_query is False

    def test_load_wiki_compile_after_indexing_true(self, temp_db, mock_settings):
        """_load_persisted_settings should load wiki_compile_after_indexing=true from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('wiki_compile_after_indexing', 'true')")
        conn.commit()
        conn.close()

        mock_settings.wiki_compile_after_indexing = False

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.wiki_compile_after_indexing is True


class TestLoadKmsSettingsBehavior:
    """Test _load_persisted_settings correctly loads KMS keys (for comparison)."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database with settings_kv table."""
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test_settings.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        yield str(db_path)
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def mock_settings(self):
        """Create a mock settings object with KMS fields."""
        from app.config import Settings
        return Settings()

    def test_load_kms_enabled_true(self, temp_db, mock_settings):
        """_load_persisted_settings should load kms_enabled=true from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('kms_enabled', 'true')")
        conn.commit()
        conn.close()

        mock_settings.kms_enabled = False

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.kms_enabled is True

    def test_load_kms_enabled_false(self, temp_db, mock_settings):
        """_load_persisted_settings should load kms_enabled=false from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('kms_enabled', 'false')")
        conn.commit()
        conn.close()

        mock_settings.kms_enabled = True

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.kms_enabled is False

    def test_load_kms_compile_on_ingest_true(self, temp_db, mock_settings):
        """_load_persisted_settings should load kms_compile_on_ingest=true from DB."""
        conn = sqlite3.connect(temp_db)
        conn.execute("INSERT INTO settings_kv (key, value) VALUES ('kms_compile_on_ingest', 'true')")
        conn.commit()
        conn.close()

        mock_settings.kms_compile_on_ingest = False

        with patch('app.lifespan.settings', mock_settings):
            from app.lifespan import _load_persisted_settings
            _load_persisted_settings(temp_db)

        assert mock_settings.kms_compile_on_ingest is True


class TestWikiAndKmsParity:
    """Test that wiki and KMS keys have symmetric reload behavior."""

    def test_wiki_and_kms_keys_have_same_types_in_settings(self):
        """Both wiki_* and kms_* boolean flags should exist in Settings with bool type."""
        from app.config import Settings

        # Create a settings instance to check defaults
        settings = Settings()

        # Wiki keys should exist and be bool
        assert hasattr(settings, 'wiki_enabled')
        assert isinstance(settings.wiki_enabled, bool)

        assert hasattr(settings, 'wiki_compile_on_ingest')
        assert isinstance(settings.wiki_compile_on_ingest, bool)

        assert hasattr(settings, 'wiki_compile_on_query')
        assert isinstance(settings.wiki_compile_on_query, bool)

        assert hasattr(settings, 'wiki_compile_after_indexing')
        assert isinstance(settings.wiki_compile_after_indexing, bool)

        # KMS keys should exist and be bool
        assert hasattr(settings, 'kms_enabled')
        assert isinstance(settings.kms_enabled, bool)

        assert hasattr(settings, 'kms_compile_on_ingest')
        assert isinstance(settings.kms_compile_on_ingest, bool)

    def test_wiki_and_kms_default_values_are_true(self):
        """Wiki and KMS compile flags should default to True for safe operation."""
        from app.config import Settings

        settings = Settings()

        # Wiki compile flags should default to True
        assert settings.wiki_enabled is True
        assert settings.wiki_compile_on_ingest is True
        assert settings.wiki_compile_on_query is True
        assert settings.wiki_compile_after_indexing is True

        # KMS flags should default to True
        assert settings.kms_enabled is True
        assert settings.kms_compile_on_ingest is True


class TestNewDirectKeysCompleteness:
    """Test that NEW_DIRECT_KEYS contains all expected keys for a given category."""

    def test_all_wiki_keys_present(self):
        """All four wiki keys should be present in NEW_DIRECT_KEYS source."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        # Find NEW_DIRECT_KEYS list content
        import re
        match = re.search(r'NEW_DIRECT_KEYS\s*=\s*\[(.*?)\]', source, re.DOTALL)
        assert match, "NEW_DIRECT_KEYS list should exist"

        keys_content = match.group(1)

        expected_wiki_keys = [
            'wiki_enabled',
            'wiki_compile_on_ingest',
            'wiki_compile_on_query',
            'wiki_compile_after_indexing',
        ]

        for key in expected_wiki_keys:
            assert f'"{key}"' in keys_content or f"'{key}'" in keys_content, \
                f"{key} should be in NEW_DIRECT_KEYS"

    def test_all_kms_keys_present(self):
        """All KMS keys should be present in NEW_DIRECT_KEYS source."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        import re
        match = re.search(r'NEW_DIRECT_KEYS\s*=\s*\[(.*?)\]', source, re.DOTALL)
        assert match, "NEW_DIRECT_KEYS list should exist"

        keys_content = match.group(1)

        expected_kms_keys = [
            'kms_enabled',
            'kms_compile_on_ingest',
        ]

        for key in expected_kms_keys:
            assert f'"{key}"' in keys_content or f"'{key}'" in keys_content, \
                f"{key} should be in NEW_DIRECT_KEYS"

    def test_new_direct_keys_section_has_both_wiki_and_kms(self):
        """The NEW_DIRECT_KEYS section should have KMS keys before wiki keys (as added)."""
        lifespan_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'lifespan.py')
        with open(lifespan_path, 'r') as f:
            source = f.read()

        import re
        match = re.search(r'NEW_DIRECT_KEYS\s*=\s*\[(.*?)\]', source, re.DOTALL)
        assert match, "NEW_DIRECT_KEYS list should exist"

        keys_content = match.group(1)

        # Verify both wiki and kms keys are present
        assert 'kms_enabled' in keys_content, "kms_enabled should be in NEW_DIRECT_KEYS"
        assert 'wiki_enabled' in keys_content, "wiki_enabled should be in NEW_DIRECT_KEYS"

        # Find positions to verify ordering (kms should come before wiki in the list)
        kms_pos = keys_content.find('kms_enabled')
        wiki_pos = keys_content.find('wiki_enabled')
        assert kms_pos < wiki_pos, "kms_enabled should appear before wiki_enabled in NEW_DIRECT_KEYS"
