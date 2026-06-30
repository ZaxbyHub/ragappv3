"""Tests for prompt versioning (FR-007).

Tests the PromptVersionStore (create/activate/list/get/get_active),
exactly-one-active invariant, and PromptBuilderService resolution
(override > active DB > built-in default).
"""

import os
import sqlite3
import tempfile

import pytest

from app.models.database import init_db, run_migrations
from app.services.prompt_builder import PromptBuilderService
from app.services.prompt_store import PromptVersionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db():
    """Create a temporary DB with the full schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        run_migrations(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()
        os.unlink(path)


@pytest.fixture
def store(fresh_db):
    """Return a PromptVersionStore backed by the fresh DB."""
    return PromptVersionStore(fresh_db)


# ---------------------------------------------------------------------------
# PromptVersionStore — create_version
# ---------------------------------------------------------------------------


class TestPromptVersionCreate:
    def test_create_version_inserts_row(self, store):
        pv = store.create_version("v1", "content of v1")
        assert pv.id is not None
        assert pv.version == "v1"
        assert pv.content == "content of v1"
        assert pv.is_active is False

    def test_create_version_with_activate(self, store):
        pv = store.create_version("v1", "content", activate=True)
        assert pv.is_active is True

    def test_create_version_duplicate_raises(self, store):
        store.create_version("v1", "content1")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_version("v1", "content2")

    def test_create_version_sets_created_by(self, store):
        pv = store.create_version("v1", "content", created_by="admin1")
        assert pv.created_by == "admin1"


# ---------------------------------------------------------------------------
# PromptVersionStore — activate
# ---------------------------------------------------------------------------


class TestPromptVersionActivate:
    def test_activate_sets_active(self, store):
        v1 = store.create_version("v1", "content1")
        v2 = store.create_version("v2", "content2")
        assert v1.is_active is False
        assert v2.is_active is False

        activated = store.activate("v2")
        assert activated.version == "v2"
        assert activated.is_active is True

        # v1 should no longer be active
        v1_after = store.get_version("v1")
        assert v1_after.is_active is False

    def test_activate_exactly_one_active(self, store):
        """Activating multiple versions leaves exactly one active."""
        store.create_version("v1", "c1", created_at="2026-01-01T00:00:00Z")
        store.create_version("v2", "c2", created_at="2026-01-02T00:00:00Z")
        store.create_version("v3", "c3", created_at="2026-01-03T00:00:00Z")

        store.activate("v1")
        store.activate("v2")
        store.activate("v3")

        all_versions = store.list_versions()
        active_versions = [v for v in all_versions if v.is_active]
        assert len(active_versions) == 1
        assert active_versions[0].version == "v3"

    def test_activate_nonexistent_raises(self, store):
        with pytest.raises(ValueError, match="No prompt version"):
            store.activate("nonexistent")


# ---------------------------------------------------------------------------
# PromptVersionStore — list_versions / get_active / get_version
# ---------------------------------------------------------------------------


class TestPromptVersionQueries:
    def test_list_versions_ordered_by_created_at_desc(self, store):
        # Pass explicit created_at values to ensure ordering (SQLite DEFAULT
        # is evaluated once per INSERT statement, not per row, so rapid
        # inserts share the same timestamp without explicit values).
        store.create_version("v1", "c1", created_at="2026-01-01T00:00:00Z")
        store.create_version("v2", "c2", created_at="2026-01-02T00:00:00Z")
        store.create_version("v3", "c3", created_at="2026-01-03T00:00:00Z")

        versions = store.list_versions()
        # Most recent first
        assert versions[0].version == "v3"
        assert versions[1].version == "v2"
        assert versions[2].version == "v1"

    def test_get_active_returns_active(self, store):
        store.create_version("v1", "c1")
        store.create_version("v2", "c2", activate=True)

        active = store.get_active()
        assert active is not None
        assert active.version == "v2"

    def test_get_active_returns_none_when_empty(self, store):
        assert store.get_active() is None

    def test_get_version_returns_row(self, store):
        created = store.create_version("v1", "my content")
        retrieved = store.get_version("v1")
        assert retrieved is not None
        assert retrieved.version == "v1"
        assert retrieved.content == "my content"

    def test_get_version_returns_none_for_missing(self, store):
        assert store.get_version("nonexistent") is None


# ---------------------------------------------------------------------------
# PromptBuilderService — resolution precedence
# ---------------------------------------------------------------------------


class TestPromptBuilderServiceResolution:
    def test_explicit_system_prompt_override_highest(self, fresh_db):
        """Constructor system_prompt takes precedence over DB active version."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "db content", activate=True)

        builder = PromptBuilderService(
            system_prompt="explicit override",
            db=fresh_db,
        )
        assert builder.system_prompt == "explicit override"

    def test_active_db_version_used_when_no_override(self, fresh_db):
        """When no explicit override, active DB version is used."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "db active content", activate=True)

        builder = PromptBuilderService(db=fresh_db)
        assert builder.system_prompt == "db active content"

    def test_built_in_default_when_no_active_version(self, fresh_db):
        """When no active version exists, built-in default is used."""
        builder = PromptBuilderService(db=fresh_db)
        default = builder._default_system_prompt()
        assert builder.system_prompt == default

    def test_built_in_default_when_no_db(self):
        """When no db is provided, built-in default is used."""
        builder = PromptBuilderService()
        default = builder._default_system_prompt()
        assert builder.system_prompt == default

    def test_built_in_default_includes_security_boundary(self):
        """Built-in default contains the SECURITY BOUNDARY clause."""
        builder = PromptBuilderService()
        assert "SECURITY BOUNDARY" in builder.system_prompt

    def test_built_in_default_includes_citation_instruction(self):
        """Built-in default contains CITATION_INSTRUCTION."""
        from app.services.prompt_builder import CITATION_INSTRUCTION

        builder = PromptBuilderService()
        assert CITATION_INSTRUCTION in builder.system_prompt

    def test_lazy_resolution_only_hits_db_once(self, fresh_db):
        """The active version is resolved once and cached."""
        store = PromptVersionStore(fresh_db)
        store.create_version("v1", "cached content", activate=True)

        builder = PromptBuilderService(db=fresh_db)
        # First access
        _ = builder.system_prompt
        # Second access
        _ = builder.system_prompt

        # Cache should prevent a second DB hit
        assert builder._cached_active_prompt == "cached content"
        assert builder._cached_active_prompt is not None

    def test_system_prompt_property_returns_string(self, fresh_db):
        """The system_prompt property always returns a string."""
        builder = PromptBuilderService(db=fresh_db)
        assert isinstance(builder.system_prompt, str)
