# KMS Content Search + RAG Integration (Phase 1.5 + Phase 2)

## Problem Statement

Two gaps in the KMS (Knowledge Management System) prevent it from delivering full value:

1. **DD-C002 (CRITICAL) — No content-level document search.** The `files_search_fts` FTS5 index covers only metadata (filename, status, source, email fields). Users cannot search document body text without going through the RAG chat pipeline. This is the top blocker for the KMS as a standalone documentation system.

2. **Phase 2 gap — KMS entries are unreachable from chat.** KMS entries exist in the database but are never retrieved for RAG queries. Wiki entries appear in chat as `[W#]` citations with cards; KMS entries have no equivalent. The retrieval pipeline is: query → vector search → wiki retrieval → LLM prompt → response. KMS has no place in this chain.

Additionally, two pre-existing bugs were confirmed during Phase 1 review:

3. **Wiki retrieval silent failure.** `WikiRetrievalService` uses `.get()`/`.put()` pool interface but the production pool exposes `.get_connection()`/`.release_connection()`. This causes silent `[]` returns in production. The FTS alias form (`FROM wiki_claims_fts fts WHERE fts MATCH ?`) also fails with "no such column: fts" in this SQLite build.

4. **KMS route bugs from master.** Duplicate `require_kms_enabled` definitions (sync 403 captured by router; async 503 shadowed). Duplicate `_safe_record_action` calls per delete. CSRF test override leak causing two CSRF-protection tests to receive 201 instead of 403.

## Goals

- Users can search document body text from the document list endpoint (Phase 1.5).
- KMS entries are retrieved alongside wiki entries during RAG queries and surfaced as `[K#]` citations with cards in the chat UI (Phase 2).
- Wiki retrieval works in production (pool interface + FTS alias fix).
- All four master-introduced bugs are fixed.

## Non-Goals

