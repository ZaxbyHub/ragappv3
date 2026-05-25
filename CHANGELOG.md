# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- Configurable rate limiting for API endpoints: `chat_rate_limit` (default 30/min), `search_rate_limit` (default 60/min), `vault_create_rate_limit` (default 10/min), and `memory_mutation_rate_limit` (default 30/min). Set to 0 for unlimited.
- async_hash_password() wrapper using ThreadPoolExecutor for non-blocking password hashing in auth endpoints
- Memory dense search optimized with FTS pre-filtering and SQL LIMIT for improved performance

### Changed

<<<<<<< HEAD
- bcrypt password verification (cost factor 14, ~400ms) now offloaded to dedicated ThreadPoolExecutor(4) via async_verify_password(), preventing event-loop blocking during login and password-change under concurrent load
- All authentication endpoints now use async bcrypt operations via async_verify_password() and async_hash_password()
- VectorStore write lock now has configurable asyncio.wait_for timeout (default 30s) via @asynccontextmanager _acquire_write_lock(); all 8 write paths updated
- VectorStore search concurrency increased from hardcoded 4 to configurable settings.vector_search_concurrency (default 16)
- LLM HTTP client pool limits now configurable: `LLM_MAX_CONNECTIONS` (default 100) and `LLM_MAX_KEEPALIVE_CONNECTIONS` (default 50)
- LanceDB optimize_mode default changed from "after_every_write" to "periodic" to reduce compaction blocking on every chunk write
=======
- Pull request CI now checks frontend toolchain compatibility, root and subpath frontend builds, configuration contract drift, and high-risk PR test-scope drift.
>>>>>>> origin/master
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
