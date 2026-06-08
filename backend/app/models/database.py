"""
SQLite database initialization and schema for RAGAPPv3.

This module provides the database schema and initialization helper for the application.
"""

import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Full, Queue

from app.config import settings

logger = logging.getLogger(__name__)

# Database schema definition
SCHEMA = """
-- Vaults table: stores document collection vaults
CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id INTEGER,
    org_id INTEGER,
    visibility TEXT DEFAULT 'private' CHECK (visibility IN ('private', 'org', 'public')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Files table: stores uploaded file metadata
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash TEXT,
    file_size INTEGER NOT NULL,
    file_type TEXT,
    chunk_count INTEGER DEFAULT 0,
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
    -- Phase-aware progress (status stays in the canonical 4-value enum;
    -- async/queued/parsing/chunking/embedding live in `phase`).
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
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    FOREIGN KEY (vault_id) REFERENCES vaults(id)
);

-- Full-text search index for document list metadata search
CREATE VIRTUAL TABLE IF NOT EXISTS files_search_fts USING fts5(
    file_name,
    file_type,
    status,
    source,
    email_subject,
    email_sender,
    document_date,
    content='files',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS files_search_fts_insert AFTER INSERT ON files BEGIN
    INSERT INTO files_search_fts(rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
    VALUES (new.id, new.file_name, new.file_type, new.status, new.source, new.email_subject, new.email_sender, new.document_date);
END;

CREATE TRIGGER IF NOT EXISTS files_search_fts_delete AFTER DELETE ON files BEGIN
    INSERT INTO files_search_fts(files_search_fts, rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
    VALUES ('delete', old.id, old.file_name, old.file_type, old.status, old.source, old.email_subject, old.email_sender, old.document_date);
END;

CREATE TRIGGER IF NOT EXISTS files_search_fts_update AFTER UPDATE ON files BEGIN
    INSERT INTO files_search_fts(files_search_fts, rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
    VALUES ('delete', old.id, old.file_name, old.file_type, old.status, old.source, old.email_subject, old.email_sender, old.document_date);
    INSERT INTO files_search_fts(rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
    VALUES (new.id, new.file_name, new.file_type, new.status, new.source, new.email_subject, new.email_sender, new.document_date);
END;

-- Memories table: stores processed document chunks with embeddings
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER,
    content TEXT NOT NULL,
    category TEXT,
    tags TEXT,       -- JSON array of tags
    source TEXT,     -- Source reference
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Full-text search virtual table for memories content
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    category,
    content='memories',
    content_rowid='id'
);

-- Trigger to keep FTS index in sync with memories table (insert)
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
END;

-- Trigger to keep FTS index in sync with memories table (delete)
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
END;

-- Trigger to keep FTS index in sync with memories table (update)
CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
    INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
END;

-- Chat sessions table: stores conversation sessions
CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    user_id INTEGER,
    title TEXT,
    forked_from_session_id INTEGER,
    fork_message_index INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vault_id) REFERENCES vaults(id)
);

-- Chat messages table: stores individual messages within sessions
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    sources TEXT,    -- JSON array of source references
    memories TEXT,   -- JSON array of memories used (M# labels) — NULL on legacy rows
    mode TEXT,       -- 'instant' | 'thinking' for assistant rows; NULL on legacy/user rows
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

-- Index for faster lookups
CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_hash_vault_status ON files(file_hash, vault_id, status);

-- Document actions for auditing admin operations
CREATE TABLE IF NOT EXISTS document_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    user_id TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hmac_sha256 TEXT NOT NULL
);

-- Admin feature toggles
CREATE TABLE IF NOT EXISTS admin_toggles (
    feature TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit log for toggle changes
CREATE TABLE IF NOT EXISTS audit_toggle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    user_id TEXT,
    ip TEXT,
    timestamp TEXT NOT NULL,
    key_version TEXT,
    hmac_sha256 TEXT NOT NULL
);

-- Secret key metadata for audit hashing
CREATE TABLE IF NOT EXISTS secret_keys (
    version TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- System flags for maintenance mode and feature toggles
CREATE TABLE IF NOT EXISTS system_flags (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Settings key-value store for persistence across restarts
CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Users table: stores user accounts for authentication
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
    locked_until TIMESTAMP
);

-- Organizations table: stores organization entities
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

-- Organization members table: links users to organizations with roles
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

-- Groups table: stores permission groups within organizations
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
    UNIQUE(org_id, name)
);

-- Group members table: links users to groups
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(group_id, user_id)
);

-- User sessions table: stores refresh tokens for authentication
CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    refresh_token_hash TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    ip_address TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Indexes for auth tables
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_org_members_org_id ON org_members(org_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user_id ON org_members(user_id);
CREATE INDEX IF NOT EXISTS idx_org_members_org_user ON org_members(org_id, user_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_refresh_hash ON user_sessions(refresh_token_hash);

-- Vault members table: direct user access to vaults
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

-- Vault group access table: group-based access to vaults
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

-- Indexes for vault permission tables
CREATE INDEX IF NOT EXISTS idx_vault_members_vault_id ON vault_members(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_members_user_id ON vault_members(user_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_vault_id ON vault_group_access(vault_id);
CREATE INDEX IF NOT EXISTS idx_vault_group_access_group_id ON vault_group_access(group_id);
CREATE INDEX IF NOT EXISTS idx_users_locked_until ON users(locked_until);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON chat_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_vault_id ON memories(vault_id);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);

-- ============================================================
-- Wiki / Knowledge Compiler tables
-- ============================================================

CREATE TABLE IF NOT EXISTS wiki_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    parent_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    page_type TEXT NOT NULL CHECK (page_type IN (
        'entity','procedure','system','acronym','qa',
        'contradiction','open_question','overview','manual')),
    markdown TEXT NOT NULL DEFAULT '',
    summary TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft','verified','stale','needs_review','archived')),
    confidence REAL DEFAULT 0.0,
    version INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_compiled_at TIMESTAMP,
    UNIQUE(vault_id, slug)
);

CREATE TABLE IF NOT EXISTS wiki_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'unknown',
    aliases_json TEXT DEFAULT '[]',
    description TEXT DEFAULT '',
    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vault_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS wiki_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
    claim_text TEXT NOT NULL,
    normalized_text TEXT,
    claim_type TEXT NOT NULL DEFAULT 'fact',
    subject TEXT,
    predicate TEXT,
    object TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'document','memory','chat_synthesis','manual','mixed')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active','contradicted','superseded','unverified','archived','needs_review')),
    confidence REAL DEFAULT 0.0,
    created_by INTEGER,
    -- 'deterministic' | 'llm_curator' | NULL (legacy / unknown).
    -- Distinct from `created_by` (user id) so a curator-authored claim
    -- can still record the operator who promoted/reviewed it.
    created_by_kind TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vault_id, claim_text)
);

CREATE TABLE IF NOT EXISTS wiki_claim_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'document','memory','chat_message','manual','wiki')),
    file_id INTEGER,
    chunk_id TEXT,
    memory_id INTEGER,
    chat_message_id INTEGER,
    source_label TEXT,
    quote TEXT,
    char_start INTEGER,
    char_end INTEGER,
    page_number INTEGER,
    confidence REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wiki_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    subject_entity_id INTEGER REFERENCES wiki_entities(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    object_entity_id INTEGER REFERENCES wiki_entities(id) ON DELETE CASCADE,
    object_text TEXT,
    claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE,
    confidence REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_entity_id, predicate, object_entity_id)
);

CREATE TABLE IF NOT EXISTS wiki_compile_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN (
        'ingest','query','memory','manual','settings_reindex')),
    trigger_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','running','completed','failed','cancelled')),
    error TEXT,
    result_json TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wiki_lint_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    finding_type TEXT NOT NULL CHECK (finding_type IN (
        'contradiction','stale','orphan','missing_page',
        'unsupported_claim','duplicate_entity','weak_provenance')),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
        'low','medium','high','critical')),
    title TEXT NOT NULL,
    details TEXT DEFAULT '',
    related_page_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_type(related_page_ids_json) = 'array'),
    related_claim_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_type(related_claim_ids_json) = 'array'),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
        'open','acknowledged','resolved','dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- FTS virtual tables for wiki search
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts USING fts5(
    title, summary, markdown,
    content='wiki_pages', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_claims_fts USING fts5(
    claim_text, subject, predicate, object,
    content='wiki_claims', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_entities_fts USING fts5(
    canonical_name, aliases_json, description,
    content='wiki_entities', content_rowid='id'
);

-- FTS triggers for wiki_pages
CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_insert AFTER INSERT ON wiki_pages BEGIN
    INSERT INTO wiki_pages_fts(rowid, title, summary, markdown) VALUES (new.id, new.title, new.summary, new.markdown);
END;
CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_delete AFTER DELETE ON wiki_pages BEGIN
    INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary, markdown) VALUES ('delete', old.id, old.title, old.summary, old.markdown);
END;
CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_update AFTER UPDATE ON wiki_pages BEGIN
    INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary, markdown) VALUES ('delete', old.id, old.title, old.summary, old.markdown);
    INSERT INTO wiki_pages_fts(rowid, title, summary, markdown) VALUES (new.id, new.title, new.summary, new.markdown);
END;

-- FTS triggers for wiki_claims
CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_insert AFTER INSERT ON wiki_claims BEGIN
    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object) VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
END;
CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_delete AFTER DELETE ON wiki_claims BEGIN
    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object) VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
END;
CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_update AFTER UPDATE ON wiki_claims BEGIN
    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object) VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object) VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
END;

-- FTS triggers for wiki_entities
CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_insert AFTER INSERT ON wiki_entities BEGIN
    INSERT INTO wiki_entities_fts(rowid, canonical_name, aliases_json, description) VALUES (new.id, new.canonical_name, new.aliases_json, new.description);
END;
CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_delete AFTER DELETE ON wiki_entities BEGIN
    INSERT INTO wiki_entities_fts(wiki_entities_fts, rowid, canonical_name, aliases_json, description) VALUES ('delete', old.id, old.canonical_name, old.aliases_json, old.description);
END;
CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_update AFTER UPDATE ON wiki_entities BEGIN
    INSERT INTO wiki_entities_fts(wiki_entities_fts, rowid, canonical_name, aliases_json, description) VALUES ('delete', old.id, old.canonical_name, old.aliases_json, old.description);
    INSERT INTO wiki_entities_fts(rowid, canonical_name, aliases_json, description) VALUES (new.id, new.canonical_name, new.aliases_json, new.description);
END;

-- Indexes for wiki tables
CREATE INDEX IF NOT EXISTS idx_wiki_pages_vault_type_status ON wiki_pages(vault_id, page_type, status);
CREATE INDEX IF NOT EXISTS idx_wiki_pages_vault_slug ON wiki_pages(vault_id, slug);
CREATE INDEX IF NOT EXISTS idx_wiki_entities_vault_name ON wiki_entities(vault_id, canonical_name);
CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_page_status ON wiki_claims(vault_id, page_id, status);
CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_normalized ON wiki_claims(vault_id, normalized_text);
CREATE INDEX IF NOT EXISTS idx_wiki_claim_sources_claim_id ON wiki_claim_sources(claim_id);
CREATE INDEX IF NOT EXISTS idx_wiki_compile_jobs_vault_status ON wiki_compile_jobs(vault_id, status);
CREATE INDEX IF NOT EXISTS idx_wiki_lint_findings_vault_status_severity ON wiki_lint_findings(vault_id, status, severity);

-- Wiki page version history (DD-C003)
CREATE TABLE IF NOT EXISTS wiki_page_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    vault_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL DEFAULT '',
    summary TEXT DEFAULT '',
    status TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    edited_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wiki_page_versions_page ON wiki_page_versions(page_id);

-- Wiki page file attachments (DD-C019)
CREATE TABLE IF NOT EXISTS wiki_page_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    vault_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(page_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_wiki_page_files_page ON wiki_page_files(page_id);
CREATE INDEX IF NOT EXISTS idx_wiki_page_files_file ON wiki_page_files(file_id);

-- Wiki inter-page links and backlinks (DD-C030)
CREATE TABLE IF NOT EXISTS wiki_page_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    target_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    vault_id INTEGER NOT NULL,
    link_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_page_id, target_page_id)
);
CREATE INDEX IF NOT EXISTS idx_wiki_page_links_source ON wiki_page_links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_wiki_page_links_target ON wiki_page_links(target_page_id);

-- Wiki activity log (DD-C026)
CREATE TABLE IF NOT EXISTS wiki_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'page_created','page_updated','page_deleted',
        'claim_created','claim_updated','claim_deleted',
        'entity_created','entity_updated',
        'job_completed','job_failed',
        'lint_resolved','lint_dismissed')),
    target_type TEXT NOT NULL CHECK (target_type IN (
        'page','claim','entity','job','lint_finding')),
    target_id INTEGER NOT NULL,
    user_id INTEGER,
    details_json TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wiki_activity_log_vault ON wiki_activity_log(vault_id, created_at DESC);

-- ============================================================
-- KMS / Knowledge Management tables (user-curated documentation)
-- ============================================================

CREATE TABLE IF NOT EXISTS kms_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    summary TEXT DEFAULT '',
    tags_json TEXT DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'manual' CHECK (source_type IN (
        'manual','document','import')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft','published','archived')),
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_compiled_at TIMESTAMP,
    UNIQUE(vault_id, slug)
);

CREATE TABLE IF NOT EXISTS kms_compile_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN (
        'ingest','manual','settings_reindex')),
    trigger_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','running','completed','failed','cancelled')),
    error TEXT,
    result_json TEXT DEFAULT '{}',
    input_json TEXT DEFAULT '{}',
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

-- FTS index over KMS entry content (title + summary + body + tags)
CREATE VIRTUAL TABLE IF NOT EXISTS kms_entries_fts USING fts5(
    title, summary, body, tags_json,
    content='kms_entries', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS kms_entries_fts_insert AFTER INSERT ON kms_entries BEGIN
    INSERT INTO kms_entries_fts(rowid, title, summary, body, tags_json) VALUES (new.id, new.title, new.summary, new.body, new.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS kms_entries_fts_delete AFTER DELETE ON kms_entries BEGIN
    INSERT INTO kms_entries_fts(kms_entries_fts, rowid, title, summary, body, tags_json) VALUES ('delete', old.id, old.title, old.summary, old.body, old.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS kms_entries_fts_update AFTER UPDATE ON kms_entries BEGIN
    INSERT INTO kms_entries_fts(kms_entries_fts, rowid, title, summary, body, tags_json) VALUES ('delete', old.id, old.title, old.summary, old.body, old.tags_json);
    INSERT INTO kms_entries_fts(rowid, title, summary, body, tags_json) VALUES (new.id, new.title, new.summary, new.body, new.tags_json);
END;

CREATE INDEX IF NOT EXISTS idx_kms_entries_vault_status ON kms_entries(vault_id, status);
CREATE INDEX IF NOT EXISTS idx_kms_entries_vault_slug ON kms_entries(vault_id, slug);
CREATE INDEX IF NOT EXISTS idx_kms_entries_file_id ON kms_entries(file_id);
CREATE INDEX IF NOT EXISTS idx_kms_compile_jobs_vault_status ON kms_compile_jobs(vault_id, status);

-- ============================================================
-- Document organization tags (user-curated, vault-scoped)
-- ============================================================

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vault_id, name),
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS document_tags (
    file_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (file_id, tag_id),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tags_vault ON tags(vault_id);
CREATE INDEX IF NOT EXISTS idx_document_tags_tag_id ON document_tags(tag_id);

-- ============================================================
-- Document folders (user-curated hierarchy, vault-scoped)
-- ============================================================

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    parent_folder_id INTEGER,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_folder_id) REFERENCES folders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_folders_vault ON folders(vault_id);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);
-- NOTE: the idx_files_folder_id index is created by migrate_add_folders(), not
-- here. The files table's executescript path also runs against pre-existing
-- (legacy) databases where files.folder_id may not exist yet; indexing it here
-- would raise "no such column: folder_id" before the migration adds the column.
"""


