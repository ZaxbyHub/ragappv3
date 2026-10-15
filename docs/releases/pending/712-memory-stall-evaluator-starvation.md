# fix(rag): memory search stops bridging event loops; evaluator survives thinking models; fail-opens become observable

## What changed
- **Memory async bridge removed (RT-S7-03).** `MemoryStore` no longer bridges
  into the async embedding service with `asyncio.run` on worker threads —
  the mechanism (`_run_coro_sync`) is deleted and a module-wide AST census
  test bans its return. `search_memories` takes a precomputed
  `query_embedding` (new `await MemoryStore.embed_query(q)` helper runs on
  the caller's event loop); the RAG engine and the memories routes precompute
  before dispatching the sync search, with their own guards so ANY precompute
  failure degrades to a WARNING + FTS-only instead of aborting the memory
  search. `add_memory`/`update_memory_content` no longer embed inline: async
  callers follow the row write with the existing `embed_and_store` helper
  (the update route already did; the create route and the chat
  remember-directive path now do too, embedding only newly-inserted rows).
  Previously, fresh event loops created on worker threads corrupted the
  shared httpx client's connection pool — 30-40s stalls on roughly a quarter
  to half of chat turns (one py-spy capture: idle 10+ minutes inside the
  bridge), silently degrading dense memory recall to FTS with only a DEBUG
  log.
- **Telemetry recorders synchronized (P01-SK2-12).** All `Telemetry`
  counters/dicts mutate and serialize under one `RLock`; the shard flush
  catches `Exception` (not only `OSError`), so a `RuntimeError` from a dict
  mutated by another thread during `json.dumps` can no longer escape a
  recorder call reached from worker threads.
- **Memory FTS uses the shared Unicode tokenizer (TQ-contract-all-07).**
  `_fts_search` routes through `fts_query.build_fts_match_query` (issue #515
  convergence completed), so CJK ("北京") and accented ("café") queries
  recall via the always-on FTS leg; stop-word removal now precedes the token
  cap. Two disclosed semantic narrowings inherited from the converged
  tokenizer (pinned by tests): bare FTS5 operator words (AND/OR/NOT/NEAR)
  become literal prefix terms instead of accidental live operators, and
  underscore-joined queries are single `\w+` tokens.
- **Evaluator is reasoning-safe (TQ-instruction-batch-5-01).** The
  one-word classification call sends a per-call `reasoning_effort="none"`
  override (new sentinel-based per-call `chat_completion` parameters), so a
  thinking-mode server no longer burns the 64-token budget on reasoning and
  returns empty. An empty response retries once with the UNION of controls
  (`reasoning_effort="none"` + `chat_template_kwargs={"enable_thinking":
  False}`) before failing open.
- **Fail-opens observable (TQ-instruction-batch-5-02).** Every evaluator
  fail-open is counted by cause (exception / empty_response /
  unexpected_response) in telemetry and exposed at `GET /metrics` as
  `ragapp_retrieval_evaluator_fail_open_total{reason=...}` — the documented
  operator signal previously had zero production readers.

## Why
Audit remediation issue #712 (frontier audit run 20260923T174456Z; findings
RT-S7-03, P01-SK2-12, TQ-contract-all-07, TQ-instruction-batch-5-01/02).
All five defects silently degraded the RAG query path instead of surfacing
their own failure.

## Migration
No schema, config, or API migration. Behavioral notes: rows written by
callers that skip the embed follow-up are FTS-only until the background
backfill heals them (same outcome as a down embedding service before);
`search_memories` called directly with an embedding service configured but
no `query_embedding` now logs a WARNING naming the required helper.

## Known limitations
- A provider that ignores BOTH no-think controls keeps the evaluator verdict
  fail-open CONFIDENT; the outage is observable per-reason on `/metrics`
  (the issue's stated operator remedy) but the verdict itself stays pinned
  until the provider honors a control.
- `asyncio.run` remains in three non-memory subsystems (wiki curator offload
  executor, draft job processor/pipeline dedicated workers, a standalone
  migration CLI); each is dispositioned in the trace's recurrence sweep as
  NOT this defect class — none drives a process-wide loop-bound async client
  (the wiki curator coroutine creates and closes its own per-call
  `httpx.AsyncClient`; the draft policy coroutines contain their DB
  connection lifetimes; the migration is a standalone CLI owning its loop).
