# Swarm Review State (working notes — not part of final report)

## Status
- Explorers: all 5 complete (RAG, auth, chat, UI/UX, cross-cutting)
- Reviewers: auth COMPLETE; RAG, chat, UI/UX, architecture IN PROGRESS
- Critic: pending

## AUTH — validated verdicts (reviewer + orchestrator verification)
- IDOR claim on /users/{id}/organizations: DOWNGRADED. Global-admin-sees-all is the design; real residual = update_user_organizations (users.py:594) lets non-superadmin admin add user to any org w/o caller-org gate → MEDIUM authz inconsistency (get_user_groups has the gate at 695-708; orgs routes don't).
- f-string UPDATE field list: DISPROVED (hardcoded literals, parameterized values).
- No audit logging for ANY auth/user-admin events: CONFIRMED MEDIUM (audit exists only for documents + toggles).
- delete_user last-superadmin claim: DISPROVED (atomic guard at users.py:547-554).
- Username enumeration via timing (no dummy bcrypt verify for nonexistent users) + 423 lockout oracle: CONFIRMED MEDIUM (auth.py:299-300 vs 329-337).
- Rate limits (login 10/min IP, register 5/hr): CONFIRMED LOW/MEDIUM (account lockout bounds brute force).
- Refresh tokens: no idle timeout; rotation extends to +30d forever; last_used_at never read: CONFIRMED MEDIUM (rotation + reuse-detection mitigate).
- CSRF tokens multi-use + sliding TTL: CONFIRMED LOW.
- SQLite CSRF store race: DISPROVED (cleanup only deletes expired rows).
- transfer_ownership to non-admin member: BY-DESIGN/LOW (owner-authorized).
- Admin password reset doesn't clear failed_attempts/locked_until → user stays locked 15min: CONFIRMED MEDIUM (users.py:386 vs auth.py:317-327).
- secure cookie flag trusts X-Forwarded-Proto: CONFIRMED LOW/MEDIUM (proxy-config dependent).
- Sessions store IP/UA but never validate: CONFIRMED LOW/MEDIUM (standard tradeoff).
- No MFA, no auth audit log, no self-service password reset, no email verification: CONFIRMED ABSENT — feature gaps MEDIUM.
- Refresh-token-as-access-token: DISPROVED (refresh tokens are opaque random strings, not JWTs).
- Zustand persisting accessToken: DISPROVED (partialize excludes it, useAuthStore.ts:337-341).
- NEW (reviewer): update_user_organizations (users.py:594) and update_user_groups (users.py:733) omit csrf_protect while all sibling mutations have it. Orchestrator verified: no global CSRF middleware; BUT backend never sets an access_token cookie (only refresh cookie path-scoped + csrf cookie), Authorization header is the real auth carrier, deps.py access_token Cookie fallback is dead code → NOT exploitable today. Verdict: MEDIUM-LOW consistency/defense-in-depth gap. (deps.py:154-172, auth.py set_cookie sites: 257,379,464,702,866 — all refresh/csrf only.)
- Spot-check of document/vault authz: PASSED (evaluate() checks present in documents.py 780,878,1052,1570,1685; deps.py get_effective_vault_permissions sound).

## CHAT — validated verdicts
- Session auto-name TOCTOU: DISPROVED (role guard chat.py:1327 + atomic conditional UPDATE 1232-1240).
- onComplete-after-onError: DISPROVED (api.ts:1254-1256 returns after error; completeOnce dedupe 1222-1226).
- Feedback migration race: DISPROVED as claimed; residual LOW (temp numeric id click in completion window → 404 + toast).
- CONFIRMED HIGH: session switch/newChat/fork mid-stream never aborts (SessionRail.tsx:749-766, ChatShell.tsx:146-179, TranscriptPane handleFork 399-435). No content bleed (appendToMessage no-ops), but composer stuck "Generating", assistant msg silently dropped from persistence (old session = dangling user msg), Stop in new session marks wrong msg stopped (useChatStore.ts:191-199). Only useChatHistory.ts:57-58 guards.
- Fork mid-stream reachable: CONFIRMED MEDIUM (same family).
- Per-token full markdown+citation re-parse, no batching/rAF: CONFIRMED MEDIUM (MarkdownMessage.tsx:413-416, AssistantMessage.tsx:55-65; per-message subscription caps blast radius).
- Streamed citation validation NEVER applied: CONFIRMED MEDIUM — stream path computes but doesn't use repaired_content (chat.py:302-331); frontend onCitationValidation has zero consumers; raw content persisted → invalid [S99] permanent in streamed sessions.
- get_session: 3 constant extra queries, LOW. Auto-name fallback: trivial. History: 20-msg window EXISTS (prompt_builder.py:192-195) — count-based not token-based, full history shipped over wire (LOW).
- Transcript unvirtualized: CONFIRMED LOW (deliberate).
- UX gaps MOSTLY DISPROVED: stop button w/ feedback EXISTS, mid-conversation edit EXISTS, per-message copy EXISTS, retry-on-error EXISTS, session search/rename/pin EXIST. Absent: Escape-to-stop/Cmd+Enter (LOW).
- NEW N1 MEDIUM: failed exchanges never persisted — onError saves nothing; reload after stream error drops user message.
- NEW N2 LOW: clean EOF without done event leaves UI stuck streaming (infra truncation only).
- XSS: checked clean (rehypeSanitize defaultSchema, no rehype-raw, escaped shiki fallback).

## UI/UX — validated verdicts
- Upload store while(true): DISPROVED as critical — has exits (removal/stop/cancel/4h cap/30-failure cap); store-level by design. Residual LOW: polls ~3min after logout; dead code :362-364.
- useSendMessage abort cleanup: DISPROVED (onError calls setAbortFn(null) :170,:184).
- useHealthCheck race: DISPROVED.
- CONFIRMED HIGH (corrected): document list silently capped at 50 — backend paginates (documents.py:517-518 per_page=50), frontend never passes page params (useDocumentPolling.ts:54-61), no pagination UI; stats show true total.
- Bulk delete desync: CONFIRMED MEDIUM (removes failed_ids from UI too, no refetch; DocumentsPage.tsx:318-335).
- Single delete rollback: DISPROVED (catch restores + refetches :478-485).
- NEW MEDIUM: undo-window delete dropped on unmount — pending 3s delete timers cleared on unmount (DocumentsPage.tsx:119-124) → "Document deleted" shown but delete never executes.
- A11y claims mostly DISPROVED (Radix focus trap; skip link exists PageShell.tsx:40-45; AdminGuard present in AdminGroupsPage:276-281). Real LOW nits: static "Expand sidebar" aria-label (NavigationRail.tsx:223); NumberInput error never rendered/linked; no aria-sort on th; no-retry-affordance on failed fetch renders misleading "No documents yet"; MobileBottomNav lacks KMS entry; select-all uses unmasked list (pending-undo doc can join bulk ops).
- Vault store on logout: CONFIRMED LOW (auto-validates on next fetch).
- Settings tabs: DISPROVED (global store, per-tab dirty dots, by-design discard).
- Sizes confirmed: AdminUsersPage 1219 lines, SessionRail 38KB, DocumentTable 15 props (LOW code quality).
- redesign/ unwired: CONFIRMED INFO.

## RAG — validated verdicts
- Spreadsheet blocking: DISPROVED (asyncio.to_thread document_processor.py:1324-1326).
- Char/3.5 token estimate: CONFIRMED LOW (overestimates by design; token_pack_truncated surfaced in trace; no tokenizer anywhere).
- Reupload dup window: CONFIRMED BY-DESIGN LOW (documented "duplicates not zero rows"; cross-generation dupes can surface in RRF since different ids).
- Cell truncation no marker: CONFIRMED LOW.
- Partial ingestion: sweep exists (30min); no orphan cleanup ANYWHERE.
- Reranker degradation: PARTIALLY DISPROVED — score_type + rerank_status="fallback" surfaced (rag_engine.py:1234-1258, trace 753).
- Wiki N+1: CONFIRMED LOW (≤20 indexed single-row lookups).
- Memory linear scan: CONFIRMED BY-DESIGN LOW (1000-cap, in to_thread; numpy would help).
- Index rebuild thrash: DISPROVED (churn>=0.2 threshold, generation sync prevents forced rebuild).
- Embedding all-or-nothing: DISPROVED — inverse REAL issue: doc marked indexed with up to 50% chunks silently dropped (document_processor.py:1690-1722) MEDIUM-ish.
- Upload size: DISPROVED (pre-check + streaming enforcement 413, documents.py:1181-1252). Zip-bomb decompression LOW.
- Semantic chunking: live path title-based only; EmbeddingSemanticChunker EXISTS UNwired (chunking.py:315; settings.semantic_chunking_strategy read by zero call sites) — unshipped feature/dead code.
- Eval: heuristic n-gram endpoint only (eval.py:62-80), no real harness.
- Embedding model versioning: CONFIRMED MEDIUM — model id never persisted (vector_store.py:2141-2143 logs instead); dim-mismatch at startup logs-and-continues (lifespan.py:399-407); same-dim swap silently mixes embedding spaces.
- Metadata filtering not user-exposed: LOW. finish_reason/truncation detection absent: LOW.
- Vault isolation in retrieval: PASSED (filters in both arms both paths; per-chunk recheck in search detail).
- NEW-1 MEDIUM: delete_by_file failure swallowed (documents.py:1493-1494) then SQLite row deleted anyway → deleted doc content stays retrievable; no reconciliation.
- NEW-2 MEDIUM: crash-retry re-appends identical chunk ids (deterministic ids, pure append, delete-old-generation keeps matching-prefix rows) → permanent duplicates.
- NEW-3 MEDIUM: citation repair whitespace regexes (citation_validator.py:138,141) run unconditionally; non-stream path substitutes repaired content (chat.py:419) → collapses code indentation.
- Multi-scale defaults 768/1536 (not 512).

## ARCHITECTURE — validated verdicts
- SQLite single-node: BY-DESIGN INFO/LOW (self-hosted stated model).
- Backup: DISPROVED — scripts/backup_sqlite.py (AES-GCM) + admin-guide cold backup exist. LOW nit: script hot-copies via read_bytes() not sqlite3 backup API.
- Parsing executor exhaustion: DISPROVED (bounded by worker count default 2); GIL contention LOW.
- Pool lock: DISPROVED (lock held only for counter ops).
- Write semaphore: DISPROVED/BY-DESIGN (guards short SQLite txns; LanceDB serialization documented correctness guard).
- Chat stream timeout: DISPROVED (llm_client httpx timeouts 300s/120s).
- Raw exception text in HTTPException + no global 500 normalizer: CONFIRMED LOW-MEDIUM (documents.py:301, search.py:196-202, auth.py:185,559,640; only RequestValidationError handler in main.py:137).
- CSRF Redis loss: DISPROVED (SQLite/in-memory fallback stores + frontend auto-retry api.ts:95-112).
- write_lock_timeout: PARTIAL LOW (acquisition wait, clean failure + retry path).
- Deps >= unpinned, no lock file, Docker/CI install unpinned: CONFIRMED MEDIUM.
- axios: DISPROVED (lockfile resolves 1.13.4).
- CI 8/214 test files: CONFIRMED MEDIUM (documented deliberate, but conftest stubs mean more could run).
- Config contract: DISPROVED (script exists; config.py:848-869 reject_insecure_defaults fails loud).
- request_id correlation: CONFIRMED LOW-MEDIUM — RequestIdFilter dead code (never registered), formatter lacks token, no metrics/otel.
- to_thread starvation: LOW/speculative.
- Deprecated fields: DISPROVED (warnings emitted config.py:602-649).
- specs//redesign/: LOW housekeeping.
- NEW: pool _created_count double-decrement underflow on create-failure path (database.py:2569-2573 + 2645-2647) LOW.

## Pending: critic verdicts on F1-F14 batch, then final report synthesis.