def init_db(sqlite_path: str) -> None:
    """
    Initialize the SQLite database with the schema.

    Args:
        sqlite_path: Path to the SQLite database file.

    Raises:
        sqlite3.Error: If database initialization fails.
    """
    # Ensure parent directory exists
    db_path = Path(sqlite_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Connect and execute schema
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        migrate_add_user_id_to_chat_sessions(sqlite_path, conn=conn)
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "files" in existing_tables:
            existing_file_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
            }
            for name, ddl in (
                ("vault_id", "INTEGER NOT NULL DEFAULT 1"),
                ("file_size", "INTEGER DEFAULT 0"),
                ("file_type", "TEXT"),
                ("source", "TEXT NOT NULL DEFAULT 'upload'"),
                ("email_subject", "TEXT"),
                ("email_sender", "TEXT"),
                ("modified_at", "TIMESTAMP DEFAULT NULL"),
                ("document_date", "TEXT"),
                ("supersedes_file_id", "INTEGER"),
                ("ingestion_version", "INTEGER DEFAULT 1"),
                ("wiki_pending", "INTEGER NOT NULL DEFAULT 0"),
                ("enrichment_status", "TEXT"),
                ("enrichment_error", "TEXT"),
                ("enrichment_updated_at", "TIMESTAMP"),
            ):
                if name not in existing_file_cols:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def run_migrations(sqlite_path: str) -> None:
    """
    Run database migrations to initialize the schema.

    This function calls init_db to create the database and apply the schema.
    It is intended to be called during application startup to ensure the
    database is properly initialized.

    Args:
        sqlite_path: Path to the SQLite database file.

    Returns:
        None
    """
    init_db(sqlite_path)

    # Migrate refresh token index from non-unique to unique
    conn = sqlite3.connect(sqlite_path)
    try:
        # Check if a unique index already exists (from prior migration run)
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_user_sessions_refresh_hash'"
        )
        row = cursor.fetchone()
        needs_migration = False
        if not row:
            # No index at all — check for duplicate hashes before creating unique index
            dup_cursor = conn.execute(
                "SELECT refresh_token_hash, COUNT(*) as cnt FROM user_sessions GROUP BY refresh_token_hash HAVING cnt > 1"
            )
            if dup_cursor.fetchone():
                needs_migration = True
            # Create unique index directly (with or without dedup)
            conn.execute("DROP INDEX IF EXISTS idx_user_sessions_refresh_hash")
            if needs_migration:
                conn.execute(
                    "DELETE FROM user_sessions WHERE id NOT IN (SELECT MAX(id) FROM user_sessions GROUP BY refresh_token_hash)"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_refresh_hash ON user_sessions(refresh_token_hash)"
            )
        elif row[0] and "UNIQUE" not in row[0].upper():
            # Non-unique index exists — migrate to unique
            conn.execute("DROP INDEX IF EXISTS idx_user_sessions_refresh_hash")
            conn.execute(
                "DELETE FROM user_sessions WHERE id NOT IN (SELECT MAX(id) FROM user_sessions GROUP BY refresh_token_hash)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_refresh_hash ON user_sessions(refresh_token_hash)"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    migrate_add_vaults(sqlite_path)
    migrate_add_email_columns(sqlite_path)
    migrate_add_file_metadata_columns(sqlite_path)
    migrate_add_user_org_tables(sqlite_path)
    migrate_add_vault_permission_columns(sqlite_path)
    migrate_vault_paths(sqlite_path)
    migrate_add_org_slug_column(sqlite_path)
    migrate_add_user_id_to_chat_sessions(sqlite_path)
    migrate_add_fork_columns(sqlite_path)
    migrate_add_feedback_column(sqlite_path)
    migrate_add_chat_memories_column(sqlite_path)
    migrate_add_chat_mode_column(sqlite_path)
    migrate_add_memory_embedding_column(sqlite_path)
    migrate_sanitize_existing_chat_messages(sqlite_path)
    migrate_add_wiki_tables(sqlite_path)
    migrate_add_wiki_refs_and_job_input(sqlite_path)
    migrate_add_wiki_jobs_retry_count(sqlite_path)
    migrate_add_kms_tables(sqlite_path)
    migrate_add_kms_refs(sqlite_path)
    migrate_add_tags_tables(sqlite_path)
    migrate_add_folders(sqlite_path)
    migrate_add_files_parsed_text(sqlite_path)
    migrate_add_files_processing_progress(sqlite_path)
    migrate_add_files_enrichment_status(sqlite_path)
    migrate_add_files_search_fts(sqlite_path)
    migrate_add_files_content_fts(sqlite_path)
    migrate_add_curator_claim_support(sqlite_path)
    migrate_widen_wiki_claim_sources_source_kind(sqlite_path)
    migrate_add_wiki_relations_unique(sqlite_path)
    migrate_add_wiki_page_hierarchy_and_versioning(sqlite_path)
    migrate_add_wiki_supporting_tables(sqlite_path)
    migrate_add_wiki_claims_normalized_text(sqlite_path)
    migrate_add_wiki_claims_unique_claim_text(sqlite_path)
    migrate_assign_orphan_users_to_default_vault(sqlite_path)
    migrate_add_wiki_lint_findings_json_check(sqlite_path)

    # Add partial unique index for duplicate hash detection (HIGH-10)
    # Wrapped in IntegrityError handler: existing databases may have duplicate
    # (file_hash, vault_id) pairs that prevent index creation.
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_files_hash_vault_indexed
            ON files(file_hash, vault_id)
            WHERE file_hash IS NOT NULL AND status = 'indexed'
        """)
        conn.commit()
    except sqlite3.IntegrityError as e:
        logger.warning(
            "Could not create unique file-hash index (duplicate indexed records exist): %s. "
            "The index will not be created until duplicates are removed.",
            e,
        )
        conn.rollback()
    finally:
        conn.close()


def migrate_add_vaults(sqlite_path: str) -> None:
    """
    Migration: Add vaults table and vault_id columns to existing databases.

    This migration is idempotent — safe to run multiple times.
    It creates the vaults table, adds vault_id columns to files/memories/
    chat_sessions if missing.  The ALTER TABLE ADD COLUMN statements use
    DEFAULT 1 because SQLite requires a DEFAULT when adding a NOT NULL
    column to a table that may already contain rows.  This is the ONLY
    code path that references vault_id=1 for legacy table upgrades — it
    does NOT create a Default vault row or INSERT any data.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        # 1. Create vaults table if not exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Add vault_id columns if missing (SQLite doesn't support IF NOT EXISTS for columns)
        def _column_exists(table: str, column: str) -> bool:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cursor.fetchall())

        if not _column_exists("files", "vault_id"):
            conn.execute(
                "ALTER TABLE files ADD COLUMN vault_id INTEGER NOT NULL DEFAULT 1"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_vault_id ON files(vault_id)"
            )

        if not _column_exists("memories", "vault_id"):
            conn.execute("ALTER TABLE memories ADD COLUMN vault_id INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_vault_id ON memories(vault_id)"
            )

        if not _column_exists("chat_sessions", "vault_id"):
            conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN vault_id INTEGER NOT NULL DEFAULT 1"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_sessions_vault_id ON chat_sessions(vault_id)"
            )

        conn.commit()
    finally:
        conn.close()


