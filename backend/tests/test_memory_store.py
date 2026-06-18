"""Tests for memory_store module."""

import os
import tempfile
import unittest
from pathlib import Path

import pydantic

from app.config import Settings
from app.models.database import SQLiteConnectionPool, init_db
from app.services.memory_store import MemoryRecord, MemoryStore, MemoryStoreError


class TestMemoryStore(unittest.TestCase):
    """Test cases for MemoryStore class."""

    def setUp(self):
        """Set up test database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(str(self.db_path))
        self.pool = SQLiteConnectionPool(str(self.db_path), max_size=2)
        self.store = MemoryStore(pool=self.pool)

    def tearDown(self):
        """Clean up test database."""
        self.pool.close_all()
        if self.db_path.exists():
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_add_memory_empty_string_raises_error(self):
        """Test that empty string raises MemoryStoreError."""
        with self.assertRaises(MemoryStoreError) as ctx:
            self.store.add_memory("")
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_add_memory_whitespace_only_raises_error(self):
        """Test that whitespace-only string raises MemoryStoreError."""
        with self.assertRaises(MemoryStoreError) as ctx:
            self.store.add_memory("   \t\n  ")
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_add_memory_none_raises_error(self):
        """Test that None input raises MemoryStoreError."""
        with self.assertRaises(MemoryStoreError) as ctx:
            self.store.add_memory(None)
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_add_memory_content_only(self):
        """Test that add_memory works with only content provided."""
        result = self.store.add_memory(content="Simple memory content")

        self.assertIsInstance(result, MemoryRecord)
        self.assertEqual(result.content, "Simple memory content")
        self.assertIsNone(result.category)
        self.assertIsNone(result.tags)
        self.assertIsNone(result.source)
        self.assertIsNotNone(result.id)
        self.assertIsNotNone(result.created_at)

    def test_add_memory_with_category_tags_source(self):
        """Test that add_memory stores category, tags, and source correctly."""
        result = self.store.add_memory(
            content="Test memory content",
            category="test_category",
            tags='["tag1", "tag2"]',
            source="test_source"
        )

        self.assertIsInstance(result, MemoryRecord)
        self.assertEqual(result.content, "Test memory content")
        self.assertEqual(result.category, "test_category")
        self.assertEqual(result.tags, '["tag1", "tag2"]')
        self.assertEqual(result.source, "test_source")
        self.assertIsNotNone(result.id)
        self.assertIsNotNone(result.created_at)

    def test_search_memories_empty_string_returns_empty_list(self):
        """Test that empty query returns empty list."""
        result = self.store.search_memories("")
        self.assertEqual(result, [])

    def test_search_memories_whitespace_only_returns_empty_list(self):
        """Test that whitespace-only query returns empty list."""
        result = self.store.search_memories("   \t\n  ")
        self.assertEqual(result, [])

    def test_search_memories_none_returns_empty_list(self):
        """Test that None query returns empty list."""
        result = self.store.search_memories(None)
        self.assertEqual(result, [])

    def test_search_memories_returns_actual_results(self):
        """Test that search_memories finds memories that were added."""
        # Add some memories
        self.store.add_memory(content="Meeting scheduled for Monday at 10am")
        self.store.add_memory(content="Project deadline is next Friday")
        self.store.add_memory(content="Team lunch on Wednesday")

        # Search for a term that should match
        results = self.store.search_memories("meeting")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, "Meeting scheduled for Monday at 10am")

        # Search for another term
        results = self.store.search_memories("deadline")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, "Project deadline is next Friday")

    def test_search_memories_respects_limit(self):
        """Test that search_memories respects the limit parameter."""
        # Add multiple memories with similar content
        self.store.add_memory(content="Important task number one")
        self.store.add_memory(content="Important task number two")
        self.store.add_memory(content="Important task number three")
        self.store.add_memory(content="Important task number four")
        self.store.add_memory(content="Important task number five")

        # Search with limit of 2
        results = self.store.search_memories("important", limit=2)
        self.assertEqual(len(results), 2)

        # Search with limit of 3
        results = self.store.search_memories("important", limit=3)
        self.assertEqual(len(results), 3)

        # Search with default limit (5)
        results = self.store.search_memories("important")
        self.assertEqual(len(results), 5)

    def test_detect_memory_intent_remember_that_pattern(self):
        """Test 'remember that' pattern detection."""
        text = "Please remember that the meeting is at 3pm."
        result = self.store.detect_memory_intent(text)
        self.assertEqual(result, "the meeting is at 3pm")

    def test_detect_memory_intent_dont_forget_pattern(self):
        """Test 'don't forget' pattern detection."""
        text = "Don't forget to buy milk"
        result = self.store.detect_memory_intent(text)
        self.assertEqual(result, "to buy milk")

    def test_detect_memory_intent_keep_in_mind_pattern(self):
        """'keep in mind that X' should capture the body without the
        leading 'that' connective. Improved capture introduced in P2.5."""
        text = "Keep in mind that the deadline is Friday."
        result = self.store.detect_memory_intent(text)
        self.assertEqual(result, "the deadline is Friday")

    def test_detect_memory_intent_note_that_pattern(self):
        """Test 'note that' pattern detection."""
        text = "Note that the server will be down for maintenance."
        result = self.store.detect_memory_intent(text)
        self.assertEqual(result, "the server will be down for maintenance")

    def test_detect_memory_intent_case_insensitive(self):
        """Test that patterns are case-insensitive."""
        text = "REMEMBER THAT this is important"
        result = self.store.detect_memory_intent(text)
        self.assertEqual(result, "this is important")

    def test_detect_memory_intent_no_match_returns_none(self):
        """Test that text with no pattern returns None."""
        text = "What is the weather today?"
        result = self.store.detect_memory_intent(text)
        self.assertIsNone(result)

    def test_detect_memory_intent_empty_text_returns_none(self):
        """Test that empty text returns None."""
        result = self.store.detect_memory_intent("")
        self.assertIsNone(result)

    def test_detect_memory_intent_whitespace_only_returns_none(self):
        """Test that whitespace-only text returns None."""
        result = self.store.detect_memory_intent("   \t\n  ")
        self.assertIsNone(result)


    def test_memory_store_pool_size_rejects_below_five(self):
        """Test that Settings rejects memory_store_pool_size values below 5."""
        with self.assertRaises(pydantic.ValidationError):
            Settings(memory_store_pool_size=0)
        with self.assertRaises(pydantic.ValidationError):
            Settings(memory_store_pool_size=4)

    def test_memory_store_pool_size_accepts_five(self):
        """Test that Settings accepts memory_store_pool_size=5 (minimum valid value)."""
        s = Settings(memory_store_pool_size=5)
        self.assertEqual(s.memory_store_pool_size, 5)

    def test_memory_store_pool_size_accepts_twenty_five(self):
        """Test that Settings accepts memory_store_pool_size=25."""
        s = Settings(memory_store_pool_size=25)
        self.assertEqual(s.memory_store_pool_size, 25)

    def test_default_memory_store_pool_size_is_ten(self):
        """Test that the default MemoryStore pool size matches settings.memory_store_pool_size (default 10)."""
        from app.config import settings
        from app.models.database import _pool_cache

        store = MemoryStore()
        self.assertEqual(store.pool.max_size, settings.memory_store_pool_size)
        self.assertEqual(store.pool.max_size, 10)
        self.assertIsNot(store.pool, _pool_cache.get(str(settings.sqlite_path)))
        try:
            store.close_all()
        finally:
            from app.services.memory_store import MemoryStore as _MemoryStore
            _cached = _pool_cache.get(str(settings.sqlite_path))
            if _cached is not None:
                _cached.close_all()

    def test_memory_store_pool_size_is_configurable_via_settings(self):
        """Test that MemoryStore respects an overridden memory_store_pool_size setting."""
        from app.config import settings
        from app.models.database import _pool_cache

        original = settings.memory_store_pool_size
        try:
            settings.memory_store_pool_size = 25
            store = MemoryStore()
            self.assertEqual(store.pool.max_size, 25)
            self.assertIsNot(store.pool, _pool_cache.get(str(settings.sqlite_path)))
        finally:
            settings.memory_store_pool_size = original
            try:
                store.close_all()
            finally:
                from app.services.memory_store import MemoryStore as _MemoryStore
                _cached = _pool_cache.get(str(settings.sqlite_path))
                if _cached is not None:
                    _cached.close_all()


if __name__ == "__main__":
    unittest.main()
