# fix(backend): #1209 off-loop LLMClient sanitizer + #1199 single connection per organizations request

## What changed

- **#1209** — `backend/app/services/llm_client.py`: both completion paths
  (`chat_completion` and the non-SSE branch of `chat_completion_stream`)
  now dispatch `_strip_thinking_content` through `asyncio.to_thread`. The
  regex-heavy assistant sanitizer (five `re.sub` passes plus a line-walking
  wrapper stripper over the full provider response) no longer stalls the
  event loop on every completion. Returned content and provider-exact token
  accounting are unchanged; a frozen thread-identity spec
  (`backend/tests/test_llm_client_offloop_sanitizer.py`) pins both paths.
- **#1199** — `backend/app/api/routes/organizations.py`: all 18 handlers
  migrated from a second standalone `pool.get_connection_async()` checkout
  to the request-scoped `db: sqlite3.Connection = Depends(get_db)`
  connection shared with the `require_role` auth chain (the pattern PR #1198
  shipped for users.py). One pooled checkout and one release per request
  now holds across success, pre-auth denial, authenticated in-handler 403,
  and exception outcomes — pinned by the five-leg counting-pool spec
  (`backend/tests/test_p02c_orgs_pool_single_connection.py`).
- Nine direct-caller/race harness suites migrated to the `db=` call-site
  seam (distinct connections per simulated request preserved).
- New Quality-contracts guardrail `scripts/check_double_hold_routes.py`
  (fail-closed on unclassified auth dependencies; C-DBLHOLD-1 double-hold,
  C-DBLHOLD-2 unknown-auth, C-DBLHOLD-3 manual-release rules), wired into
  `ci.yml`, the `justfile` `quality-contracts` recipe, and the script
  inventories in `AGENTS.md`, `docs/engineering/conventions.md`, and
  `docs/engineering/testing.md`; contract tests in
  `backend/tests/test_double_hold_guardrail.py`.

## Why

- #1209: loop-stall latency scaled with response size on every chat
  completion, delaying concurrent requests (defect class fixed for chat.py
  under #1202; LLMClient was the deferred residual).
- #1199: the 18 organizations handlers held two pooled SQLite connections
  per request (the #205 hold-and-wait class), halving effective pool
  capacity on those routes; the 2026-11 deployment audit observed pool
  saturation on shallow calls (tracker #1148).

## Migration

No schema, config, or operator action. The draft-room
`upload_draft_input` detached-commit connection keeps its
independent-lifetime ownership by design (documented exception in the
guardrail).

## Caveats

- Direct Python callers of the migrated organizations handlers must now
  pass `db=` (HTTP contract unchanged).
- The guardrail fails closed: a new `Depends(require_...)`-shaped auth
  dependency must be classified in the script's holding/non-holding sets.