def migrate_add_email_columns(sqlite_path: str) -> None:
    """
    Migration: Add email tracking columns to files table.

    Adds source, email_subject, and email_sender columns to track
    documents ingested via email.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        def _column_exists(table: str, column: str) -> bool:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cursor.fetchall())

        # Add source column (track upload, scan, email)
        if not _column_exists("files", "source"):
            conn.execute(
                "ALTER TABLE files ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_source ON files(source)")

        # Add email_subject column (nullable)
        if not _column_exists("files", "email_subject"):
            conn.execute("ALTER TABLE files ADD COLUMN email_subject TEXT")

        # Add email_sender column (nullable)
        if not _column_exists("files", "email_sender"):
            conn.execute("ALTER TABLE files ADD COLUMN email_sender TEXT")

        conn.commit()
    finally:
        conn.close()


def migrate_add_file_metadata_columns(sqlite_path: str) -> None:
    """
    Migration: Add file metadata columns to files table.

    Adds file_size, file_type, and modified_at columns that were added to the
    schema but never had ALTER TABLE migrations. Existing rows get safe defaults.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        def _column_exists(table: str, column: str) -> bool:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cursor.fetchall())

        if not _column_exists("files", "file_size"):
            conn.execute("ALTER TABLE files ADD COLUMN file_size INTEGER DEFAULT 0")

        if not _column_exists("files", "file_type"):
            conn.execute("ALTER TABLE files ADD COLUMN file_type TEXT")

        if not _column_exists("files", "modified_at"):
            conn.execute(
                "ALTER TABLE files ADD COLUMN modified_at TIMESTAMP DEFAULT NULL"
            )
            # Backfill existing rows: use created_at as a reasonable modified_at proxy
            conn.execute(
                "UPDATE files SET modified_at = created_at WHERE modified_at IS NULL"
            )

        conn.commit()
    finally:
        conn.close()


def migrate_add_user_org_tables(sqlite_path: str) -> None:
    """
    Migration: Ensure user and organization tables exist.

    Runs the full schema which includes users, organizations,
    groups, and permission tables. Idempotent via CREATE IF NOT EXISTS.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def migrate_add_vault_permission_columns(sqlite_path: str) -> None:
    """
    Migration: Add permission columns to vaults table.

    Adds owner_id, org_id, and visibility columns to support
    the new RBAC permission system.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        cursor = conn.execute("PRAGMA table_info(vaults)")
        columns = [row[1] for row in cursor.fetchall()]

        # Add owner_id column
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE vaults ADD COLUMN owner_id INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vaults_owner_id ON vaults(owner_id)"
            )

        # Add org_id column
        if "org_id" not in columns:
            conn.execute("ALTER TABLE vaults ADD COLUMN org_id INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vaults_org_id ON vaults(org_id)"
            )

        # Add visibility column
        if "visibility" not in columns:
            conn.execute(
                "ALTER TABLE vaults ADD COLUMN visibility TEXT DEFAULT 'private' "
                "CHECK (visibility IN ('private', 'org', 'public'))"
            )
            # Set default visibility for existing vaults
            conn.execute(
                "UPDATE vaults SET visibility = 'private' WHERE visibility IS NULL"
            )

        conn.commit()
    finally:
        conn.close()


def migrate_add_org_slug_column(sqlite_path: str) -> None:
    """Migration: Add slug column to organizations table and add 'owner' to org_members role CHECK."""
    import re

    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        # Add slug column to organizations if missing
        cursor = conn.execute("PRAGMA table_info(organizations)")
        columns = [row[1] for row in cursor.fetchall()]

        if "slug" not in columns:
            conn.execute("ALTER TABLE organizations ADD COLUMN slug TEXT")
            # Generate slugs for existing orgs
            cursor = conn.execute("SELECT id, name FROM organizations")
            for row in cursor.fetchall():
                slug = row[1].lower().strip()
                slug = re.sub(r"[^a-z0-9]+", "-", slug)
                slug = slug.strip("-")
                slug = re.sub(r"-+", "-", slug)[:50]
                conn.execute(
                    "UPDATE organizations SET slug = ? WHERE id = ?", (slug, row[0])
                )

        conn.commit()
    finally:
        conn.close()


def migrate_vault_paths(sqlite_path: str) -> None:
    """
    Migration: Rename vault directories from sanitized_name to numeric ID.

    Reads all vaults from the database, and for each vault, checks if:
    - vaults/{sanitized_name}/ exists but vaults/{id}/ does NOT exist → rename
    - Both exist → merge contents (copy from name-dir to id-dir, then remove name-dir)
    - Only vaults/{id}/ exists → skip (already migrated)

    Uses pathlib.Path for cross-platform compatibility. Idempotent.
    """
    vaults_dir = settings.vaults_dir
    if not vaults_dir.exists():
        return

    conn = sqlite3.connect(sqlite_path)
    try:
        cursor = conn.execute("SELECT id, name FROM vaults")
        vaults = cursor.fetchall()

        for vault_id, name in vaults:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            old_path = vaults_dir / safe_name
            new_path = vaults_dir / str(vault_id)

            try:
                if not old_path.exists() and not new_path.exists():
                    # Neither exists - nothing to do for this vault
                    continue
                elif not old_path.exists() and new_path.exists():
                    # Already migrated - skip
                    continue
                elif old_path.exists() and not new_path.exists():
                    # Rename old to new
                    old_path.rename(new_path)
                    logger.info(f"Migrated vault directory: {safe_name} → {vault_id}")
                elif old_path.exists() and new_path.exists():
                    # Both exist - merge contents
                    shutil.copytree(old_path, new_path, dirs_exist_ok=True)
                    shutil.rmtree(old_path)
                    logger.info(
                        f"Merged vault directory contents: {safe_name} → {vault_id}"
                    )
            except (OSError, shutil.Error) as e:
                logger.warning(f"Failed to migrate vault '{name}' (ID {vault_id}): {e}")
                # Continue with other vaults, don't raise
    finally:
        conn.close()


