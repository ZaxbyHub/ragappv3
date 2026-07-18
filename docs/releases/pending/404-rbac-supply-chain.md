# 404 — Security: global-memory RBAC + chat write-authz + supply-chain pinning

## Summary
Closes the global-memory (`vault_id IS NULL`) cross-tenant visibility gap and
hardens supply-chain pinning.

## What changed

### Security — global memories are now admin-only (closes #392 / MEDIUM-11)
A "global memory" is a `memories` row with `vault_id IS NULL`. Previously any
authenticated user could read, create, update, delete, and promote global
memories, and global memories leaked into non-admin chat prompts via the RAG
retrieval path. Global memories are now admin/superadmin-only on every path:

- **Read**: `GET /memories?vault_id=N` and `/memories/search` return global
  rows only to admins; `MemoryStore.search_memories` gained an
  `include_global` flag (default `False`, fail-closed) threaded from the
  caller's role through `RAGEngine.query()`.
- **Write/mutate**: `POST/PUT/DELETE /memories` with a global target requires
  admin (`_require_admin_for_global` helper).
- **Chat**: the RAG retrieval path (`rag_engine._memory_retrieve`) receives
  `include_global` from the chat route's role check.
- **Promote**: `WikiCompiler.promote_memory` rejects global-memory promotion
  for non-admins (`is_admin` param, default `False` fail-closed).

### Security — chat "remember" write authorization (R12, in-scope per decision)
A member with **read-only** vault access could previously write a vault memory
by typing "remember ..." in chat. `RAGEngine.query()` now takes a
`can_write_memory` flag (default `False`, fail-closed); the chat route resolves
write permission and threads it; when blocked the user gets a feedback chunk
instead of a silent drop.

### Supply chain (closes #391)
- Docker images pinned by `@sha256:` digest: `docker-compose.yml` (3 images),
  root `Dockerfile`, `frontend/Dockerfile`, `backend/embedding_server/Dockerfile`
  (5 `FROM` lines).
- Requirements upper-bounded below the next major (`backend/requirements.txt`,
  `-dev.txt`, `-ci.txt`). Lockfiles unchanged (Linux-built originals are still
  authoritative; CI `pip-compile --dry-run` confirms consistency).
- CI Actions SHA-pinning and dependabot config were already in place.

## Migration
No schema migration required. No env-var changes.

### Behavioral changes operators should know
- **Non-admin users can no longer see or manage global memories.** If your
  deployment relied on global memories being a shared cross-vault knowledge
  base visible to all members, that is no longer the default. Admins retain
  full access. To surface specific knowledge to members, store it in their
  vault(s) instead.
- **Read-only vault members can no longer create memories via chat.** Grant
  write permission to the vault if a member should be able to use "remember".
- **Docker images are now digest-pinned.** Dependabot will open PRs when new
  digests publish; images no longer auto-update on `cuda-latest`/`7-alpine`.

## Already-satisfied (no work this PR, documented for completeness)
- `_sanitize_filename` control-char stripping (criterion 1) — done in #417.
- `wiki_curator_url` SSRF guard (criterion 2) — done via `curator_ssrf.py`.
- ci.yml Actions SHA pins (criterion 6) and dependabot (criterion 7).

## Known caveats
- The bandit SAST baseline (`backend/security/bandit-baseline.json`) was
  regenerated for line-number shift churn only (21 entries moved; 0 genuinely
  new findings). See PR body for the content-level proof.
