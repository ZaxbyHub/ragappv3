# evaluate_policy double-connection elimination (Issue #205, S-003)

## What changed

### Backend — connection-pool demand halved on vault-scoped routes
- Migrated all 20 standalone `evaluate_policy(...)` call sites across 7 route
  files (`chat.py`, `memories.py`, `kms.py`, `folders.py`, `tags.py`,
  `wiki.py`, `vault_members.py`) to the DI variant
  `evaluate: Callable = Depends(get_evaluate_policy)`, which reuses the
  request-scoped `Depends(get_db)` connection instead of opening a second
  pool checkout.
- Migrated `vault_members.py`'s 8 handlers off the legacy
  `get_pool()/get_connection()/try/finally/release` boilerplate onto
  `Depends(get_db)` for consistency with the rest of the codebase.
- `wiki_events_stream` (SSE endpoint) intentionally retains a transient
  standalone `evaluate_policy` for its pre-stream permission check; see the
  inline docstring for the connection-lifecycle rationale.

### Tests
- New `test_route_policy_no_double_connection.py` asserts `get_pool` is NOT
  called on the request path for one representative route per migrated file,
  plus a carve-out test locking in the `wiki_events_stream` design decision.
- Updated `test_chat_fork.py`, `test_chat_message_sanitization.py`, and
  `test_vault_members_integrity_error.py` to the new DI/override patterns.

## Why
The standalone `evaluate_policy()` (`deps.py:724`) opened its own pool
connection via `get_pool()`. Vault-scoped routes that called it while also
holding a `Depends(get_db)` connection consumed **2 pool connections per
request**. At 10 concurrent users this over-subscribed the pool 2:1 (demand
20 vs supply 10), causing a 15s block then `RuntimeError` → HTTP 503.
Per-request pool demand returns to 1×, restoring headroom for 10+ concurrent
users.

## Migration steps
No migration required. All changes are backward-compatible:
- Authorization semantics are byte-for-byte preserved — both the standalone
  and DI paths delegate to the same `_evaluate_policy(db, ...)` core.
- The standalone `evaluate_policy` is retained in `deps.py` for backward
  compatibility (still tested by `test_require_vault_permission_di_adversarial.py`).
- No public API, schema, or config change.

## Breaking changes
None.

## Known caveats
- `wiki_events_stream` still opens a transient second connection for its
  pre-stream permission check (then releases it before streaming). This is
  the same shape as the pre-fix code and is documented inline. The separate
  concern of SSE connection pinning via `Depends(get_db)` (auth holds one
  connection for the whole stream) is tracked under issue #301.