def migrate_add_user_id_to_chat_sessions(
    sqlite_path: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Migration: Add owner user_id to chat_sessions for per-user policy checks."""
    owns_connection = conn is None
    conn = conn or sqlite3.connect(sqlite_path)
    try:
        table_cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'"
        )
        if table_cursor.fetchone() is None:
            return

        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()
        }
        if "user_id" not in columns:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN user_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id "
            "ON chat_sessions(user_id)"
        )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def migrate_add_fork_columns(sqlite_path: str) -> None:
    """Migration: Add forked_from_session_id and fork_message_index to chat_sessions."""
    conn = sqlite3.connect(sqlite_path)
    try:
        cursor = conn.execute("PRAGMA table_info(chat_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "forked_from_session_id" not in columns:
            conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN forked_from_session_id INTEGER"
            )
        if "fork_message_index" not in columns:
            conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN fork_message_index INTEGER"
            )
        conn.commit()
    finally:
        conn.close()


def migrate_add_feedback_column(sqlite_path: str) -> None:
    """Migration: Add feedback column to chat_messages table."""
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()]
        if "feedback" not in existing_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN feedback TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_add_chat_memories_column(sqlite_path: str) -> None:
    """Migration: add ``memories`` JSON column to chat_messages table.

    Stores the list of memories used by the assistant when generating each
    message. Persisted as a JSON string for symmetry with ``sources``. Legacy
    rows are left with NULL — the chat route handles both shapes.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
        ]
        if "memories" not in existing_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN memories TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_add_chat_mode_column(sqlite_path: str) -> None:
    """Migration: add ``mode`` column to chat_messages table.

    Records which chat mode (``instant`` or ``thinking``) generated each
    assistant row so the UI can show a per-message model badge that survives
    reloads. Legacy rows remain NULL.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
        ]
        if "mode" not in existing_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN mode TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_sanitize_existing_chat_messages(sqlite_path: str) -> None:
    """Migration: scrub model thinking/reasoning blocks from previously
    persisted assistant chat_messages rows.

    Idempotent: rows whose content is unchanged after sanitization are
    skipped, so re-running the migration is a no-op once clean. Uses the
    same canonical sanitizer as the runtime persistence path so the
    cleanup produces exactly the same content the live ingest would
    produce today.
    """
    # Import inside the function to avoid cycles during initial schema setup.
    from app.utils.assistant_sanitizer import (
        cleanup_existing_chat_messages_rows,
    )

    conn = sqlite3.connect(sqlite_path)
    try:
        # Check the table exists and has the expected columns first; some
        # very old test fixtures call run_migrations on a partially-bootstrapped
        # schema and we should not crash there.
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'"
        )
        if cursor.fetchone() is None:
            return
        cursor = conn.execute(
            "SELECT id, content FROM chat_messages WHERE role = 'assistant'"
        )
        rows = cursor.fetchall()
        cleaned = cleanup_existing_chat_messages_rows(rows)
        if not cleaned:
            return
        for row_id, new_content in cleaned:
            conn.execute(
                "UPDATE chat_messages SET content = ? WHERE id = ?",
                (new_content, row_id),
            )
        conn.commit()
        logger.info(
            "Sanitized %d existing assistant chat_messages rows during migration",
            len(cleaned),
        )
    finally:
        conn.close()


def migrate_add_memory_embedding_column(sqlite_path: str) -> None:
    """Migration: add ``embedding`` and ``embedding_model`` columns to memories.

    Memory embeddings power semantic/hybrid memory retrieval. The embedding
    is stored as a JSON-encoded float list keyed by the model that produced
    it so we can detect stale embeddings if the embedding model changes.
    Both columns are nullable so existing memories still work via FTS5
    fallback when no embedding has been computed yet.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        ]
        if "embedding" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN embedding TEXT")
        if "embedding_model" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN embedding_model TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_tables(sqlite_path: str) -> None:
    """
    Migration: Add wiki / Knowledge Compiler tables for existing databases.

    Idempotent — safe to run multiple times. All tables, FTS virtual tables,
    triggers, and indexes use IF NOT EXISTS guards.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                page_type TEXT NOT NULL CHECK (page_type IN (
                    'entity','procedure','system','acronym','qa',
                    'contradiction','open_question','overview','manual')),
                markdown TEXT NOT NULL DEFAULT '',
                summary TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                    'draft','verified','stale','needs_review','archived')),
                confidence REAL DEFAULT 0.0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_compiled_at TIMESTAMP,
                UNIQUE(vault_id, slug)
            );

            CREATE TABLE IF NOT EXISTS wiki_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                canonical_name TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'unknown',
                aliases_json TEXT DEFAULT '[]',
                description TEXT DEFAULT '',
                page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vault_id, canonical_name)
            );

            CREATE TABLE IF NOT EXISTS wiki_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                claim_text TEXT NOT NULL,
                claim_type TEXT NOT NULL DEFAULT 'fact',
                subject TEXT,
                predicate TEXT,
                object TEXT,
                source_type TEXT NOT NULL CHECK (source_type IN (
                    'document','memory','chat_synthesis','manual','mixed')),
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                    'active','contradicted','superseded','unverified','archived','needs_review')),
                confidence REAL DEFAULT 0.0,
                created_by INTEGER,
                created_by_kind TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vault_id, claim_text)
            );

            CREATE TABLE IF NOT EXISTS wiki_claim_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
                source_kind TEXT NOT NULL CHECK (source_kind IN (
                    'document','memory','chat_message','manual')),
                file_id INTEGER,
                chunk_id TEXT,
                memory_id INTEGER,
                chat_message_id INTEGER,
                source_label TEXT,
                quote TEXT,
                char_start INTEGER,
                char_end INTEGER,
                page_number INTEGER,
                confidence REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wiki_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                subject_entity_id INTEGER REFERENCES wiki_entities(id) ON DELETE CASCADE,
                predicate TEXT NOT NULL,
                object_entity_id INTEGER REFERENCES wiki_entities(id) ON DELETE CASCADE,
                object_text TEXT,
                claim_id INTEGER REFERENCES wiki_claims(id) ON DELETE CASCADE,
                confidence REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wiki_compile_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                trigger_type TEXT NOT NULL CHECK (trigger_type IN (
                    'ingest','query','memory','manual','settings_reindex')),
                trigger_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','running','completed','failed','cancelled')),
                error TEXT,
                result_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wiki_lint_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                finding_type TEXT NOT NULL CHECK (finding_type IN (
                    'contradiction','stale','orphan','missing_page',
                    'unsupported_claim','duplicate_entity','weak_provenance')),
                severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
                    'low','medium','high','critical')),
                title TEXT NOT NULL,
                details TEXT DEFAULT '',
                related_page_ids_json TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_type(related_page_ids_json) = 'array'),
                related_claim_ids_json TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_type(related_claim_ids_json) = 'array'),
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                    'open','acknowledged','resolved','dismissed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts USING fts5(
                title, summary, markdown,
                content='wiki_pages', content_rowid='id'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_claims_fts USING fts5(
                claim_text, subject, predicate, object,
                content='wiki_claims', content_rowid='id'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_entities_fts USING fts5(
                canonical_name, aliases_json, description,
                content='wiki_entities', content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_insert AFTER INSERT ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(rowid, title, summary, markdown) VALUES (new.id, new.title, new.summary, new.markdown);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_delete AFTER DELETE ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary, markdown) VALUES ('delete', old.id, old.title, old.summary, old.markdown);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_pages_fts_update AFTER UPDATE ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary, markdown) VALUES ('delete', old.id, old.title, old.summary, old.markdown);
                INSERT INTO wiki_pages_fts(rowid, title, summary, markdown) VALUES (new.id, new.title, new.summary, new.markdown);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_insert AFTER INSERT ON wiki_claims BEGIN
                INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object) VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_delete AFTER DELETE ON wiki_claims BEGIN
                INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object) VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_update AFTER UPDATE ON wiki_claims BEGIN
                INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object) VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
                INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object) VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
            END;

            CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_insert AFTER INSERT ON wiki_entities BEGIN
                INSERT INTO wiki_entities_fts(rowid, canonical_name, aliases_json, description) VALUES (new.id, new.canonical_name, new.aliases_json, new.description);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_delete AFTER DELETE ON wiki_entities BEGIN
                INSERT INTO wiki_entities_fts(wiki_entities_fts, rowid, canonical_name, aliases_json, description) VALUES ('delete', old.id, old.canonical_name, old.aliases_json, old.description);
            END;
            CREATE TRIGGER IF NOT EXISTS wiki_entities_fts_update AFTER UPDATE ON wiki_entities BEGIN
                INSERT INTO wiki_entities_fts(wiki_entities_fts, rowid, canonical_name, aliases_json, description) VALUES ('delete', old.id, old.canonical_name, old.aliases_json, old.description);
                INSERT INTO wiki_entities_fts(rowid, canonical_name, aliases_json, description) VALUES (new.id, new.canonical_name, new.aliases_json, new.description);
            END;

            CREATE INDEX IF NOT EXISTS idx_wiki_pages_vault_type_status ON wiki_pages(vault_id, page_type, status);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_vault_slug ON wiki_pages(vault_id, slug);
            CREATE INDEX IF NOT EXISTS idx_wiki_entities_vault_name ON wiki_entities(vault_id, canonical_name);
            CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_page_status ON wiki_claims(vault_id, page_id, status);
            CREATE INDEX IF NOT EXISTS idx_wiki_claim_sources_claim_id ON wiki_claim_sources(claim_id);
            CREATE INDEX IF NOT EXISTS idx_wiki_compile_jobs_vault_status ON wiki_compile_jobs(vault_id, status);
            CREATE INDEX IF NOT EXISTS idx_wiki_lint_findings_vault_status_severity ON wiki_lint_findings(vault_id, status, severity);
        """)
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_refs_and_job_input(sqlite_path: str) -> None:
    """Migration: add wiki_refs to chat_messages and input_json to wiki_compile_jobs.

    Idempotent — safe to run multiple times.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_msg_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
        ]
        if "wiki_refs" not in existing_msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN wiki_refs TEXT")

        existing_job_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()
        ]
        if "input_json" not in existing_job_cols:
            conn.execute(
                "ALTER TABLE wiki_compile_jobs ADD COLUMN input_json TEXT DEFAULT '{}'"
            )
        conn.commit()
    finally:
        conn.close()


def migrate_add_kms_refs(sqlite_path: str) -> None:
    """Migration: add ``kms_refs`` to chat_messages for persisted [K#] citation
    cards (the KMS counterpart to ``wiki_refs``). Idempotent.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_msg_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()
        ]
        if "kms_refs" not in existing_msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN kms_refs TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_jobs_retry_count(sqlite_path: str) -> None:
    """Migration: add retry_count to wiki_compile_jobs. Idempotent."""
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()
        ]
        if "retry_count" not in existing_cols:
            conn.execute(
                "ALTER TABLE wiki_compile_jobs ADD COLUMN retry_count INTEGER DEFAULT 0"
            )
        conn.commit()
    finally:
        conn.close()


