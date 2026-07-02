"""Shared test schema constants for org/vault/group/auth integration tests.

This module is NOT collected by pytest (no test_ prefix, no _test suffix).
Import this instead of duplicating TEST_SCHEMA across multiple test files.

Tables included:
    users, organizations, org_members, groups, group_members,
    vaults, vault_members, vault_group_access, access_token_denylist

Indexes included for all tables that are referenced in WHERE/JOIN clauses.

Usage:
    from backend.tests.schema_constants import TEST_SCHEMA, build_test_schema

    # In a test fixture:
    conn.executescript(TEST_SCHEMA)

    # Or use the helper:
    build_test_schema(conn)
"""

import sqlite3

# ------------------------------------------------------------------
# The canonical TEST_SCHEMA used by the org/vault/group/auth integration
# test suite.  This is the fullest version (used by test_groups_auth.py,
# test_organizations_routes.py, test_groups_policy_wiring.py,
# test_vault_group_access_routes.py, test_vault_members_integrity_error.py,
# test_vault_org_routes_adversarial.py, and others).
#
# Schema variations in individual test files are handled via supplemental
# SQL appended at usage time (see PRESERVE_VARIATIONS note below).
# ------------------------------------------------------------------

TEST_SCHEMA = """
-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    hashed_password TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('superadmin','admin','member','viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP,
    password_changed_at REAL NOT NULL DEFAULT 0
);

-- Organizations table
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    slug TEXT UNIQUE,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- Organization members
CREATE TABLE IF NOT EXISTS org_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member')),
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(org_id, user_id)
);

-- Organization invites
CREATE TABLE IF NOT EXISTS org_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('admin','member')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by_user_id INTEGER,
    accepted_at TEXT,
    accepted_by_user_id INTEGER,
    revoked_at TEXT,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (accepted_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_org_invites_token_hash ON org_invites(token_hash);

-- Groups table
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE(org_id, name)
);

-- Group members
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(group_id, user_id)
);

-- Vaults table
CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    org_id INTEGER,
    visibility TEXT DEFAULT 'private' CHECK (visibility IN ('private', 'org', 'public')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enrichment_enabled INTEGER,
    FOREIGN KEY (owner_id) REFERENCES users(id),
    FOREIGN KEY (org_id) REFERENCES organizations(id)
);

-- Vault members table
CREATE TABLE IF NOT EXISTS vault_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, user_id)
);

-- Vault group access
CREATE TABLE IF NOT EXISTS vault_group_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    permission TEXT NOT NULL DEFAULT 'read' CHECK (permission IN ('read','write','admin')),
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE(vault_id, group_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_vault_members_vault_id ON vault_members(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_members_user_id ON vault_members(user_id);
CREATE INDEX IF NOT EXISTS idx_org_members_org_id ON org_members(org_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user_id ON org_members(user_id);
CREATE INDEX IF NOT EXISTS idx_groups_org_id ON groups(org_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_vault_id ON vault_group_access(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_group_id ON vault_group_access(group_id);

-- Access-token denylist (task 1.2 table)
CREATE TABLE IF NOT EXISTS access_token_denylist (
    jti TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_token_denylist_expires ON access_token_denylist(expires_at);

-- Service accounts (task 1.3 table)
CREATE TABLE IF NOT EXISTS service_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_rotated_at TEXT,
    revoked_at TEXT,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_service_accounts_key_hash ON service_accounts(key_hash);

-- Prompt versioning (FR-007)
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    is_active INTEGER NOT NULL DEFAULT 0,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_is_active ON prompt_versions(is_active);

-- Per-organization prompt overrides (FR-007 part 2)
CREATE TABLE IF NOT EXISTS prompt_org_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL UNIQUE,
    version TEXT NOT NULL,
    set_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    set_by TEXT,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_prompt_org_overrides_org_id ON prompt_org_overrides(org_id);

-- A/B experiments for prompt variants (FR-007 part 3)
CREATE TABLE IF NOT EXISTS prompt_ab_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    control_version TEXT NOT NULL,
    challenger_version TEXT NOT NULL,
    split_pct INTEGER NOT NULL DEFAULT 50
        CHECK (split_pct >= 0 AND split_pct <= 100),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'ended')),
    winner TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS prompt_ab_exposures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL
        REFERENCES prompt_ab_experiments(id) ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    assigned_variant TEXT NOT NULL
        CHECK (assigned_variant IN ('control', 'challenger')),
    exposed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(experiment_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_prompt_ab_exposures_experiment_id ON prompt_ab_exposures(experiment_id);

-- Files table (minimal for file-related test queries)
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    file_type TEXT,
    chunk_count INTEGER DEFAULT 0,
    chunks_failed INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'indexed', 'error')),
    error_message TEXT,
    source TEXT DEFAULT 'upload',
    email_subject TEXT,
    email_sender TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    modified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    document_date TEXT,
    supersedes_file_id INTEGER,
    ingestion_version INTEGER DEFAULT 1,
    phase TEXT,
    phase_message TEXT,
    progress_percent REAL,
    processed_units INTEGER,
    total_units INTEGER,
    unit_label TEXT,
    phase_started_at TIMESTAMP,
    processing_started_at TIMESTAMP,
    wiki_pending INTEGER NOT NULL DEFAULT 0,
    enrichment_status TEXT,
    enrichment_error TEXT,
    enrichment_updated_at TIMESTAMP,
    enrichment_enabled INTEGER,
    -- NULL = inherit vault/global; 1 = on; 0 = off (per-file override)
    folder_id INTEGER
);
"""


def build_test_schema(conn: sqlite3.Connection) -> None:
    """Execute TEST_SCHEMA on an existing connection.

    Convenience wrapper around conn.executescript(TEST_SCHEMA) that handles
    PRAGMA settings and commit.  Usage::

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        build_test_schema(conn)
        conn.close()
    """
    conn.executescript(TEST_SCHEMA)
