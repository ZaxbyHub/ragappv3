# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Security

- **Prompt injection hardening Phase 1 (issue #209)**: XML special-character escaping (`html.escape`, default `quote=True`) now applied to all five untrusted-content boundary sites inside their XML wrappers: `<user_query>` (user input), `<document>` (retrieved chunks, including parent-window text), `<memory>` (memory records), `<wiki_evidence>` (compiled wiki body), and `<kms_evidence>` (KMS excerpt). All five XML special characters (`<`, `>`, `&`, `'`, `"`) are escaped. Closes CRITICAL-1, HIGH-1, HIGH-2 from issue #209 v8 review. Test suite (`test_prompt_builder_xml_escape.py`) covers 20 adversarial test methods across injection payloads, double-encoding, Unicode/CJK/emoji, 10 KB+ payloads, parent-window path, empty strings, and edge cases.

- **FR-4 – CI test scope expansion (Phase 2, issue #209)**: Replaced the 8-file test allow-list in `.github/workflows/ci.yml` with `pytest --tb=short -v tests/`, expanding CI to run all 3934 backend tests including 21 scope-escalation adversarial tests, 1 prompt-injection test, 5 CSRF tests, 18 vault authorization tests, and 3 KMS authorization tests. Closes CRITICAL-2 from v8 review.

- **FR-5 – X-Scopes privilege-escalation fix (Phase 2, issue #209)**: Closed the client-supplied `X-Scopes` header attack vector (HIGH-3 from v8 review) by migrating `require_scope` to derive scopes exclusively from `settings.admin_token_scopes`, a server-side `token → [scopes]` mapping. The `X-Scopes` header is no longer read. Defense-in-depth measures added: `secrets.compare_digest` used for token comparison (authn-before-authz order, preventing oracle attacks), an empty-token guard returns HTTP 503 before any lookup, and `authn-before-authz` ordering is enforced. Both `admin.py` and `email.py` routes migrated from local `require_admin_scope` to the centralized `require_scope`. New `admin_token_scopes: dict[str, list[str]]` field added to `config.py` with a Pydantic `model_validator`. Regression suite (`test_security_scope_escalation.py`) includes 15 tests covering HTTP-layer proof via `TestClient`.

- **FR-6 – bounded asyncio.Queue DoS mitigation (Phase 2, issue #209)**: `BackgroundProcessor` now constructs both `self.queue` and `self.enrichment_queue` with `asyncio.Queue(maxsize=settings.ingestion_queue_max_size)`, preventing unbounded memory growth under load. `ingestion_queue_max_size: int = 1000` added to `config.py`. Queue producers that hit full capacity simply wait rather than enqueuing unboundedly. Regression suite (`test_background_tasks_bounded_queue.py`) includes 6 tests. Closes HIGH-4 from v8 review.

### Added

- **Native TEI embedding endpoint support**: the embedding client now supports a third provider mode for HuggingFace Text Embeddings Inference (TEI) servers that expose only the native `POST /embed` route (`{"inputs": ...}`, raw array-of-arrays response) and not the OpenAI-compatible `/v1/embeddings` route. Detection: an explicit `/embed` path or a bare `host:8080` URL resolves to native TEI; an explicit `/v1/embeddings` path still selects OpenAI mode. The model-availability check (`ModelChecker`) and the startup `STRICT_EMBEDDING_MODEL_CHECK` now probe TEI's `/info` endpoint (at the server root) for native-TEI URLs. The Settings UI documents the native-TEI URL form.
- **Wiki master switch enforced (Phase 1)**: `wiki_enabled` is now a hard gate — all wiki routes (GET/POST/PUT/DELETE) return HTTP 503 when disabled. Previously the flag was documented but inert. Job enqueueing in `document_processor.py` is also gated on `wiki_enabled AND wiki_compile_on_ingest`. The flag is now persisted in `NEW_DIRECT_KEYS` for runtime reload.
- **KMS content search (Phase 1.5)**: Document body text is now searchable via `GET /api/documents?search=`. A new `files_content_fts` FTS5 external-content table indexes `files.parsed_text`; triggers keep it in sync; `list_documents` ORs content-FTS hits alongside metadata-FTS hits. Fixes DD-C002, the critical blocker from issue #119.
- **KMS RAG integration (Phase 2)**: KMS entries now surface in chat as `[K#]` citations alongside wiki `[W#]` citations. `KMSRetrievalService` performs FTS-backed retrieval (gated by `kms_enabled`), injected into the RAG pipeline after wiki retrieval. The SSE `done` event emits `kms_used`; `kms_refs` is persisted per chat message. The frontend renders `KMSCards` (emerald theme) after assistant messages and adds a "Knowledge" tab in the right-pane source viewer.
- KMS citation support in `CitationValidator`: `[K#]` labels parsed by `parse_kms_citations()`; K-citations included in repair logic.
- **Wiki CSRF protection (Phase 2)**: All 13 wiki mutating routes (POST/PUT/DELETE) now require `_csrf_token: str = Depends(csrf_protect)`; GET routes remain CSRF-exempt. `TestWikiCSRFProtection` test suite added.
- **Document organization (Phase 3)**: Normalized `tags` + `document_tags` tables (vault-scoped, `ON DELETE CASCADE`) with a `TagStore` service and CRUD/assignment routes under `/api/tags`. The document list (`GET /api/documents`) now accepts `sort_by` (`created_at`/`file_name`/`file_size`/`status`), `sort_order`, and `tag_id` filtering, and returns assigned tags per document (batched, no N+1). New `GET /api/documents/{id}` returns a single document with its tags and vault. Frontend: a dedicated document detail page at `/documents/:documentId` (metadata, tag editing, inline text/PDF preview), clickable sortable column headers, a tag filter, and a bulk tag-assignment dialog. `DocumentsPage` decomposed into focused components (`DocumentTable`, `DocumentCardsList`, `UploadQueue`, `UploadDropzone`, `DocumentStatsCards`, `RejectedFilesBanner`, `ConfirmDialog`, `TagFilter`, `BulkTagDialog`) and hooks (`useDocumentPolling`, `useBulkSelection`).
- **Codebase review skill v8.2**: new `.opencode/skills/codebase-review-swarm/` skill for quote-grounded full-repo audits with Phase 0 inventory, non-diluting selected-track depth, reviewer validation, and Phase 2M MEDIUM/LOW finalization. Writes artifacts under `.swarm/review-v8/runs/<run_id>/`. Thin-pointer adapters in `.claude/skills/` and `.agents/skills/` keep the three runner trees in sync per `AGENTS.md:31`.
- **Embedding-based semantic chunking wired up (issue #229 E1)**: `semantic_chunking_strategy = "embedding"` now actually selects the existing `EmbeddingSemanticChunker` (cosine-similarity breakpoints over sentence-window embeddings) during ingestion; it previously had no effect and the title-based chunker always ran. The default `"title"` behavior is unchanged, and the embedding strategy falls back to title-based chunking with a warning when no embedding service is available.

### Fixed

- **Deleted documents can no longer leave chunks silently searchable (issue #219)**: when removing a document's LanceDB chunks fails during a delete, the failure is now durably recorded in a new `vector_delete_pending` table (in the same transaction as the file-row delete) and a background sweep in `BackgroundProcessor` retries the chunk delete at startup and hourly thereafter, removing the pending row only on confirmed success (capped at 10 attempts, then left for operator review). Previously the failure was only logged and the orphaned chunks stayed retrievable forever.
- **Partial embedding failures are no longer invisible (issue #221)**: documents that index with up to 50% of their chunks dropped due to embedding failures previously showed a plain "Indexed" status with only a backend log line. The dropped-chunk count is now persisted to a new `files.chunks_failed` column, surfaced through the documents API, and rendered as an amber "Partially indexed" badge (with a failed-chunk tooltip) in the document table and cards.
- **Hallucinated citations no longer persist in streamed chats (#217)**: the streaming chat path computed citation validation but discarded the cleaned text, so invalid references (e.g. `[S99]` with no matching source) were rendered as citation chips and persisted in history. The stream now emits the citation-repaired content on the `done` event and the client adopts it before display/persistence. The repair pass was also made non-destructive — it only tidies whitespace where a citation was actually stripped and never collapses code indentation (which also fixes the prior code-mangling on the non-stream path).
- **Documents list pagination (#218)**: the documents page fetched only the first 50 rows and ignored the server `total`, silently hiding the rest of a large vault. It now shows "Showing N of M documents" with a "Load more" control that grows the fetched window (search and sort remain server-side); the backend `per_page` cap was raised to 1000.
- **Confirmed document deletes are no longer lost on navigation (#223)**: the single-document delete deferred the real API call inside a 3-second undo window, and unmounting the page cancelled the pending timer — so navigating away within 3s silently discarded a delete the user had already confirmed. Pending deletes are now flushed (executed) on unmount instead of cancelled.
- **Chat streams now abort on session switch**: switching or forking a conversation while a response was streaming left the orphan stream running — the new session's composer stayed locked in "Generating" and the assistant reply was silently dropped (the old session ended with a dangling user message). `loadChat`/`newChat` now abort the in-flight stream and reset streaming state before switching.
- **RRF recency no longer dominates relevance**: raw RRF scores (~0.016 for a top hit at k=60) were blended directly with recency scores spanning [0, 1], so the recency tiebreaker outweighed the relevance signal and biased candidate selection toward recently-uploaded content. RRF scores are now normalized against the strongest hit before blending, so recency contributes exactly `retrieval_recency_weight`.
- **Streaming reasoning filter no longer truncates answers**: the legacy bare markers `_lhs` and `Thinking Process:` were treated as thinking-block openers anywhere in the response, silently swallowing the rest of an answer when those substrings appeared in genuine content (e.g. an identifier like `expr_lhs`). They are now honored only at the very start of the response; the XML `<think>` handling is unchanged.
- **Forced password change is now usable**: a user flagged with `must_change_password` (e.g. an admin-created account with a temporary password) previously hit a wall of 403s with no way to change their password, and a reload logged them out. The app now routes flagged users to a dedicated change-password screen and rehydrates the flag on reload. Backend: read-only `GET /auth/me` is exempt from the gate so the SPA can detect the flag (mutating `PATCH /auth/me` stays blocked); login now returns `must_change_password`.
- **Wrapped native-TEI embedding responses**: the native-TEI client now accepts both a raw `[[...]]` array and a `{"embeddings": [[...]]}` wrapper (some TEI-compatible servers return the latter) on both the single and batch paths, fixing 100% chunk-embedding failures against those servers.
- **spaCy model missing on fresh containers**: `en_core_web_sm` (used by `unstructured`) is now pre-downloaded at image build time. Previously the runtime auto-download failed with a permission error under the non-root container user (root-owned `site-packages`), breaking the first document parse.
- **Docker SSRF guard blocked the bundled model services**: the stock `docker-compose.yml` now passes `ALLOW_LOCAL_SERVICES` (default `1`) and `ALLOW_LOCAL_CURATOR` through to the container, so the private-network `harrier-embed`/`reranker` endpoints are reachable at startup instead of being blocked by the SSRF guard.
- **`reranker_url` now SSRF- and format-validated at settings-change time**: the reranker endpoint URL was a persisted, user-settable field but was excluded from both the URL format validator and the PUT-time `assert_url_safe` SSRF check, so a private/internal or malformed reranker URL could be saved without validation. It is now validated like `ollama_embedding_url`/`ollama_chat_url`/`instant_chat_url`.
- **DD-C011 — non-atomic vault-wide delete**: `delete_all_vault_documents` deleted file rows one at a time, each in its own commit, so a mid-batch failure left the vault partially deleted. It now purges derived data (vector chunks, wiki claims) per file, then deletes every `files` row for the vault inside a single `BEGIN IMMEDIATE` transaction (with audit rows in the same transaction), rolling back on any failure.
- **`POST /api/documents/batch` authorization gate**: batch delete had no admin gate, so a caller with no admin capability anywhere received a `422` (malformed body) or an all-failed `200` instead of a clear authorization error. It is now guarded by `require_document_admin` — app admins/superadmins or users holding admin permission on at least one accessible vault — returning `403 "Admin access required"` before the body is parsed. Per-file vault-admin checks inside the handler are unchanged.
- **`test_vault_document_permissions_regression` could not run**: the fixture seeded `vault_members`/`files` for `vault_id=1` without creating vault 1 (FK violation, all 6 tests errored on collection), and `test_vault_list_includes_effective_permission_and_public_read` queried the admin-only `/api/vaults` instead of `/api/vaults/accessible` (always 403 for a member). Both are test-only fixes; no production code changed.

- **Wiki retrieval silent failure**: `WikiRetrievalService` returned empty results in production due to two bugs: (1) pool interface mismatch (service used `.get()`/`.put()` but the production pool exposes `.get_connection()`/`.release_connection()`); (2) FTS alias form (`FROM table alias WHERE alias MATCH ?`) fails in this SQLite build. Both fixed — dual-interface `_acquire`/`_release` pattern and full table names in FTS queries.
- **`files_content_fts` rebuild on every startup**: `migrate_add_files_content_fts` was running `VALUES('rebuild')` unconditionally, re-indexing all document bodies on every application restart. Now gated on whether the FTS table is being created for the first time — subsequent startups are a no-op.
- **KMS content FTS cross-vault leak (performance)**: The `files_content_fts` subquery in `list_documents` was unscoped, causing the FTS engine to search across all vaults before the outer `WHERE vault_id = ?` narrowed results. Now uses a vault-scoped JOIN when `vault_id` is provided.
- **KMS duplicate `require_kms_enabled`**: The sync 403 version was captured by the router before the async 503 version was defined. Consolidated to a single async 503 version defined before `router = APIRouter(...)`.
- **Document delete double audit**: `batch_delete_documents` and `delete_all_vault_documents` emitted two `_safe_record_action("delete")` calls per file after PR #127 introduced a second call while the first (from Phase 1) was already present.
- **KMS CSRF test override leak**: `TestKMSCSRFProtection.setUp` called `super().setUp()` which installed `app.dependency_overrides[csrf_protect]` but never removed it, causing CSRF-protected endpoints to bypass protection and return 201 instead of 403 in CSRF tests.
- **PR #153 review v7 follow-up (F-001…F-009)**: fixed a `must_change_password` account-lockout deadlock — the exempt path set checked `/auth/change-password` but the auth router mounts under `/api`, so the real runtime path never matched and flagged users were locked out of the only recovery endpoint (now suffix-matched). Closed an SSRF gap where a hot-swapped `ollama_embedding_url` reached `EmbeddingService` (which reads it live) without `assert_url_safe` — the URL is now validated at the settings-change boundary before it lands in the live config. Added `csrf_protect` to the remaining mutating endpoints (auth session revoke, all user-management mutations including delete, and the six document upload/scan/delete routes). Restored `must_change_password` to the `GET /auth/me` response (an API-contract regression). Closed a first-user-superadmin TOCTOU race by moving the user-count check inside the registration `BEGIN IMMEDIATE` transaction. Added change-password CSRF enforcement coverage and put the change-password / must-change-password security suites under CI.
- **Skill helper hardening (`init-review-run.py` / `validate-skill-package.py`)**: replaced deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)` while preserving the trailing-`Z` output format; wrapped the YAML frontmatter parser in a clean-error path so bad frontmatter no longer surfaces a Python traceback; rejected Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) in `--run-id`; harmonized the `mode`/`depth_tier` field name in the v8.2 protocol TRACK_DEPTH_PLAN block with the coverage-unit JSONL schema. New mirror adapters in `.claude/skills/codebase-review-swarm/` and `.agents/skills/codebase-review-swarm/` plus the corresponding `agents/openai.yaml` keep the three runner trees discoverable.

### Changed

- **Document upload limit raised to 100 MB**: the backend `max_file_size_mb`
  default, `.env.example`, `docker-compose.yml` env forwarding, frontend
  preflight, and schema parser guardrail now allow 100 MB per document upload.
  Reverse proxies must allow multipart upload overhead, for example with nginx
  `client_max_body_size 125m;`.
- **`OLLAMA_EMBEDDING_URL` and `RERANKER_URL` are now overridable in `docker-compose.yml`** via `${VAR:-<bundled default>}`. They were previously hardcoded, so host/`.env` overrides were silently ignored; the bundled defaults are unchanged.
- **Bare `host:8080` embedding URL now routes to native TEI `/embed`** (previously OpenAI `/v1/embeddings`). This affects only embedding URLs configured with **no path** on port 8080: such URLs previously appended `/v1/embeddings` (OpenAI mode) and now append `/embed` (native TEI mode). Default deployments are unaffected — the shipped `OLLAMA_EMBEDDING_URL` uses an explicit `/v1/embeddings` path, which still selects OpenAI mode. Operators running an OpenAI-compatible embedding server on a bare `:8080` URL should set an explicit `/v1/embeddings` path to keep OpenAI-mode behavior.
- bcrypt password verification (cost factor 14, ~400ms) now offloaded to dedicated ThreadPoolExecutor(4) via async_verify_password(), preventing event-loop blocking during login and password-change under concurrent load
- All authentication endpoints now use async bcrypt operations via async_verify_password() and async_hash_password()
- VectorStore write lock now has configurable asyncio.wait_for timeout (default 30s) via @asynccontextmanager _acquire_write_lock(); all 8 write paths updated
- VectorStore search concurrency increased from hardcoded 4 to configurable settings.vector_search_concurrency (default 16)
- LLM HTTP client pool limits now configurable: `LLM_MAX_CONNECTIONS` (default 100) and `LLM_MAX_KEEPALIVE_CONNECTIONS` (default 50)
- LanceDB optimize_mode default changed from "after_every_write" to "periodic" to reduce compaction blocking on every chunk write
- Pull request CI now checks frontend toolchain compatibility, root and subpath frontend builds, configuration contract drift, and high-risk PR test-scope drift.
- Browser login documentation now reflects JWT-only username/password auth with httpOnly refresh cookies; the legacy frontend `kv_api_key` fallback is no longer documented as supported.
- Settings write endpoints (`POST /api/settings` and `PUT /api/settings`) are documented as admin-protected operations, and `GET /api/settings/connection` is documented as an authenticated model connection check.
- Auth error handling changes are documented for API consumers: expired or invalid JWTs now return HTTP 401 with structured token error details.
- Configuration and installation docs now align with the current Harrier TEI embedding service, 9090 backend port, 3000 frontend dev port, and thinking/instant model defaults.
- Docker Compose defaults now match runtime retrieval defaults: `PARENT_RETRIEVAL_ENABLED=true` and `PER_DOC_CHUNK_CAP=5`.
- Docker Compose and `.env.example` CORS defaults now preserve both Vite dev (`http://localhost:5173`) and frontend dev (`http://localhost:3000`) origins.

### Fixed

- Prevented successful chat SSE streams from firing the completion callback twice.
- Declared `numpy` as a direct backend dependency and removed the unused `pybreaker` dependency.
- Removed or rewired hardcoded local Windows paths from development helper scripts and stale sample/test output artifacts.
- SQLite calls in `auth.py`, `deps.py`, and `vaults.py` wrapped with `asyncio.to_thread()` for async safety; fixed critical bugs where async functions were incorrectly passed to `asyncio.to_thread()`
- All database write paths now have internal rollback in named functions for improved error handling and consistency
- VectorStore write lock now uses timeout and Semaphore for better concurrency control
- KMS routes now return HTTP 503 when the KMS subsystem is disabled (`require_kms_enabled` dependency added to all KMS endpoints)
- KMS mutating routes (POST, PUT, DELETE) now protected by CSRF token validation
- KMS entry slug field now enforced with min_length=1 validation
- Document batch delete and vault delete operations now emit HMAC-signed audit log entries via `_safe_record_action()`
- KMSCompileProcessor background worker now only starts when `kms_enabled=true`; skipped entirely when KMS is disabled
- Email ingestion now accepts PowerPoint (.pptx) attachments via the `application/vnd.openxmlformats-officedocument.presentationml.presentation` MIME type

### Added

- New config settings: vector_search_concurrency (default 16), write_lock_timeout_seconds (default 30.0)
- _auth_executor.shutdown() in application teardown for clean ThreadPoolExecutor lifecycle

## [1.0.6] - 2026-05-02

### Fixed

- **Chat message overlap**: Replaced TanStack Virtual absolute-positioned rows with normal document flow in `TranscriptPane.tsx`. Dynamic content (streamed markdown, Shiki highlighting, SourceCards, MemoryCards) previously caused stale height measurements that positioned subsequent messages too high. Each message now renders as a block-flow `<div data-message-id="{id}">` with no absolute positioning. Auto-scroll uses `el.scrollTo({ top: el.scrollHeight })` with `isAtBottomRef` and `userScrolledUpRef` sentinels; token-growth scroll fires only while streaming and user is pinned to bottom; "New messages" button re-pins scroll to bottom.
- **Irrelevant memory display**: Weakly-related memories no longer appear in document-grounded answers. Backend: added `memory_dense_min_similarity` (0.30), `memory_rrf_min_score` (0.005), and `memory_context_top_k` (3) config fields; dense and RRF search paths filter candidates below threshold; `RAGEngine` caps retrieved memories at `context_top_k`. Frontend: `AssistantMessage.tsx` now shows only citation-matched memories; removed fallback to all `memoriesUsed`.
- **Vault ID validation**: `activeVaultId` stored in localStorage is now validated after `fetchVaults()` — if the stored ID no longer exists, the store auto-selects the first available vault or clears to null.
- **Document search double-fetch**: `DocumentsPage` was calling `fetchDocuments()` twice on mount. Fixed with `isFirstSearchRender` ref guard.
- **Upload state tracking**: Upload status now transitions through all states — `pending → uploading → indexing → indexed/error`. After upload, the store polls `getDocumentStatus()` every 3 seconds until indexing completes (3-minute timeout).
- **Memory edit UI**: Memory page now has inline edit (pencil) and delete (trash with confirm dialog) controls. `window.confirm` replaced with a proper Dialog.
- **Health hook state mutation**: `useHealthCheck.ts` was mutating the previous state object directly. Fixed to create a new state object on each check.

### Added

- **Reindex detection**: Settings page detects when saved changes affect embeddings (`embedding_model`, `vector_metric`, `chunk_size_chars`, `chunk_overlap_chars`, `embedding_doc_prefix`, `embedding_query_prefix`) and shows an amber warning banner prompting a re-index.
- **Profile org/vault cards**: ProfilePage now displays the user's organization memberships and accessible vaults with file counts.
- **`GET /api/documents` filtering**: Accepts optional `search` (filename substring) and `status` query parameters for server-side filtering.
- **`is_default` on VaultResponse**: Vault API responses now include `is_default: bool` to identify the default vault (id=1).
- **`search_type` on SearchResponse**: Semantic search responses include `search_type: "diagnostic"` for client-side differentiation.
- **Memory relevance config**: Three new environment variables — `MEMORY_DENSE_MIN_SIMILARITY`, `MEMORY_RRF_MIN_SCORE`, `MEMORY_CONTEXT_TOP_K` — for tuning memory retrieval quality.

## [1.0.5] - 2026-05-01

### Added

- **Normalized chat store**: `useChatStore` uses `messageIds: string[]` + `messagesById: Record<string, Message>` with `appendToMessage(id, chunk)` for O(1) streaming token append — non-streaming message rows no longer re-render on every token
- **`sendDirect(content, history, options)`**: new primitive in `useSendMessage` that accepts content and history directly, eliminating the stale-Zustand-state race in retry and edit-resubmit flows
- **Shiki syntax highlighting**: fenced code blocks render with `github-light` / `github-dark` themes; lazy-loaded as a separate Vite chunk; unknown languages fall back to unstyled code; copy button copies raw code
- **`Composer.tsx`**: extracted from `TranscriptPane.tsx` — auto-growing textarea, IME composition guard (`e.nativeEvent.isComposing`), stop button always clickable during streaming, slash command menu, vault indicator, character count
- **File attachments**: paste or drag-drop files into the composer; uploads to active vault via `uploadDocument()`; attachment tray shows filename, size, progress bar, and remove button; send blocked until all uploads complete
- **`SourceCards` component**: shown under every assistant answer — "Sources:" header with count, top 3 sources with expand/collapse, filename + snippet + relevance label, "View all N sources" link to RightPane, clicking a card selects it in RightPane
- **`MarkdownMessage.tsx`**: unified markdown renderer (GFM, Shiki, streaming caret, inline citation chips); replaces parallel rendering in `AssistantMessage` and `MessageContent`
- **`MessageActions.tsx`**: shared action bar — copy with clipboard feedback, retry, thumbs-up/down feedback, fork/branch, developer debug panel
- **`SourceCitation.tsx`**: inline citation chip handling `[S1]` and `[Source: filename]` formats
- **Session delete undo**: Sonner toast with Undo action on session deletion; session disappears optimistically and is restored on Undo
- Granular Zustand selectors: `useMessageIds`, `useMessage(id)`, `useChatIsStreaming`, `useChatStreamingId`, `useChatInput`, `useChatInputError`, `useChatActiveChatId`

### Changed

- **Chat layout**: removed full-width tinted background bands (`bg-muted/30`, `bg-primary/[0.12]`, `border-l-2`); content column is now `max-w-[760px]`; 24–32px vertical rhythm between message groups; hover action bar hidden until hover/focus/coarse-pointer
- **RightPane heading**: renamed from "Details" to "Evidence"
- **`AssistantMessage.tsx`** and **`MessageContent.tsx`**: now thin wrappers over shared components; one markdown parse per message
- **`TranscriptPane.tsx`**: duplicate `evidence:jump-to-answer` listener removed; Composer extracted; layout variables applied
- `@tailwindcss/typography` applied to assistant prose

### Fixed

- Duplicate `evidence:jump-to-answer` event listener caused two scroll + highlight cycles per citation click
- Stop generation incorrectly showed red error UI for user-initiated aborts
- Retry and edit-resubmit could send stale input state due to async Zustand batching

## [1.0.4] - 2026-05-01

### Security

- Prompt injection defense (CWE-1333): all untrusted content (document chunks, memory records, user queries, title-generation inputs, synthesis source passages) is now wrapped in named XML tags (`<document>`, `<memory>`, `<user_query>`, `<user_message>`, `<source_passages>`) before LLM injection; system prompts include an explicit SECURITY BOUNDARY directive
- Role allowlist: `ChatMessage` and `AddMessageRequest` Pydantic models now use `Literal["user", "assistant"]` for the `role` field; any other value (e.g. `"system"`, `"admin"`) is rejected at deserialization with `ValidationError`
- Magic byte validation on file upload: binary formats (`.pdf`, `.docx`, `.xlsx`, `.xls`) are validated against their magic byte signatures before being written to disk; mismatched or empty files return HTTP 400

### Added

- 15 automated tests in `backend/tests/test_prompt_injection.py` verifying role allowlist enforcement, XML boundary wrapping for chunks and memory, and SECURITY BOUNDARY directive presence in system prompts
- Password visibility toggle (Eye/EyeOff) on LoginPage, RegisterPage, and SetupPage
- Live password requirements checklist on RegisterPage (8+ chars, 1 digit, 1 uppercase) matching backend `password_strength_check()`
- Profile and Organizations entries added to MobileBottomNav "More" drawer
- Branded loading state on LoginPage: `text-primary` spinner with "Loading KnowledgeVault…" label

### Changed

- RegisterPage: authenticated users now redirect to `/` (was `/login`)
- RegisterPage and SetupPage: API errors on submit are surfaced in the UI via `role="alert"` paragraph; 409 conflict → "Username already registered"
- Chat export now produces `.md` files with `text/markdown` MIME type; messages formatted with `### User` / `### Assistant` headers and `---` separators

## [1.0.3] - 2026-04-30

### Added
- Message feedback (thumbs up/down) persisted to database via new `PATCH /chat/sessions/{session_id}/messages/{message_id}/feedback` route; feedback round-trips through GET session response and initializes from server state
- DB migration (`migrate_add_feedback_column`) adds `feedback TEXT` column to `chat_messages` with idempotency guard
- Session rail drag-to-resize: drag handle on right edge of sidebar adjusts width (200–400 px, stored in `useChatShellStore`); `sessionRailWidth` / `setSessionRailWidth` added to shell store
- Message timestamp shown on hover (desktop) / always (touch): `<time>` element with `formatRelativeTime` in `MessageBubble`
- Edit button (pencil icon) on user messages in `MessageBubble`; clicking populates the composer and trims forward messages
- "Try again →" button inside error boxes wired to `onRetry` callback
- TanStack Virtual scrolling in `SessionRail` with JSDOM fallback for tests; `MemoryPage` similarly virtualized
- `WaitingIndicator` rendered at `TranscriptPane` row level when streaming with empty content (replaces inline bounce dots)
- Evidence jump-to-answer: custom `evidence:jump-to-answer` window event scrolls TranscriptPane to the cited message and flashes a highlight ring
- `formatRelativeTime` extracted to `frontend/src/lib/formatters.ts` as a shared utility
- EmptyState components replace freeform empty-state markup in `RightPane` and `VaultsPage`
- Skeleton cards replace Loader2 spinner on `VaultsPage` loading state
- Accessible Labels (`<Label htmlFor>`) added to vault name/description inputs in `VaultsPage`

### Changed
- Copy button (`ActionBar`) strips `[Source:…]` and `[S\d+]` citation markers before writing to clipboard; `CopyButton` (code blocks) gains execCommand fallback and `toast.error` on failure
- Session rail toggle button no longer hidden on desktop (`md:hidden` removed); sidebar can be opened/closed on all screen sizes
- Active session in rail: `bg-accent/50` replaced with `bg-primary/10 border-l-2 border-primary` left-accent style
- `MessageBubble` accepts `userInitial` as prop (computed once in `TranscriptPane`) instead of subscribing to `useAuthStore` per row
- `renderedContent` in `AssistantMessage` wrapped in `useMemo` to avoid re-parsing on every streaming token
- Auto-scroll dependency narrowed to `messages.length` (not full array reference) to reduce spurious re-renders
- Fork button and edit button always visible on touch devices (`[@media(pointer:coarse)]:opacity-100`)
- Debug panel and debug button gated behind `import.meta.env.DEV`
- `DocumentsPage` table rows get correct ARIA roles (`role="row"` / `role="rowgroup"`)
- Amber warning banner in `DocumentsPage` gains `dark:text-amber-300` for dark-mode contrast
- `SessionRail` error state uses `AlertCircle` icon with a Retry button
- `RightPane` source text truncation delegated to CSS `line-clamp-2` / `truncate` (removes JS `.slice()`)
- `useSendMessage` maps DB message IDs back via `migrateId`, migrating localStorage feedback keys and updating message state

### Fixed
- Feedback API call correctly validates vault-level authorization via `evaluate_policy` before updating (prevents cross-user writes)
- `ChatHistory.tsx` deleted (was dead code with no importers)
- `SessionRail` no longer clears the fetched-IDs cache on empty search query (prevents flash re-fetch)

## [1.0.2] - 2026-04-30

### Added
- File-type icon utility (`frontend/src/lib/fileIcon.tsx`) with color-coded Lucide icons for PDF (red), DOCX (blue), Markdown (teal), spreadsheets (green), and generic fallback
- File-type icons integrated into SourcesPanel, DocumentCard, and DocumentsPage table rows
- Citation chip source-snippet tooltips — hovering a `[Source: …]` chip now shows up to 100 chars of the matched passage
- Code block rendering in assistant messages: language badge, copy-to-clipboard button, proper `pre`/`code` wrapping via ReactMarkdown component overrides
- Suggested-prompt chips enriched with contextual Lucide icons (TrendingUp, AlignLeft, Database, CheckCircle2)

### Changed
- ActionBar default opacity raised from 30% to 60% for better discoverability; always 100% on touch/coarse-pointer devices (`[@media(pointer:coarse)]`)
- Mobile bottom-nav labels bumped from 10 px to `text-xs` (12 px) to meet WCAG minimum font-size
- User message rows: left-border accent (`border-l-2 border-primary/40`) + slightly stronger background (`bg-primary/[0.12]`) replacing flat `bg-primary/10`
- User avatar now renders the authenticated user's initials (from `useAuthStore`) instead of a static `<User>` icon
- Composer textarea border highlights on `focus-within` for clearer focus state
- Send and Stop buttons bumped from `size="sm"` to `size="default"` for larger tap targets
- SourcesPanel scroll area set to `h-full` so it fills its container rather than a fixed 400 px height
- Removed duplicate "View all sources" text link that competed with the existing "+N more" chip button
- NavigationRail active-indicator motion pill removed (caused layout shift); logo wrapper simplified

### Fixed
- `fileIcon.tsx` handles `null`/`undefined` filenames without crashing (null-coalescing before `.split`)
- AssistantMessage tests updated to match the count-prefixed aria-label `"View all N sources"` and the new `opacity-60` class