def migrate_add_kms_tables(sqlite_path: str) -> None:
    """
    Migration: Add KMS / Knowledge Management tables for existing databases.

    Idempotent — safe to run multiple times. All tables, FTS virtual tables,
    triggers, and indexes use IF NOT EXISTS guards.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kms_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                summary TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                source_type TEXT NOT NULL DEFAULT 'manual' CHECK (source_type IN (
                    'manual','document','import')),
                status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                    'draft','published','archived')),
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_compiled_at TIMESTAMP,
                UNIQUE(vault_id, slug)
            );

            CREATE TABLE IF NOT EXISTS kms_compile_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                trigger_type TEXT NOT NULL CHECK (trigger_type IN (
                    'ingest','manual','settings_reindex')),
                trigger_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','running','completed','failed','cancelled')),
                error TEXT,
                result_json TEXT DEFAULT '{}',
                input_json TEXT DEFAULT '{}',
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS kms_entries_fts USING fts5(
                title, summary, body, tags_json,
                content='kms_entries', content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS kms_entries_fts_insert AFTER INSERT ON kms_entries BEGIN
                INSERT INTO kms_entries_fts(rowid, title, summary, body, tags_json) VALUES (new.id, new.title, new.summary, new.body, new.tags_json);
            END;
            CREATE TRIGGER IF NOT EXISTS kms_entries_fts_delete AFTER DELETE ON kms_entries BEGIN
                INSERT INTO kms_entries_fts(kms_entries_fts, rowid, title, summary, body, tags_json) VALUES ('delete', old.id, old.title, old.summary, old.body, old.tags_json);
            END;
            CREATE TRIGGER IF NOT EXISTS kms_entries_fts_update AFTER UPDATE ON kms_entries BEGIN
                INSERT INTO kms_entries_fts(kms_entries_fts, rowid, title, summary, body, tags_json) VALUES ('delete', old.id, old.title, old.summary, old.body, old.tags_json);
                INSERT INTO kms_entries_fts(rowid, title, summary, body, tags_json) VALUES (new.id, new.title, new.summary, new.body, new.tags_json);
            END;

            CREATE INDEX IF NOT EXISTS idx_kms_entries_vault_status ON kms_entries(vault_id, status);
            CREATE INDEX IF NOT EXISTS idx_kms_entries_vault_slug ON kms_entries(vault_id, slug);
            CREATE INDEX IF NOT EXISTS idx_kms_entries_file_id ON kms_entries(file_id);
            CREATE INDEX IF NOT EXISTS idx_kms_compile_jobs_vault_status ON kms_compile_jobs(vault_id, status);
        """)
        conn.commit()
    finally:
        conn.close()


def migrate_add_tags_tables(sqlite_path: str) -> None:
    """
    Migration: Add document organization tags tables for existing databases.

    Idempotent — safe to run multiple times. Tables and indexes use IF NOT
    EXISTS guards. document_tags rows are removed automatically via ON DELETE
    CASCADE when a file or tag is deleted (foreign keys are enabled on every
    pooled connection).

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vault_id, name),
                FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS document_tags (
                file_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (file_id, tag_id),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tags_vault ON tags(vault_id);
            CREATE INDEX IF NOT EXISTS idx_document_tags_tag_id ON document_tags(tag_id);
        """)
        conn.commit()
    finally:
        conn.close()


def migrate_add_folders(sqlite_path: str) -> None:
    """
    Migration: Add document folder hierarchy for existing databases.

    Idempotent — safe to run multiple times. The folders table and indexes use
    IF NOT EXISTS guards; files.folder_id is added only when absent. Deleting a
    folder unfiles its documents (files.folder_id ON DELETE SET NULL) and
    cascade-deletes its subfolders (parent_folder_id ON DELETE CASCADE);
    deleting a vault cascade-deletes all of its folders.

    Args:
        sqlite_path: Path to the SQLite database file.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                parent_folder_id INTEGER,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vault_id) REFERENCES vaults(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_folder_id) REFERENCES folders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_folders_vault ON folders(vault_id);
            CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);
        """)
        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        ]
        if "folder_id" not in existing_cols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN folder_id INTEGER "
                "REFERENCES folders(id) ON DELETE SET NULL"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_folder_id ON files(folder_id)"
        )
        conn.commit()
    finally:
        conn.close()


def migrate_add_files_parsed_text(sqlite_path: str) -> None:
    """Migration: add parsed_text to files table for wiki recompile. Idempotent."""
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        ]
        if "parsed_text" not in existing_cols:
            conn.execute("ALTER TABLE files ADD COLUMN parsed_text TEXT")
        conn.commit()
    finally:
        conn.close()


def migrate_add_curator_claim_support(sqlite_path: str) -> None:
    """Migration (PR C): widen wiki_claims.status to include 'needs_review'
    and add a TEXT ``created_by_kind`` column that records whether a claim
    came from deterministic extraction or the optional LLM curator.

    SQLite cannot ALTER a CHECK constraint, so we follow the rename-
    recreate-copy pattern. Three subtleties matter:

    1. CRITICAL — SQLite >= 3.26 with the default
       ``legacy_alter_table=OFF`` rewrites the textual FK references in
       *child* tables to follow ``ALTER TABLE … RENAME TO``. After we
       drop ``wiki_claims_old``, those FKs would dangle silently:
       ``wiki_claim_sources.claim_id`` and ``wiki_relations.claim_id``
       would still point at ``wiki_claims_old`` and CASCADE would never
       fire. We set ``legacy_alter_table=ON`` for the duration of the
       swap so child FK references stay textually pointing at
       ``wiki_claims``. Verified post-swap with
       ``PRAGMA foreign_key_check`` and ``foreign_key_list``.

    2. FTS triggers on wiki_claims are dropped before the rename and
       recreated after the swap; the ``wiki_claims_fts`` virtual table
       is repopulated via the standard ``('rebuild')`` command so search
       keeps working.

    3. Recovery — if a previous run died after the rename but before the
       drop, ``wiki_claims_old`` will still exist on disk. The function
       detects that at the top and restores by renaming back, then
       re-runs cleanly. Without this an interrupted migration would
       leave the DB unable to ever finish.

    Idempotent — safe to run multiple times.
    """
    conn = sqlite3.connect(sqlite_path)
    # Force autocommit so PRAGMA statements take effect outside of an
    # implicit transaction. This matches the behaviour we verified in
    # the SQLite reproduction case at PR-C reviewer time.
    conn.isolation_level = None
    try:
        # Recovery: if a previous run crashed mid-migration, the old
        # table may still be present alongside (or instead of) the new
        # one. Restore the canonical name before doing anything else.
        old_present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_claims_old'"
        ).fetchone()
        new_present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_claims'"
        ).fetchone()
        if old_present and not new_present:
            # Crashed after rename but before CREATE TABLE wiki_claims.
            # Restore the original name and let the migration run again.
            logger.warning(
                "migrate_add_curator_claim_support: detected "
                "wiki_claims_old without wiki_claims; restoring."
            )
            conn.execute("PRAGMA legacy_alter_table = ON")
            conn.execute("ALTER TABLE wiki_claims_old RENAME TO wiki_claims")
            conn.execute("PRAGMA legacy_alter_table = OFF")
            new_present = True

        # Skip if wiki_claims doesn't exist yet (fresh install path will
        # create it via migrate_add_wiki_tables, which already includes
        # the new schema after this function lands).
        if not new_present:
            return

        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()
        }
        if "created_by_kind" in existing_cols:
            # Migration already applied; nothing to do — but if a stray
            # wiki_claims_old somehow lingers (shouldn't, given the
            # recovery branch above), drop it now.
            if old_present:
                conn.execute("DROP TABLE IF EXISTS wiki_claims_old")
            return

        # Third recovery branch (per PR-C critic Fix #1): both tables
        # exist and wiki_claims is the OLD shape (no created_by_kind).
        # This means a previous run completed the rename, started the
        # new CREATE TABLE, but somehow ended up with both tables.
        # The safe move is to drop the stale wiki_claims_old before the
        # main path runs — otherwise the next ALTER ... RENAME TO
        # wiki_claims_old below will fail with "table already exists".
        if old_present:
            logger.warning(
                "migrate_add_curator_claim_support: detected stale "
                "wiki_claims_old alongside pre-PR-C wiki_claims; "
                "dropping the stale table before re-running migration."
            )
            conn.execute("DROP TABLE IF EXISTS wiki_claims_old")

        # Snapshot row count for parity check.
        before_count = conn.execute("SELECT COUNT(*) FROM wiki_claims").fetchone()[0]

        # Detach FK validation for the duration of the swap and force
        # legacy ALTER behaviour so child-table FK references stay
        # textually pointing at the canonical table name.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            # Drop FTS triggers — they reference wiki_claims by name.
            for trig in (
                "wiki_claims_fts_insert",
                "wiki_claims_fts_delete",
                "wiki_claims_fts_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trig}")

            # Move the old table out of the way.
            conn.execute("ALTER TABLE wiki_claims RENAME TO wiki_claims_old")

            # Create the new table with widened status CHECK + new column.
            conn.execute(
                """
                CREATE TABLE wiki_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vault_id INTEGER NOT NULL,
                    page_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL,
                    claim_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL DEFAULT 'fact',
                    subject TEXT,
                    predicate TEXT,
                    object TEXT,
                    source_type TEXT NOT NULL CHECK (source_type IN (
                        'document','memory','chat_synthesis','manual','mixed')),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','contradicted','superseded','unverified','archived','needs_review')),
                    confidence REAL DEFAULT 0.0,
                    created_by INTEGER,
                    created_by_kind TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Copy every column. created_by_kind defaults to NULL on
            # backfill — interpreted by readers as "deterministic /
            # unknown" (the curator path explicitly sets 'llm_curator').
            conn.execute(
                """
                INSERT INTO wiki_claims (
                    id, vault_id, page_id, claim_text, claim_type,
                    subject, predicate, object, source_type, status,
                    confidence, created_by, created_at, updated_at
                )
                SELECT id, vault_id, page_id, claim_text, claim_type,
                       subject, predicate, object, source_type, status,
                       confidence, created_by, created_at, updated_at
                FROM wiki_claims_old
                """
            )

            # Verify row count parity before dropping the old table.
            after_count = conn.execute("SELECT COUNT(*) FROM wiki_claims").fetchone()[0]
            if after_count != before_count:
                # Bail out without dropping the old table so an operator
                # can recover.
                raise RuntimeError(
                    f"migrate_add_curator_claim_support: row-count parity "
                    f"failed ({before_count} -> {after_count}). "
                    f"wiki_claims_old has been preserved."
                )

            conn.execute("DROP TABLE wiki_claims_old")

            # Recreate the FTS triggers exactly as in the original schema.
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_insert
                AFTER INSERT ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object)
                    VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
                END;
                CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_delete
                AFTER DELETE ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object)
                    VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
                END;
                CREATE TRIGGER IF NOT EXISTS wiki_claims_fts_update
                AFTER UPDATE ON wiki_claims BEGIN
                    INSERT INTO wiki_claims_fts(wiki_claims_fts, rowid, claim_text, subject, predicate, object)
                    VALUES ('delete', old.id, old.claim_text, old.subject, old.predicate, old.object);
                    INSERT INTO wiki_claims_fts(rowid, claim_text, subject, predicate, object)
                    VALUES (new.id, new.claim_text, new.subject, new.predicate, new.object);
                END;
                """
            )

            # Repopulate the FTS virtual table from the new wiki_claims.
            conn.execute(
                "INSERT INTO wiki_claims_fts(wiki_claims_fts) VALUES('rebuild')"
            )

            # Re-create vault/page/status index (was on the original
            # table; index is dropped by the rename pattern in some
            # SQLite versions).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_page_status "
                "ON wiki_claims(vault_id, page_id, status)"
            )
        finally:
            # Restore both PRAGMAs unconditionally; new connections
            # otherwise inherit the legacy_alter_table=ON behaviour
            # which is undesirable for normal traffic.
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")

        # Validate FK integrity post-swap. Any violation here means we
        # produced a broken DB; fail loudly so an operator notices.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"migrate_add_curator_claim_support: foreign_key_check "
                f"reported {len(violations)} violation(s) post-swap: "
                f"{violations[:5]}"
            )
    finally:
        conn.close()


