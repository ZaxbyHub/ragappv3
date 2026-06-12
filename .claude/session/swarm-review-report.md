# RAGAPPv3 Swarm Codebase Review — Final Report

Date: 2026-06-12
Method: 5 parallel explorer agents (RAG pipeline, auth/user management, chat interface,
general UI/UX, cross-cutting architecture) → 5 independent skeptical reviewer agents
validating every candidate against the actual code → critic agent challenging the 14
highest-impact confirmed findings. Only validated results appear below. Explorers
produced ~120 candidate findings; roughly 60% were disproved or materially downgraded
on evidence — verdicts below reflect the surviving set.

## Verdict

This is a genuinely mature, well-engineered RAG system — hybrid dense+BM25 retrieval
with RRF, multi-scale chunk indexing (768/1536), cross-encoder reranking with surfaced
fallback status, HyDE + step-back + follow-up query rewriting, CRAG-style retrieval
evaluation, parent-window context expansion, lost-in-the-middle-aware prompt packing,
crash-safe insert-then-delete reupload ordering, vault-scoped multi-tenancy verified
in both retrieval arms, clean XSS posture, and a chat UI that already has edit, retry,
regenerate, copy, fork, pin, rename, and session search. Most of the things that would
disqualify a "state of the art" claim are NOT missing.

What stands between it and state-of-the-art is: one HIGH-severity chat lifecycle bug,
a cluster of MEDIUM data-integrity/consistency gaps (citations, deletes, embedding
versioning, partial indexing), and a set of absent capabilities (real eval harness,
semantic chunking that exists but is unwired, MFA/SSO/audit logging, observability).

## CONFIRMED FINDINGS

### HIGH

**H1. Switching/forking sessions mid-stream silently loses the assistant reply and
corrupts UI state.**
`SessionRail.tsx:749-766` (new chat, session click), the `ChatShell.tsx:146-179` route
effect, and `TranscriptPane.tsx:399-435` (`handleFork`, no `isStreaming` guard unlike
`handleEdit`) all switch sessions without aborting the active stream. `loadChat`
(`useChatStore.ts:208`) resets `streamingMessageId` but not `isStreaming`/`abortFn`, so:
(a) the new session's composer is locked in "Generating…" (`Composer.tsx:594,600,775`)
until the orphan stream ends; (b) on completion, `useSendMessage.ts:194-211` saves the
user message but the assistant message id was wiped from `messagesById`, so the reply is
permanently dropped — the old session ends with a dangling user message; (c) pressing
Stop in the new session marks the wrong message `stopped: true` via the
`useChatStore.ts:191` fallback. Only `useChatHistory.ts:57-58` has a guard.
Fix: call `stopStreaming()` (or guard) in `handleNewChat`, `handleSessionClick`, the
route effect, and `handleFork`.

### MEDIUM

**M1. Hallucinated citations are rendered and persisted in streamed chats.**
The stream path computes `citation_validation` but never uses `repaired_content`
(`chat.py:302-331`); the frontend's `onCitationValidation` has zero consumers
(`api.ts:738,1322-1323`; the type is literally `CitationValidationDebug`), and raw
streamed content is what gets persisted. Invalid refs like `[S99]` render as citation
chips even with no matching source (`MarkdownMessage.tsx:201-209`) — in the product's
core trust UI — while the (unused-by-frontend) non-stream path strips them.

**M2. Documents page silently shows only the first 50 documents.**
Backend paginates (`documents.py:517-518`, default `per_page=50`); the frontend never
passes page params (`useDocumentPolling.ts:54-61`), ignores the returned `total`
(`api.ts:572`), and has no pagination/load-more UI. Stats cards show the true count
while the table caps at 50. Mitigation keeping this out of HIGH: search and sort are
server-side, so any document is still reachable via search.

**M3. Deleted-document content can remain retrievable forever.**
`documents.py:1493-1494` swallows `delete_by_file` failures with a log warning, then
deletes the SQLite row and commits anyway (`:1523-1526`). No chunk-level orphan sweep or
reconciliation exists anywhere (only wiki/KMS `settings_reindex`). A failed LanceDB
delete leaves "deleted" content surfacing in chat answers with a dangling `file_id` —
a privacy-expectation breach with no recovery path.

**M4. Embedding model changes are undetectable and silently corrupt retrieval.**
Model identity is never persisted with the table — `vector_store.py:2141-2143` logs the
metadata it "should" store; startup dim-mismatch validation logs-and-continues
(`lifespan.py:399-407`); there is no document reindex-all operation. Swapping between
same-dimension embedding models silently mixes embedding spaces with zero signal.

