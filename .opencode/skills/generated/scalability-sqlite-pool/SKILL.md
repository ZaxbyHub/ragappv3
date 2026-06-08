---
name: scalability-sqlite-pool
description: >
  Connection pool topology, double-connection pattern detection, semaphore/lock
  inventory, and exhaustion headroom calculation for RAGAPPv3. Load before
  reviewing scalability, connection pool sizing, or concurrent user throughput.
---

# Scalability & Connection Pool Patterns (RAGAPPv3)

## Pool Inventory

The application has **4 independent SQLite connection pools**:

| Pool | Size | Created by | Used by |
|------|------|-----------|---------|
| Main | 10 (set at `backend/app/lifespan.py:315`; default `max_size=5` at `backend/app/models/database.py:2532` is overridden) | `lifespan.py:315` | Auth, routes, vault operations, BackgroundProcessor, RAG engine |
| MemoryStore | 2 (`backend/app/services/memory_store.py:144`) | `memory_store.py` init | Memory search (FTS + dense) |
| DocumentProcessor fallback | 2 (`backend/app/services/document_processor.py:556`) | `document_processor.py` | Document ingestion when no pool is injected |
| FileWatcher fallback | 2 (`backend/app/services/file_watcher.py:180`) | `file_watcher.py` | File watcher scan path |

**BackgroundProcessor** does NOT have its own pool — it shares the main pool (max_size=10) via `app.state.db_pool`.

## Double-Connection Pattern Detection

The most common scalability trap is a dependency that opens its **own** pool connection instead of using the DI-injected one. This doubles per-request connection consumption.

### Pattern: `evaluate_policy` vs `_evaluate_policy`

There are two variants of the permission check function:

- **DI variant** (`_evaluate_policy` at `backend/app/api/deps.py:393`): `_evaluate_policy(db, principal, resource_type, resource_id, action)` — accepts an injected DB connection. Use this in FastAPI dependency chains.
- **Standalone variant** (`evaluate_policy` at `backend/app/api/deps.py:448`): `evaluate_policy(principal, resource_type, resource_id, action)` — calls `get_pool()` and `pool.get_connection()` directly, opening a second connection.

**`require_vault_permission` at `backend/app/api/deps.py:476` uses the standalone variant**, which means every vault-scoped request using this dependency consumes 2 pool connections instead of 1.

### Detection heuristics

When reviewing scalability in this codebase:
1. Search for `.get_connection()` calls inside dependency functions — these bypass DI
2. Check if `require_vault_permission` is used alongside `Depends(get_db)` — this signals double-connection
3. Prefer `get_evaluate_policy` (the DI generator at `backend/app/api/deps.py:426`) over the standalone `evaluate_policy`

## Auth Caching Gap

`get_current_active_user` at `backend/app/api/deps.py:240` executes a fresh DB query on **every** authenticated request:
```python
await asyncio.to_thread(lambda: db.execute(
    "SELECT id, username, full_name, role, is_active, must_change_password FROM users WHERE id = ?",
    (user_id,)
).fetchone())
```
This is the highest-frequency DB query in the system. There is no in-memory cache, no TTL, no memoization. At 10 concurrent users, this means 10 concurrent auth DB queries per request wave.

## Semaphore & Lock Inventory

| Resource | Type | Size/Capacity | Timeout | Created at |
|----------|------|---------------|---------|-----------|
| Vector store write lock | `asyncio.Lock` | 1 | 30s (`backend/app/services/vector_store.py:86`, `write_lock_timeout_seconds`) | Per VectorStore instance |
| Search semaphore | `asyncio.Semaphore` | 16 (default, `backend/app/config.py:113`, `vector_search_concurrency`) | **None** | Per VectorStore instance |
| Embedding batch semaphore | `asyncio.Semaphore` | 4 (global, `backend/app/services/embeddings.py:117`) | None | Module-level singleton |
| Background write semaphore | `asyncio.Semaphore` | 1 (`backend/app/services/background_tasks.py:224`) | None | Per BackgroundProcessor |
| Circuit breaker (embeddings) | `AsyncCircuitBreaker` | fail_max=5, reset=30s | N/A | Module-level singleton |
| Circuit breaker (LLM) | `AsyncCircuitBreaker` | fail_max=5, reset=60s | N/A | Per LLMClient instance |
| Circuit breaker (reranker) | `AsyncCircuitBreaker` | fail_max=3, reset=30s | N/A | Module-level singleton |

## Exhaustion Headroom Calculation

To determine how many concurrent users the system can support:

```
headroom = floor(pool_size / connections_per_request) - concurrent_users
```

Where `connections_per_request` is the count of separate pool connections consumed across the full middleware + dependency chain for the worst-case request path.

**Example at 10 concurrent users with a vault-scoped POST:**
- Each request: auth (1 connection) + evaluate_policy (1 connection, standalone) = 2 connections
- Main pool: 10
- Without double-connection fix: `floor(10 / 2) - 10 = -5` → **exhausted** (5 users worth of requests blocking)
- With double-connection fix: `floor(10 / 1) - 10 = 0` → **at capacity**

The headroom formula reveals that eliminating the double-connection pattern is a 2× improvement in effective capacity.

## `asyncio.to_thread` Usage Pattern

Services using `asyncio.to_thread` for sync DB operations (correct — SQLite I/O releases GIL during C-level execution):
- MemoryStore: `search_memories()` → calls `_fts_search` + `_dense_search` (both sync, connections held **sequentially**, not simultaneously)
- Document retrieval: `_get_indexed_file_ids` in `backend/app/services/rag_engine.py:741`
- Wiki retrieval: `wiki_retrieval.retrieve` in `backend/app/services/rag_engine.py:626`
- KMS retrieval: `kms_retrieval.retrieve` in `backend/app/services/rag_engine.py:643`

Services that are **synchronous in an async context** (event-loop blocking — should use `to_thread`):
- `backend/app/api/routes/vault_members.py` — all handlers are `def`, not `async def`. FastAPI wraps sync handlers in a threadpool, but using `asyncio.to_thread` explicitly would be more consistent.

## Load Test Checklist

When testing scalability at 8-10+ concurrent users, verify:
1. Pool exhaustion: `asyncio.gather(*[client.post(vault_scoped_route) for _ in range(10)])` → all 10 succeed, none timeout
2. Auth caching: 10 rapid requests from same user → only 1 auth DB query (not 10)
3. Permission check: `asyncio.gather` batching vs sequential — measure latency at N concurrent
4. Search semaphore contention: 10 concurrent searches → measure p50/p99 tail latency
5. Write lock starvation: concurrent search + upload → searches don't time out waiting for index creation
