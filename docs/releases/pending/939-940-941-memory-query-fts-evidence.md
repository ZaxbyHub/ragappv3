# Memory query lifecycle bounds, FTS semantics pins, c07 evidence hardening (#939, #940, #941)

## Memory embed budget + admission (#939)

- New setting `MEMORY_EMBED_BUDGET_SECONDS` (default 10.0): the total per-call
  budget for the memory query-embedding precompute. One `asyncio.timeout` window
  covers BOTH the `EMBEDDING` admission acquire and the embed await (the budget
  binds before the embedding client's own 60s per-request timeout, so a
  slow-but-alive provider degrades the memory leg instead of holding the
  device), on the two production precompute call sites — the chat-turn engine path
  (`RAGEngine._memory_retrieve`) and the memories search route
  (`_perform_memory_search`, both `GET /memories/search` and the POST search
  model). On expiry the embed task is cancelled, a WARNING names the mechanism
  (the expiry arm: "Memory query embedding precompute exceeded the …
  memory_embed_budget_seconds budget; continuing FTS-only"; the saturated
  arm: "… admission rejected (admission rejected: …)"), and the memory leg
  completes FTS-only within the budget instead of stalling the turn on a
  hanging embed.
- The memory precompute now acquires an `EMBEDDING` admission slot (issue #518
  device budget) on both call sites. A saturated budget degrades the memory leg
  FTS-only with its own admission-rejected WARNING instead of bypassing the
  budget. Capacity
  note: memory-search requests and chat turns now share the embedding device
  budget with retrieval embeds (one slot per call, held for the embed only.
  Operator note: the memories search routes carry no rate limiter, so a burst
  of concurrent authenticated searches now contends with chat-embed capacity —
  each hold is capped at the budget and authorization precedes the embed; add
  a limiter if search traffic is untrusted —
  and the lease release now completes even when the budget window cancels the
  holder mid-embed, so an expiring budget can no longer hold the slot until
  the store TTL: the admission lease's `release()` (the `_Lease` class in app/services/admission.py) finishes the local accounting and
  shields the store release, then re-raises the cancellation so the #827
  cancellation-coverage contract is preserved; when the holder is cancelled
  mid-store-release, the hub wakeup is DEFERRED to the detached release's
  completion so the pump observes a freed slot on latency stores, with
  `_mark_degraded` on its failure).
- A blocked admission (a controller whose acquire never yields) is also bounded
  by the same budget window — previously the acquire could wait unboundedly
  (no default admission deadline).
- Overlap-task lifecycle: every early exit of the chat stream between task
  creation and the overlap awaits (generator close included) now cancels the
  in-flight memory/KMS tasks via a try/finally backstop. The admission-rejected
  exit already cancelled (pinned by `test_c08_admission_rejected_cleanup.py`).

## New 422 surfaces on memory search (#939 AC2)

- `GET /memories/search`'s `query` parameter and `MemorySearchRequest.query`
  (the POST search model) are capped at 10000 characters, matching the memory
  content cap; longer queries are rejected with 422 BEFORE any embedding work.
  Layer note: the embedder's per-text input cap
  (`EMBEDDING_MAX_TEXT_CHARS = 8192`, issue #698) is NOT enforced on the
  single-text embed path, so queries of 8193–10000 characters are accepted and
  keep their existing PROVIDER-DEPENDENT embed behavior (only an exception or
  empty embedding degrades FTS-only); queries over 10000 now get a clean 422.
  The frontend memory-search input is uncapped and sends verbatim text — users
  pasting very long queries will now see the 422 instead of a provider-dependent
  embed outcome. Chat messages are NOT affected (the existing configurable
  `chat_message_max_chars` cap, issue #922, is unchanged).

## Memory FTS semantics affirmed + cap-order fix (#940)

- Stop-word filtering now precedes every token cap in the memory FTS arm
  (`build_fts_match_query` gained an optional `stop_words` parameter; the
  memory call site passes `_FTS_STOP_WORDS`): a query with arbitrarily many
  leading stop words keeps every content term. Previously a query whose first
  64 raw tokens were all stop words lost every content term and returned `[]`.
  Other tokenizer callers (wiki/KMS/documents) are byte-identical (default
  `stop_words=None`).
- The post-#712 memory FTS semantics are now pinned as the STANDING, deliberate
  contract in `backend/tests/test_memory_fts_semantics.py`: single-term ASCII
  prefix matching ("cat" matches "cats"/"category"), the 8-content-term cap, and
  operator words (OR/AND/NOT/NEAR) as literal conjunctive prefix terms. The
  operator-word narrowing is recorded there as the formal exception to issue
  #712's "keep matching every ASCII query it already matches" constraint
  (the #515-converged tokenizer semantics; forking a memory-private tokenizer
  would re-lose CJK/accented recall).

## c07 evidence hardening (#941, no production behavior change)

- The dense-degradation oracle in `test_c07_memory_bridge.py` now credits the
  `embed_query` mechanism message ("Memory query embedding…") instead of any
  WARNING on the memory_store logger (the old oracle was satisfiable by the
  never-attempted tripwire alone).
- The fail-open wiring check in `test_c07_evaluator_reasoning_budget.py` now
  AST-binds the three `record_evaluator_fail_open` call sites and asserts a
  recorded fail-open reaches the `/metrics` exposition (the old shape was a
  token-presence substring scan that stayed green when all call sites were
  deleted).
- `test_c07_telemetry_flush.py` no longer falls back to mutating the stdlib
  `json` module process-wide; the module-local `_dumps` seam is the only patch
  surface (the seam is pinned by the new
  `test_c07_evidence_oracles.py::test_telemetry_serialization_seam_is_module_local`).
- New suite `backend/tests/test_c07_evidence_oracles.py` (5 checks): mechanism
  credit, live call-path wiring, an alias-resolving all-spelling event-loop
  census over `memory_store.py`, the underscore single-token pin with a control
  row, and the telemetry seam pin — each strengthening mutation-demonstrated in
  the PR evidence.

## Tests added

- `backend/tests/test_memory_query_lifecycle.py` (4 frozen checks, issue #939)
- `backend/tests/test_memory_fts_semantics.py` (4 checks, issue #940)
- `backend/tests/test_c07_evidence_oracles.py` (5 checks, issue #941)
- `backend/tests/test_memory_query_lifecycle_companions.py` (14 non-frozen
  tests: cancellation observation with value sensitivity, rejected-admission
  zero-embeds + FTS kwarg, active-lease occupancy, blocked-admission budget
  bound, three route-side legs, the real-controller budget-expiry release pin,
  the telemetry json-binding pin, the event-gated shield + re-raise falsifier
  pin (a real controller over a yielding store; kills the shield-removal and
  re-raise-removal mutants), POST/GET query-cap boundary pins, the 8-term cap
  lower-bound pin, and the finite-budget-validator pin)