**M5. Documents are marked "indexed" with up to 50% of chunks silently missing.**
`document_processor.py:1690-1722` drops chunks whose embeddings failed and proceeds to
`indexed` if failure ratio ≤50% — only a log warning, no partial status anywhere in the
schema or UI. Users see success while retrieval quality is silently degraded.

**M6. Backend dependencies are entirely unpinned; builds are non-reproducible.**
Every line of `requirements.txt`/`requirements-ci.txt` is `>=` (sole ceiling
`bcrypt<5.0.0` — itself evidence of a past breakage); no lock file; `Dockerfile:61-62`
installs unpinned. A breaking `lancedb`/`pydantic`/`unstructured` release breaks fresh
production builds. Fix: pip-compile/uv lock.

**M7. Confirmed deletes silently never execute if the user navigates away.**
The real `deleteDocument` call sits in a 3s undo-window `setTimeout`
(`DocumentsPage.tsx:466-489`), and unmount cleanup cancels pending timers (`:119-124`).
Confirm a destructive dialog, see "Document deleted", navigate within 3s → the document
is still there. Fix: flush (execute) pending deletes on unmount instead of cancelling.

**M8. Per-token full markdown + citation re-parse during streaming.**
`MarkdownMessage.tsx:413-416` / `AssistantMessage.tsx:55-65` re-run regex citation
parsing plus the full ReactMarkdown pipeline on every SSE chunk with no coalescing
(O(n²) in message length). Per-message Zustand subscriptions cap the blast radius to
the streaming row, but long answers still get visibly janky. Fix: rAF-batch
`appendToMessage`.

**M9. Org-membership admin routes are inconsistent with the rest of the authz surface.**
`PUT /users/{id}/organizations` (`users.py:594`) lets a non-superadmin admin add a user
to ANY org with no caller-org check (the sibling groups route has one at `:695-708`),
and both `update_user_organizations` and `update_user_groups` (`users.py:733`) are the
only mutating routes in the file without `csrf_protect`. Verified not directly
exploitable as CSRF (auth is Authorization-header; the backend never sets an
`access_token` cookie, so the `deps.py:155` cookie fallback is dead code) — this is an
authz-consistency and defense-in-depth gap, not an IDOR. The app's global-admin model
makes cross-org *reads* by-design.

### LOW (confirmed, briefly)

- Citation-repair whitespace regexes (`citation_validator.py:138,141`) collapse code
  indentation in the **non-stream** path (`chat.py:419`) — frontend never calls it;
  affects direct API consumers only.
- Crash-retry appends duplicate same-id chunk rows (deterministic ids + pure append +
  generation delete keeps matching prefixes) — masked at query time by UID dedup
  (`document_retrieval.py:411-417`), so residual harm is index bloat and wasted top-k
  slots; self-heals on content-changing reupload.
- Failed chat exchanges persist nothing (`useSendMessage.ts:164-187`) — reload before
  retrying drops the user message; Retry affordance exists, so defensible.
- Clean SSE EOF without a `done` event leaves the UI stuck streaming (defensive gap;
  backend always emits terminal events).
- Auth hardening backlog: login timing oracle (no dummy bcrypt verify,
  `auth.py:299-300`; rate-limited 10/min); sliding 30-day refresh with no idle timeout
  (`last_used_at` written, never read); admin password reset doesn't clear
  `failed_attempts`/`locked_until` (user stays locked ≤15 min, `users.py:386`); CSRF
  tokens multi-use with sliding TTL; sessions not IP/UA-bound; `secure` cookie flag
  trusts `X-Forwarded-Proto`.
- No auth/user-admin audit logging (audit exists only for documents + toggles).
- Raw exception text in 500 details (`documents.py:301`, `search.py:196-202`,
  `auth.py:185,559,640`); no global error-normalizing handler.
- Request-id correlation is dead code: `RequestIdFilter` never registered, formatter
  lacks the token, access log renders no method/path/status; no metrics/tracing.
- Bulk delete UI removes failed ids too (toast shown; heals on next fetch).
- Upload-store polling continues ~3 min after logout (4h cap + failure cap exist).
- `backup_sqlite.py:24` hot-copies via `read_bytes()` instead of the sqlite3 backup
  API (documented cold-backup path is safe).
- Pool `_created_count` double-decrement on connection-create failure
  (`database.py:2569-2573` + `2645-2647`) can let the pool exceed `max_size`.