- Folder/tag organization (Phase 3 per issue #119).
- Document detail page (Phase 3).
- DecomposE DocumentsPage (Phase 3).
- Vector-based KMS search (FTS5 is sufficient for Phase 2).

## Design Decisions

- **Content FTS**: Separate `files_content_fts` table (not adding `parsed_text` to existing metadata FTS). This avoids polluting metadata search results with body-text hits and keeps indexing concerns separated.
- **KMS citations**: Full wiki-parallel — `[K#]` labels, `KMSCards` component (emerald theme), side-pane tab, gated by `kms_enabled`.
- **Pool interface**: Dual-interface `_acquire()`/`_release()` pattern checks for `.get_connection` attr first, falls back to `.get()`. This supports both production pool and test pools without modifying either.
- **FTS queries**: Use full table name in FROM + MATCH (not alias form). Applies to both `wiki_retrieval.py` and `kms_retrieval.py`.
- **Chat persistence**: `kms_refs` column added to `chat_messages` via migration; side-write/side-fetch pattern (matching `mode` column pattern) to avoid bifurcating branched SELECTs.

## Architecture

### Phase 1.5: `files_content_fts`

```
files_content_fts (FTS5 external-content, content='files', content_rowid='id')
  - parsed_text column
  - INSERT trigger: after INSERT on files
  - UPDATE trigger: after UPDATE on files WHEN new.parsed_text IS NOT old.parsed_text
  - DELETE trigger: before DELETE on files

Migration: migrate_add_files_content_fts()
  - Registered in run_migrations() after migrate_add_files_search_fts
  - INSERT OR IGNORE to skip if column exists
  - Backfill: INSERT INTO files_content_fts(rowid, parsed_text) SELECT id, parsed_text FROM files WHERE parsed_text IS NOT NULL

list_documents() search:
  - OR id IN (SELECT rowid FROM files_content_fts WHERE files_content_fts MATCH ?)
  - Bound to the same `search` parameter
```

### Phase 2: KMS RAG Integration

```
KMSRetrievalService (backend/app/services/kms_retrieval.py)
  - Pool: dual-interface _acquire()/_release()
  - retrieve(query, vault_id, top_k=5) -> List[KMSEvidence]
  - FTS query: kms_entries_fts MATCH ? JOIN kms_entries WHERE status IN ('draft','published')
  - Returns KMSEvidence(id, vault_id, title, body, summary, tags, slug, label)
  - Gated: settings.kms_enabled check before query

RAGEngine (rag_engine.py)
  - __init__ gets kms_retrieval: Optional[Any] = None
  - Retrieval: after wiki block, asyncio.to_thread(self._kms_retrieval.retrieve, query, vault_id)
  - Citation: parse_kms_citations(full_response) → cited_kms labels
  - _build_done_message: emits kms_used list

PromptBuilder (prompt_builder.py)
  - format_kms_evidence(evidence, index) → KMS context block
  - build_messages: KMS sections injected after wiki, [K#] in CITATION_INSTRUCTION

CitationValidator (citation_validator.py)
  - _CITATION_RE: r"\[(S|M|W|K)(\d+)\]"
  - parse_kms_citations(content) → List[dict]
  - validate_and_repair_citations: kms_count param

Chat routes (chat.py)
  - ChatResponse + AddMessageRequest: kms_used / kms_refs fields
  - Streaming: collect kms_used from done chunk, emit in both error/normal done
  - Persistence: migrate_add_kms_refs migration; side-write after INSERT
  - Get messages: side-fetch kms_by_id dict
  - Fork: propagate kms_refs to new rows

Lifespan (lifespan.py)
  - KMSRetrievalService(pool=app.state.db_pool)
  - Passed to RAGEngine as kms_retrieval=app.state.kms_retrieval

Frontend
  - KMSReference interface in api.ts
  - onKMS callback in ChatStreamCallbacks
  - KMSCards.tsx (emerald theme, links to /kms/${entry_id})
  - kmsRefs on Message in useChatStore.ts
  - useLastCompletedAssistantKmsRefs selector
  - AssistantMessage.tsx: renders KMSCards after WikiCards
  - RightPane.tsx: "Knowledge" tab alongside Wiki tab
  - MarkdownMessage.tsx: [K#] citation spans (emerald color)
```

## Files Changed

### New Files
- `backend/app/services/kms_retrieval.py`
- `backend/tests/test_documents_content_search.py`
- `backend/tests/test_kms_retrieval.py`
- `frontend/src/components/chat/KMSCards.tsx`
- `frontend/src/components/chat/KMSCards.test.tsx`

### Modified Files
- `backend/app/models/database.py` — `migrate_add_files_content_fts`, `migrate_add_kms_refs`
- `backend/app/api/routes/documents.py` — content FTS search, fix duplicate audit calls
- `backend/app/api/routes/kms.py` — consolidated `require_kms_enabled` (async 503)
- `backend/app/api/routes/chat.py` — kms_used/kms_refs fields, persistence, fork
- `backend/app/lifespan.py` — KMSRetrievalService init + pass to RAGEngine
- `backend/app/services/citation_validator.py` — K citations
- `backend/app/services/prompt_builder.py` — KMS evidence injection
- `backend/app/services/rag_engine.py` — KMS retrieval block
- `backend/app/services/wiki_retrieval.py` — pool interface + FTS alias fix
- `backend/tests/test_kms_routes.py` — CSRF test override leak fix
- `backend/tests/test_wiki_retrieval.py` — pool interface + end-to-end test
- `frontend/src/lib/api.ts` — KMSReference type, callbacks, SSE parsing
- `frontend/src/stores/useChatStore.ts` — kmsRefs on Message, selector
- `frontend/src/components/chat/AssistantMessage.tsx` — KMSCards rendering
- `frontend/src/components/chat/MarkdownMessage.tsx` — [K#] citation spans
- `frontend/src/components/chat/RightPane.tsx` — Knowledge tab
- `frontend/src/components/chat/RightPane.test.tsx` — mock update
- `frontend/src/components/chat/RightPane.adversarial.test.tsx` — mock update
- `frontend/src/components/chat/RightPane.virtualization.test.tsx` — mock update
- `frontend/src/hooks/useSendMessage.ts` — kms_refs handling
- `frontend/src/pages/ChatShell.tsx` — kms_refs pass-through

## Acceptance Criteria

### Phase 1.5 — Content Search
- [ ] `GET /documents?vault_id=X&search=foo` returns documents whose `parsed_text` contains "foo", even when "foo" is absent from metadata fields.
- [ ] Documents without matching `parsed_text` but with matching metadata still appear (OR logic, not replacement).
- [ ] `files_content_fts` migration runs idempotently (re-running does not error).
- [ ] Backfill populates FTS for all existing documents with `parsed_text IS NOT NULL`.

### Phase 2 — KMS RAG Integration
- [ ] With `kms_enabled=true`, a RAG chat response citing a KMS entry includes `[K1]` (or `[K#]`) in the response text.
- [ ] The SSE `done` event includes `kms_used: [...]` with the referenced entries.
- [ ] `kms_refs` is persisted in `chat_messages` and returned in the get-messages response.
- [ ] `KMSCards` render in the chat UI after the assistant message, using emerald styling.
- [ ] The "Knowledge" tab in RightPane shows KMS cards from the last assistant message.
- [ ] With `kms_enabled=false`, KMS retrieval is skipped and `kms_used` is empty.
- [ ] Wiki retrieval returns results in production (pool interface + FTS alias fixed).

### Bug Fixes
- [ ] `GET /kms/entries` without `kms_enabled` returns 503 (not 403).
- [ ] Delete endpoints log exactly one audit entry per file delete.
- [ ] `test_create_entry_without_csrf_returns_403` and `test_recompile_without_csrf_returns_403` both return 403.

## Test Cases

### Backend
| Test file | Coverage |
|-----------|----------|
| `test_documents_content_search.py` | Content match, no-match exclusion, filename regression (3 tests) |
| `test_kms_retrieval.py` | Status filtering, vault scoping, label assignment, disabled flag, empty query, to_dict shape (9 tests) |
| `test_kms_routes.py` | CSRF protection (existing, fixed), 503 master switch, CRUD operations |
| `test_wiki_retrieval.py` | Pool interface fix, FTS alias fix, end-to-end with real DB |

### Frontend
| Test file | Coverage |
|-----------|----------|
| `KMSCards.test.tsx` | Empty/null rendering, container, per-ref cards, label badge, tags, expand/collapse (6 tests) |
| `RightPane.test.tsx` | Mock updated for `useLastCompletedAssistantKmsRefs` |
| `RightPane.adversarial.test.tsx` | Mock updated |
| `RightPane.virtualization.test.tsx` | Mock updated |

## Quality Gates

- Backend: `cd backend && python -m pytest tests/test_documents_content_search.py tests/test_kms_retrieval.py tests/test_kms_routes.py tests/test_wiki_retrieval.py -v`
- Frontend: `cd frontend && npx vitest run && npx tsc --noEmit`