def migrate_add_files_processing_progress(sqlite_path: str) -> None:
    """Migration: add phase-aware processing-progress columns to files table.

    files.status stays in the canonical 4-value enum
    ('pending','processing','indexed','error'). All async/queued/parsing/
    chunking/embedding/writing-index detail lives in `phase` and friends.

    Also creates ``idx_files_hash_vault_status`` to back the in-flight
    duplicate check used by the async upload route. Without this index,
    the ``IN ('pending','processing','indexed')`` filter falls back to
    scanning every indexed row and the request-blocking dedup check
    becomes O(indexed_rows).

    Idempotent — safe to run multiple times.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }
        new_columns = (
            ("phase", "TEXT"),
            ("phase_message", "TEXT"),
            ("progress_percent", "REAL"),
            ("processed_units", "INTEGER"),
            ("total_units", "INTEGER"),
            ("unit_label", "TEXT"),
            ("phase_started_at", "TIMESTAMP"),
            ("processing_started_at", "TIMESTAMP"),
            ("wiki_pending", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, ddl in new_columns:
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_hash_vault_status "
            "ON files(file_hash, vault_id, status)"
        )
        conn.commit()
    finally:
        conn.close()


def migrate_add_files_enrichment_status(sqlite_path: str) -> None:
    """Migration: add independent post-index enrichment status columns."""
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }
        for name, ddl in (
            ("enrichment_status", "TEXT"),
            ("enrichment_error", "TEXT"),
            ("enrichment_updated_at", "TIMESTAMP"),
        ):
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")
        conn.commit()
    finally:
        conn.close()


def migrate_add_files_search_fts(sqlite_path: str) -> None:
    """Create and backfill the document-list metadata FTS index."""
    conn = sqlite3.connect(sqlite_path)
    try:
        def _drop_files_search_fts() -> None:
            for trigger in (
                "files_search_fts_insert",
                "files_search_fts_delete",
                "files_search_fts_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TABLE IF EXISTS files_search_fts")

        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        }
        # init_db() may have created FTS triggers before a legacy files table
        # gained newer metadata columns. Drop them before ALTER TABLE so SQLite
        # never has to compile triggers that reference missing OLD/NEW columns.
        if not {
            "file_type",
            "source",
            "email_subject",
            "email_sender",
            "document_date",
        }.issubset(existing_cols):
            _drop_files_search_fts()

        for name, ddl in (
            ("file_type", "TEXT"),
            ("source", "TEXT NOT NULL DEFAULT 'upload'"),
            ("email_subject", "TEXT"),
            ("email_sender", "TEXT"),
            ("document_date", "TEXT"),
            ("supersedes_file_id", "INTEGER"),
            ("ingestion_version", "INTEGER DEFAULT 1"),
        ):
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")

        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS files_search_fts USING fts5(
                file_name,
                file_type,
                status,
                source,
                email_subject,
                email_sender,
                document_date,
                content='files',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS files_search_fts_insert AFTER INSERT ON files BEGIN
                INSERT INTO files_search_fts(rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
                VALUES (new.id, new.file_name, new.file_type, new.status, new.source, new.email_subject, new.email_sender, new.document_date);
            END;

            CREATE TRIGGER IF NOT EXISTS files_search_fts_delete AFTER DELETE ON files BEGIN
                INSERT INTO files_search_fts(files_search_fts, rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
                VALUES ('delete', old.id, old.file_name, old.file_type, old.status, old.source, old.email_subject, old.email_sender, old.document_date);
            END;

            CREATE TRIGGER IF NOT EXISTS files_search_fts_update AFTER UPDATE ON files BEGIN
                INSERT INTO files_search_fts(files_search_fts, rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
                VALUES ('delete', old.id, old.file_name, old.file_type, old.status, old.source, old.email_subject, old.email_sender, old.document_date);
                INSERT INTO files_search_fts(rowid, file_name, file_type, status, source, email_subject, email_sender, document_date)
                VALUES (new.id, new.file_name, new.file_type, new.status, new.source, new.email_subject, new.email_sender, new.document_date);
            END;
            """
        )
        conn.execute("INSERT INTO files_search_fts(files_search_fts) VALUES('rebuild')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_add_files_content_fts(sqlite_path: str) -> None:
    """Migration: add ``files_content_fts`` FTS5 index over ``files.parsed_text``.

    This is the document *body* search index — kept separate from
    ``files_search_fts`` (metadata only) so content search can be tuned
    independently of filename/metadata search. Lives in a migration rather
    than the base schema because the indexed column (``parsed_text``) is itself
    added by a migration, so this must run after ``migrate_add_files_parsed_text``.

    The UPDATE trigger is guarded with ``WHEN new.parsed_text IS NOT old.parsed_text``
    so frequent metadata/progress writes during ingestion do not re-index the
    full document body on every row update.

    **Deployment note — one-time rebuild cost:** The first time this migration
    runs (i.e. when ``files_content_fts`` does not yet exist), it executes
    ``INSERT INTO files_content_fts(...) VALUES('rebuild')`` which re-indexes
    all existing ``parsed_text`` values in the files table. For databases with
    many large documents this can take several seconds to a few minutes. The
    migration is gated on table existence, so subsequent startups are a no-op.
    If you need to avoid startup latency on first deploy, run the migration
    manually before bringing up the service:

        python -c "from app.models.database import migrate_add_files_content_fts; \
migrate_add_files_content_fts('/path/to/app.db')"

    Idempotent — safe to run multiple times.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()
        ]
        if "parsed_text" not in existing_cols:
            # Defensive: normally added by migrate_add_files_parsed_text first.
            conn.execute("ALTER TABLE files ADD COLUMN parsed_text TEXT")

        # Check before running executescript so we know whether the table is
        # being created for the first time (rebuild needed) or already exists
        # (no rebuild — triggers keep the index in sync at runtime).
        table_is_new = (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master"
                " WHERE type='table' AND name='files_content_fts'"
            ).fetchone()[0]
            == 0
        )

        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS files_content_fts USING fts5(
                parsed_text,
                content='files',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS files_content_fts_insert AFTER INSERT ON files BEGIN
                INSERT INTO files_content_fts(rowid, parsed_text)
                VALUES (new.id, new.parsed_text);
            END;

            CREATE TRIGGER IF NOT EXISTS files_content_fts_delete AFTER DELETE ON files BEGIN
                INSERT INTO files_content_fts(files_content_fts, rowid, parsed_text)
                VALUES ('delete', old.id, old.parsed_text);
            END;

            CREATE TRIGGER IF NOT EXISTS files_content_fts_update AFTER UPDATE ON files
            WHEN new.parsed_text IS NOT old.parsed_text BEGIN
                INSERT INTO files_content_fts(files_content_fts, rowid, parsed_text)
                VALUES ('delete', old.id, old.parsed_text);
                INSERT INTO files_content_fts(rowid, parsed_text)
                VALUES (new.id, new.parsed_text);
            END;
            """
        )
        if table_is_new:
            # Backfill existing rows — only needed on first creation.
            # On subsequent startups the triggers keep the index current.
            conn.execute(
                "INSERT INTO files_content_fts(files_content_fts) VALUES('rebuild')"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_widen_wiki_claim_sources_source_kind(sqlite_path: str) -> None:
    """Migration: add 'wiki' to the wiki_claim_sources.source_kind CHECK constraint.

    SQLite cannot ALTER a CHECK constraint, so we use the rename-recreate-copy
    pattern. wiki_claim_sources has NO child tables referencing it, NO FTS
    virtual table, and NO triggers — simpler than the wiki_claims rebuild.

    We still set ``legacy_alter_table=ON`` and ``foreign_keys=OFF`` during the
    swap for safety, and validate FK integrity afterwards.

    Idempotent — safe to run multiple times.
    """
    conn = sqlite3.connect(sqlite_path)
    conn.isolation_level = None
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_claim_sources'"
        ).fetchone()
        if not tbl:
            return

        old_present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_claim_sources_old'"
        ).fetchone()
        if old_present and not tbl:
            conn.execute("PRAGMA legacy_alter_table = ON")
            conn.execute("ALTER TABLE wiki_claim_sources_old RENAME TO wiki_claim_sources")
            conn.execute("PRAGMA legacy_alter_table = OFF")
        elif old_present:
            conn.execute("DROP TABLE IF EXISTS wiki_claim_sources_old")

        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='wiki_claim_sources'"
        ).fetchone()
        if create_sql and "'wiki'" in create_sql[0]:
            return

        before_count = conn.execute("SELECT COUNT(*) FROM wiki_claim_sources").fetchone()[0]

        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            conn.execute("ALTER TABLE wiki_claim_sources RENAME TO wiki_claim_sources_old")

            conn.execute(
                """
                CREATE TABLE wiki_claim_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES wiki_claims(id) ON DELETE CASCADE,
                    source_kind TEXT NOT NULL CHECK (source_kind IN (
                        'document','memory','chat_message','manual','wiki')),
                    file_id INTEGER,
                    chunk_id TEXT,
                    memory_id INTEGER,
                    chat_message_id INTEGER,
                    source_label TEXT,
                    quote TEXT,
                    char_start INTEGER,
                    char_end INTEGER,
                    page_number INTEGER,
                    confidence REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                INSERT INTO wiki_claim_sources (
                    id, claim_id, source_kind, file_id, chunk_id, memory_id,
                    chat_message_id, source_label, quote, char_start, char_end,
                    page_number, confidence, created_at
                )
                SELECT id, claim_id, source_kind, file_id, chunk_id, memory_id,
                       chat_message_id, source_label, quote, char_start, char_end,
                       page_number, confidence, created_at
                FROM wiki_claim_sources_old
                """
            )

            after_count = conn.execute("SELECT COUNT(*) FROM wiki_claim_sources").fetchone()[0]
            if after_count != before_count:
                raise RuntimeError(
                    f"migrate_widen_wiki_claim_sources_source_kind: row-count "
                    f"parity failed ({before_count} -> {after_count}). "
                    f"wiki_claim_sources_old has been preserved."
                )

            conn.execute("DROP TABLE wiki_claim_sources_old")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_claim_sources_claim_id "
                "ON wiki_claim_sources(claim_id)"
            )
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")

        violations = conn.execute("PRAGMA foreign_key_check(wiki_claim_sources)").fetchall()
        if violations:
            raise RuntimeError(
                f"migrate_widen_wiki_claim_sources_source_kind: "
                f"foreign_key_check reported {len(violations)} violation(s): "
                f"{violations[:5]}"
            )
    finally:
        conn.close()