- A11y nits: static "Expand sidebar" label (`NavigationRail.tsx:223`); `NumberInput`
  error never rendered/linked via `aria-describedby`; no `aria-sort` on table headers;
  failed document fetch renders misleading "No documents yet" with no retry; mobile
  bottom nav lacks a KMS entry; select-all can include a doc pending undo-delete.
- Housekeeping: `redesign/` and `specs/` are unwired planning artifacts;
  `EmbeddingSemanticChunker` and `settings.semantic_chunking_strategy` are dead code
  (see E1); CI runs 8 of 214 backend test files — documented and deliberate
  (heavy-dep exclusion), but a nightly full-suite job would close the gap.

## NOTABLE DISPROVED CLAIMS (so they don't resurface)

Explorer claims rejected on evidence: spreadsheet parsing blocking the event loop
(it's in `to_thread`); upload size unenforced (pre-check + mid-stream 413); no backups
(encrypted backup script + documented procedure exist); CSRF Redis fragility (SQLite/
in-memory fallbacks + frontend auto-retry); refresh-token-as-access-token (opaque
tokens, not JWTs); accessToken persisted to localStorage (explicitly excluded); SQL
injection in user update (hardcoded field literals); missing last-superadmin guard
(atomic SQL guard exists); AdminGroupsPage missing guard (present); Radix dialogs
lacking focus management (Radix provides it); settings tabs losing unsaved changes
(global store, by design); chat missing stop/edit/copy/retry/search/rename/pin (all
exist); no history windowing (20-message window exists); index-rebuild thrash (churn
threshold prevents it); axios outdated (lockfile resolves 1.13.4); config contract
unenforced (fail-loud `reject_insecure_defaults`).

## WHAT'S MISSING FOR STATE-OF-THE-ART (enhancements)

**RAG quality**
- E1: Wire up the existing `EmbeddingSemanticChunker` (`chunking.py:315`) — complete and
  unit-tested, read by zero call sites; the live path is title-based fixed-size only.
- E2: Real evaluation harness. `eval.py` is n-gram-overlap heuristics on single queries;
  add golden-dataset regression evals (faithfulness/relevance/recall) runnable in CI.
- E3: Embedding versioning + assisted reindex (fixes M4): persist model id + dim per
  table, detect mismatch at startup, offer reindex-all with progress.
- E4: Token-based (not count-based) history windowing and tokenizer-based context
  packing; detect `finish_reason` truncation mid-stream.
- E5: Query decomposition for multi-part questions (step-back/HyDE exist; decomposition
  doesn't); user-facing metadata filtering (date/tag/author) — `filter_expr` exists
  internally and is never exposed.
- E6: Semantic near-duplicate detection at ingestion (only exact file-hash dedup today).
- E7: Background reconciliation sweep: LanceDB `file_id`s vs `files` table (fixes M3,
  cleans crash leftovers); partial-index status surfaced to UI (fixes M5).
- E8: numpy-vectorize memory cosine scan; live streaming citation surfacing in the
  evidence panel.

**Auth/enterprise readiness**
- E9: MFA (TOTP/WebAuthn), SSO/OIDC, self-service password reset, email verification —
  all confirmed absent; table stakes for multi-user enterprise deployments.
- E10: Auth/admin audit log (logins, resets, role changes, session revocations) reusing
  the existing HMAC'd document-audit pattern.

**Operations/observability**
- E11: Register `RequestIdFilter`, add request_id to the formatter, emit real access
  logs; add a `/metrics` endpoint (Prometheus) and optional OTel tracing.
- E12: Dependency lock file (M6); nightly full-test CI job; standard error envelope
  (`error_code`/`category`/`trace_id`).
- E13: Hot-backup via sqlite3 backup API + scheduled snapshots including LanceDB.

**Chat/UX polish**
- E14: Abort-on-switch (H1), rAF token batching (M8), pagination UI (M2), flush pending
  deletes on unmount (M7).
- E15: Escape-to-stop and shortcut palette; route-level skeletons instead of the global
  spinner; retry affordances on failed list fetches.

## Priority order (impact × effort)

1. H1 abort-on-session-switch (small fix, core-flow data loss)
2. M1 wire citation validation into the streamed path (trust UI)
3. M2 documents pagination UI (silent truncation)
4. M7 flush pending deletes on unmount (destructive-action integrity)
5. M3+E7 delete reconciliation sweep
6. M4+E3 embedding model versioning/reindex
7. M6+E12 dependency lock file
8. M5 partial-index status
9. M8 rAF streaming batching
10. E1 wire semantic chunker; E2 eval harness (the two biggest "state of the art" gaps)