def migrate_add_wiki_relations_unique(sqlite_path: str) -> None:
    """Migration: add UNIQUE constraint on wiki_relations(subject_entity_id, predicate, object_entity_id).

    Deduplicates existing rows first (keeps highest id per triple), then
    creates a unique index. Idempotent.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_relations'"
        ).fetchone()
        if not tbl:
            return

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wiki_relations_unique_triple'"
        ).fetchone()
        if idx:
            return

        conn.execute(
            """DELETE FROM wiki_relations WHERE id NOT IN (
                SELECT MAX(id) FROM wiki_relations
                GROUP BY subject_entity_id, predicate, object_entity_id
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_wiki_relations_unique_triple "
            "ON wiki_relations(subject_entity_id, predicate, object_entity_id)"
        )
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_claims_unique_claim_text(sqlite_path: str) -> None:
    """Migration: add UNIQUE constraint on wiki_claims(vault_id, claim_text).

    Deduplicates existing rows first (keeps highest id per vault+claim pair),
    then creates a unique index.  Idempotent.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_claims'"
        ).fetchone()
        if not tbl:
            return

        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_wiki_claims_unique_vault_claim'"
        ).fetchone()
        if idx:
            return

        # Remove duplicate rows (keep the latest / highest-id per vault+claim pair)
        conn.execute(
            """DELETE FROM wiki_claims WHERE id NOT IN (
                SELECT MAX(id) FROM wiki_claims
                GROUP BY vault_id, claim_text
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_wiki_claims_unique_vault_claim "
            "ON wiki_claims(vault_id, claim_text)"
        )
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_page_hierarchy_and_versioning(sqlite_path: str) -> None:
    """Migration: add parent_id and version columns to wiki_pages. Idempotent."""
    conn = sqlite3.connect(sqlite_path)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(wiki_pages)").fetchall()]
        if "parent_id" not in cols:
            conn.execute("ALTER TABLE wiki_pages ADD COLUMN parent_id INTEGER REFERENCES wiki_pages(id) ON DELETE SET NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_pages_parent ON wiki_pages(parent_id)")
        if "version" not in cols:
            conn.execute("ALTER TABLE wiki_pages ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_supporting_tables(sqlite_path: str) -> None:
    """Migration: add wiki_page_versions, wiki_page_files, wiki_page_links,
    wiki_activity_log tables. Idempotent via IF NOT EXISTS."""
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wiki_page_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                vault_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                markdown TEXT NOT NULL DEFAULT '',
                summary TEXT DEFAULT '',
                status TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                edited_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_page_versions_page ON wiki_page_versions(page_id);

            CREATE TABLE IF NOT EXISTS wiki_page_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                vault_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(page_id, file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_page_files_page ON wiki_page_files(page_id);
            CREATE INDEX IF NOT EXISTS idx_wiki_page_files_file ON wiki_page_files(file_id);

            CREATE TABLE IF NOT EXISTS wiki_page_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                target_page_id INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
                vault_id INTEGER NOT NULL,
                link_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_page_id, target_page_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_page_links_source ON wiki_page_links(source_page_id);
            CREATE INDEX IF NOT EXISTS idx_wiki_page_links_target ON wiki_page_links(target_page_id);

            CREATE TABLE IF NOT EXISTS wiki_activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK (action IN (
                    'page_created','page_updated','page_deleted',
                    'claim_created','claim_updated','claim_deleted',
                    'entity_created','entity_updated',
                    'job_completed','job_failed',
                    'lint_resolved','lint_dismissed')),
                target_type TEXT NOT NULL CHECK (target_type IN (
                    'page','claim','entity','job','lint_finding')),
                target_id INTEGER NOT NULL,
                user_id INTEGER,
                details_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_activity_log_vault ON wiki_activity_log(vault_id, created_at DESC);
        """)
        conn.commit()
    finally:
        conn.close()


def migrate_add_wiki_claims_normalized_text(sqlite_path: str) -> None:
    """Migration: add normalized_text column + index to wiki_claims and backfill
    existing rows. Idempotent. Mirrors normalize_claim_text in wiki_store."""
    import re

    def _normalize(text: str) -> str:
        normalized = re.sub(r"[^\w\s]", "", (text or "").lower().strip())
        return re.sub(r"\s+", " ", normalized).strip()

    conn = sqlite3.connect(sqlite_path)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(wiki_claims)").fetchall()]
        if "normalized_text" not in cols:
            conn.execute("ALTER TABLE wiki_claims ADD COLUMN normalized_text TEXT")
            # Backfill existing rows (SQLite has no regex, so normalize in Python).
            rows = conn.execute("SELECT id, claim_text FROM wiki_claims").fetchall()
            for claim_id, claim_text in rows:
                conn.execute(
                    "UPDATE wiki_claims SET normalized_text = ? WHERE id = ?",
                    (_normalize(claim_text), claim_id),
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_claims_vault_normalized "
            "ON wiki_claims(vault_id, normalized_text)"
        )
        conn.commit()
    finally:
        conn.close()


def get_db_connection(sqlite_path: str) -> sqlite3.Connection:
    """
    Get a connection to the SQLite database.

    Args:
        sqlite_path: Path to the SQLite database file.

    Returns:
        sqlite3.Connection: Database connection with row factory set.
    """
    conn = sqlite3.connect(sqlite_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


class SQLiteConnectionPool:
    """
    A connection pool for SQLite databases.

    Manages a pool of reusable SQLite connections to improve performance
    in multi-threaded environments.
    """

    def __init__(self, sqlite_path: str, max_size: int = 5):
        """
        Initialize the connection pool.

        Args:
            sqlite_path: Path to the SQLite database file.
            max_size: Maximum number of connections in the pool.
        """
        self.sqlite_path = sqlite_path
        self.max_size = max_size
        self._pool = Queue(maxsize=max_size)
        self._lock = threading.Lock()
        self._created_count = 0
        self._closed = False

    def _create_connection(self) -> sqlite3.Connection:
        """
        Create a new SQLite connection with proper settings.

        Returns:
            sqlite3.Connection: A new database connection.

        Raises:
            sqlite3.Error: If connection creation fails.
        """
        try:
            conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            return conn
        except (sqlite3.Error, OSError):
            # Decrement created count on any failure
            with self._lock:
                self._created_count -= 1
            raise

    def _validate_connection(self, conn: sqlite3.Connection) -> bool:
        """
        Validate that a connection is still alive and usable.

        Args:
            conn: The connection to validate.

        Returns:
            bool: True if the connection is valid, False otherwise.
        """
        try:
            conn.execute("SELECT 1")
            conn.execute("PRAGMA foreign_keys = ON")
            return True
        except (sqlite3.DatabaseError, sqlite3.OperationalError):
            # Rollback any failed transaction state
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return False
        except sqlite3.Error:
            return False

    def get_connection(self, max_wait_attempts: int = 3) -> sqlite3.Connection:
        """
        Get a connection from the pool.

        If the pool has available connections, returns one from the pool.
        Otherwise, creates a new connection if under max_size limit.
        Validates connections before returning them.

        Args:
            max_wait_attempts: Maximum number of wait attempts when pool is at capacity.

        Returns:
            sqlite3.Connection: A database connection.

        Raises:
            RuntimeError: If the pool has been closed or max wait attempts exhausted.
        """
        if self._closed:
            raise RuntimeError("Connection pool has been closed")

        attempts = 0
        while attempts < max_wait_attempts:
            # Try to get an existing connection from the pool
            try:
                conn = self._pool.get_nowait()
                # Validate the connection before returning it
                if self._validate_connection(conn):
                    return conn
                else:
                    # Connection is invalid, decrement count and try again
                    with self._lock:
                        self._created_count -= 1
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    continue
            except Empty:
                pass

            # No available connections, try to create a new one if under limit
            with self._lock:
                if self._created_count < self.max_size:
                    self._created_count += 1
                    try:
                        return self._create_connection()
                    except sqlite3.Error:
                        self._created_count -= 1
                        raise

            # If at max capacity, block until a connection is available
            try:
                conn = self._pool.get(timeout=5)
                # Validate the connection before returning it
                if self._validate_connection(conn):
                    return conn
                else:
                    # Connection is invalid, decrement count and try again
                    with self._lock:
                        self._created_count -= 1
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    continue
            except Empty:
                # Timeout occurred, increment attempts and retry
                attempts += 1
                continue

        # Max attempts exhausted
        raise RuntimeError(
            f"Could not obtain a connection from the pool after {max_wait_attempts} attempts"
        )

    def release_connection(self, conn: sqlite3.Connection) -> None:
        """
        Release a connection back to the pool.

        Args:
            conn: The connection to release back to the pool.

        Raises:
            RuntimeError: If the pool has been closed.
        """
        if self._closed:
            raise RuntimeError("Connection pool has been closed")

        try:
            self._pool.put_nowait(conn)
        except Full:
            # Pool is full, close the connection
            conn.close()

    def close_all(self) -> None:
        """
        Close all connections in the pool.

        This closes all pooled connections and prevents further use of the pool.
        """
        with self._lock:
            self._closed = True
            # Close all connections in the pool using while True/except Empty pattern
            while True:
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Empty:
                    break

    @contextmanager
    def connection(self):
        """
        Context manager for getting and releasing a connection.

        Automatically releases the connection back to the pool when done.

        Example:
            with pool.connection() as conn:
                cursor = conn.execute("SELECT * FROM table")
                results = cursor.fetchall()
        """
        conn = None
        try:
            conn = self.get_connection()
            yield conn
        finally:
            if conn is not None:
                try:
                    self.release_connection(conn)
                except (RuntimeError, sqlite3.Error):
                    # Ignore release errors to avoid masking original exception
                    pass


# Global pool cache for singleton pattern
_pool_cache: dict[str, SQLiteConnectionPool] = {}
_pool_cache_lock = threading.Lock()


def get_pool(sqlite_path: str, max_size: int = 5) -> SQLiteConnectionPool:
    """
    Get or create a connection pool for the given SQLite path.

    This function implements a singleton pattern, returning the same
    pool instance for the same sqlite_path.

    Args:
        sqlite_path: Path to the SQLite database file.
        max_size: Maximum number of connections in the pool.

    Returns:
        SQLiteConnectionPool: A connection pool instance.
    """
    global _pool_cache

    with _pool_cache_lock:
        if sqlite_path not in _pool_cache:
            _pool_cache[sqlite_path] = SQLiteConnectionPool(sqlite_path, max_size)
        return _pool_cache[sqlite_path]


@contextmanager
def transaction_context(conn: sqlite3.Connection):
    """Context manager for database transactions.

    Usage:
        with transaction_context(conn):
            conn.execute("INSERT ...")
            conn.execute("UPDATE ...")
        # Automatically commits on success, rolls back on exception

    Args:
        conn: SQLite database connection

    Yields:
        The connection object (for convenience)

    Raises:
        Any exception that occurs during the transaction (after rollback)
    """
    conn.execute("BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_assign_orphan_users_to_default_vault(sqlite_path: str) -> None:
    """
    Migration: Assign existing users without vault access to the Default vault.

    Idempotent — uses INSERT OR IGNORE and a NOT IN subquery to only insert
    rows for users who have zero vault_members entries.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        # Find the Default vault ID
        cursor = conn.execute("SELECT id FROM vaults WHERE name = ?", ("Default",))
        row = cursor.fetchone()
        if not row:
            # Create Default vault if it doesn't exist
            cursor = conn.execute(
                "INSERT INTO vaults (name, description) VALUES (?, ?)",
                ("Default", "Default vault for all users"),
            )
            default_vault_id = cursor.lastrowid
        else:
            default_vault_id = row[0]

        # Assign all users without any vault_members to Default vault
        conn.execute(
            """INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission)
               SELECT ?, id, 'read' FROM users
               WHERE id NOT IN (SELECT DISTINCT user_id FROM vault_members)""",
            (default_vault_id,),
        )
        conn.commit()
        logger.info("Orphan users assigned to Default vault")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_add_wiki_lint_findings_json_check(sqlite_path: str) -> None:
    """
    Migration: Add CHECK constraints to wiki_lint_findings JSON columns.

    Ensures related_page_ids_json and related_claim_ids_json always contain
    valid JSON arrays, preventing json_each() errors from NULL or malformed
    values. SQLite does not support ALTER TABLE ADD CONSTRAINT, so the table
    is recreated with the CHECK constraints.

    Idempotent — if the CHECK constraints already exist (new databases), the
    function exits early after detecting them in the schema SQL.

    Normalisation: any pre-existing value that is not a valid JSON array
    (NULL, empty string, JSON null, objects, scalars, malformed JSON) is
    silently coerced to '[]' during the backfill.  Uses json_valid() as the
    outer guard so that truly malformed text (which would raise an
    OperationalError inside json_type()) is handled safely.
    """
    # Use autocommit mode so we can wrap DDL + DML in a single explicit
    # transaction.  Python's sqlite3 with the default isolation_level=''
    # auto-commits DDL statements (RENAME, CREATE TABLE) outside any
    # implicit transaction, which would leave the database in a broken
    # half-migrated state if the INSERT later fails.  Matches the pattern
    # used by migrate_add_curator_claim_support and
    # migrate_widen_wiki_claim_sources.
    conn = sqlite3.connect(sqlite_path)
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        # Recovery prologue: if a prior run crashed after RENAME but before
        # COMMIT, _wiki_lint_findings_old may be present alongside an empty
        # (or absent) wiki_lint_findings.  Restore the canonical name so the
        # rest of the migration can proceed correctly.
        old_present = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='_wiki_lint_findings_old'"
        ).fetchone()
        new_present = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='wiki_lint_findings'"
        ).fetchone()

        if old_present and not new_present:
            # Crashed after RENAME before CREATE TABLE; rename back and retry.
            logger.warning(
                "migrate_add_wiki_lint_findings_json_check: detected "
                "_wiki_lint_findings_old without wiki_lint_findings; restoring."
            )
            conn.execute(
                "ALTER TABLE _wiki_lint_findings_old "
                "RENAME TO wiki_lint_findings"
            )
            old_present = None
        elif old_present and new_present:
            # Both tables exist: a prior INSERT failed after DDL committed.
            # Drop the stale backup so the RENAME below won't collide.
            logger.warning(
                "migrate_add_wiki_lint_findings_json_check: detected stale "
                "_wiki_lint_findings_old alongside wiki_lint_findings; "
                "dropping stale table before re-running migration."
            )
            conn.execute("DROP TABLE _wiki_lint_findings_old")
            old_present = None

        # Check if the table exists at all
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='wiki_lint_findings'"
        ).fetchone()
        if not row:
            return
        if "json_type(related_page_ids_json)" in row[0]:
            # CHECK constraints already present — nothing to do.
            return

        # Wrap the entire RENAME → CREATE → INSERT → DROP sequence in one
        # explicit transaction so a mid-migration failure is fully atomic.
        conn.execute("BEGIN")
        conn.execute(
            "ALTER TABLE wiki_lint_findings RENAME TO _wiki_lint_findings_old"
        )
        conn.execute("""
            CREATE TABLE wiki_lint_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_id INTEGER NOT NULL,
                finding_type TEXT NOT NULL CHECK (finding_type IN (
                    'contradiction','stale','orphan','missing_page',
                    'unsupported_claim','duplicate_entity','weak_provenance')),
                severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
                    'low','medium','high','critical')),
                title TEXT NOT NULL,
                details TEXT DEFAULT '',
                related_page_ids_json TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_type(related_page_ids_json) = 'array'),
                related_claim_ids_json TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_type(related_claim_ids_json) = 'array'),
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                    'open','acknowledged','resolved','dismissed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Normalise: any value that is not a valid JSON array (NULL, empty
        # string, JSON null, object, scalar, or malformed JSON) is coerced to
        # '[]'.  json_valid() is used as the outer guard because json_type()
        # raises OperationalError on malformed JSON (e.g. 'not-json'), while
        # json_valid() returns 0 without raising for all invalid inputs.
        conn.execute("""
            INSERT INTO wiki_lint_findings
                (id, vault_id, finding_type, severity, title, details,
                 related_page_ids_json, related_claim_ids_json,
                 status, created_at, updated_at)
            SELECT
                id, vault_id, finding_type, severity, title, details,
                CASE WHEN json_valid(related_page_ids_json)
                          AND json_type(related_page_ids_json) = 'array'
                     THEN related_page_ids_json ELSE '[]' END,
                CASE WHEN json_valid(related_claim_ids_json)
                          AND json_type(related_claim_ids_json) = 'array'
                     THEN related_claim_ids_json ELSE '[]' END,
                status, created_at, updated_at
            FROM _wiki_lint_findings_old
        """)
        conn.execute("DROP TABLE _wiki_lint_findings_old")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_wiki_lint_findings_vault_status_severity
            ON wiki_lint_findings(vault_id, status, severity)
        """)
        conn.execute("COMMIT")
        logger.info("Added CHECK constraints to wiki_lint_findings JSON columns")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
