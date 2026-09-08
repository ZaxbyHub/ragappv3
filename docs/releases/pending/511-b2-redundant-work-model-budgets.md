# Workstream B2: remove redundant RAG work and enforce model-aware inference budgets (Issue #511, PR 2 of 3)

## What changed

### Backend

- **Embedding provider parity and per-request config (EMBED-001/EMBED-002)** —
  `backend/app/services/embeddings.py`: the Ollama dialect is now
  endpoint-aware. A configured modern `/api/embed` endpoint gets the modern
  `{"model", "input"}` contract for single AND batch calls (previously
  misdetected as TEI); the legacy `/api/embeddings` endpoint keeps its
  `{"model", "prompt"}` single-shot contract and batch ingestion now issues
  bounded per-item prompt requests (ordered, semaphore-capped, per-item
  bounded retry) instead of sending the modern `input` payload that the
  legacy endpoint rejects. Every embedding request now captures a config
  snapshot (URL, mode, dialect, model, prefixes) once at request start; the
  same snapshot builds the payload, parses the response, derives cache keys,
  and fills `last_metrics` — a provider change made via the Settings UI
  mid-flight no longer corrupts an in-flight request (the in-flight request
  completes under its issuing config; the NEXT request uses the new config).

- **Reranker repairs (RERANK-001/002/003/004)** —
  `backend/app/services/reranking.py`: the local CrossEncoder cache is keyed
  by model identity (LRU, max 2 loaded models) so changing
  `reranker_model` — or running two service instances with different models —
  takes effect immediately instead of keeping the first-loaded model until
  restart. A single-chunk rerank input that bypasses scoring now returns
  `success=False` (previously `True` with no computed score, which displayed
  a raw distance under rerank-score semantics downstream). TEI `/rerank`
  requests now send `raw_scores: true` so the client applies its
  overflow-protected sigmoid exactly once to raw logits, independent of the
  server's default normalization (previously TEI's normalized 0..1 scores
  were sigmoid-compressed a second time into ~0.5-0.73, displaying
  near-zero relevance as mid relevance). Ordering is unchanged.
  `backend/app/services/feedback_reranker.py`: feedback votes now aggregate
  over DISTINCT (message, document) pairs — one rated answer citing several
  passages of the same document contributes exactly ONE vote for that
  document (previously N passages = N votes, reaching the ±0.10 adjustment
  cap from a single rating).

- **Lazy query-variant embeddings; consolidate-then-rerank; KMS overlap
  (RAG-DEEP-01/02/03)** — `backend/app/services/rag_engine.py`: query-variant
  embeddings are computed lazily for the selected route. When the query
  planner decomposes into multiple sub-queries, the step-back/HyDE variants
  are no longer embedded (they were discarded by that route); the original
  query is embedded once and reused when a sub-query matches it; when wiki
  evidence directly answers the query, no query embeddings are computed at
  all. Sub-query orchestration now fuses and identity-deduplicates evidence
  from all sub-queries BEFORE one final rerank call (previously every
  sub-query reranked separately against the same user query and dedup ran
  after rerank) — one provider rerank call per orchestration instead of N,
  with paired facets preserved by the identity-aware dedup key. KMS
  retrieval now runs concurrently with the document pipeline instead of
  being awaited ahead of every raw search; wiki evidence still gates raw
  RAG (precedence preserved). `finish_reason` from the LLM (stream and
  non-stream) is captured into the client's `last_metrics` and recorded on
  the RAG trace when present, so provider-side truncation is observable.

- **Optional total model-aware prompt budget (FULL-ENH-01)** — new
  `backend/app/services/token_accounting.py` (`count_tokens`: tiktoken when
  installed, otherwise a conservative script-aware fallback that never
  undercounts CJK the way the legacy len/3.5 estimate did) and a budget pass
  in `PromptBuilderService.build_messages`. When enabled, the assembled
  prompt (system instructions, history, retrieved evidence, memories,
  wiki/KMS, visual observations) plus reserved answer tokens is kept within
  the configured model context window; overflow is shed lowest-value-first
  (oldest history → supporting evidence → wiki/KMS tails → long memories,
  retaining unique short facts; the system prompt, user query, and the top
  primary evidence chunk are never shed — primary evidence #2/#3 are shed
  only as a last resort when the protected set alone would exceed the window)
  and the prompt carries a visible omission
  note plus an in-process structured budget report
  (`PromptBuilderService.last_budget_report`) for diagnostics. Default OFF: prompts are
  built exactly as before.

- **Event-loop hygiene (FULL-ENH-03/04)** —
  `backend/app/services/context_distiller.py`: the O(n²) greedy sentence
  dedup loop now runs in a worker thread with a bounded input
  (`context_distiller_max_sentences`, default 600), so long answers no longer
  monopolize the request event loop. `backend/app/services/redis_io.py`
  (new) + `embeddings.py`/`query_transformer.py`: the OPTIONAL Redis cache
  reads/writes (embedding L2 cache, query-transform and planner caches) run
  off the event loop under `redis_io_timeout_seconds` (default 1.0s) and
  degrade to cache-miss on timeout — a slow Redis can no longer stall every
  concurrent request. Auth-critical Redis (CSRF) is intentionally unchanged.

- **Chunk-enrichment response validation (FULL-ENH-05)** —
  `backend/app/services/chunk_enrichment.py`: auxiliary fields of the parsed
  enrichment response are validated independently (summary as text;
  questions/entities/aliases as lists of strings with today's caps); a
  malformed field is dropped with a warning instead of corrupting the
  enrichment or aborting the remaining fields.

### Config

New settings (all exposed via GET/PUT `/settings` and persisted across
restarts): `retrieval_consolidated_rerank` (True),
`retrieval_kms_overlap` (True), `prompt_budget_enabled` (False),
`model_context_tokens` (8192 — set this to match your deployed chat model's
context window before enabling the budget), `prompt_reserve_output_tokens`
(2048), `redis_io_timeout_seconds` (1.0),
`context_distiller_max_sentences` (600).

## Rollout and rollback

Performance changes are staged behind measured profiles:

- `retrieval_consolidated_rerank` and `retrieval_kms_overlap` ship enabled
  (they are the issue's required repairs). To roll back to the legacy
  behavior without redeploying, disable either flag via the Settings UI
  (`PUT /settings`): `retrieval_consolidated_rerank=false` restores
  per-sub-query reranking; `retrieval_kms_overlap=false` restores the
  serialized wiki/KMS gather. Both flags are named here as the rollback
  switches; full revert of the lazy-variant embedding change (no flag — it
  removes pure waste) is by reverting this PR.
- `prompt_budget_enabled` ships DISABLED; enable it only after setting
  `model_context_tokens` to the deployed model's real context window.
- Legacy dispositions: the semantic chunker was already wired as an opt-in
  strategy and query decomposition already exists (issue #511 rebaselined
  both claims as superseded; nothing rebuilt).

Admission limits remain coordinated with E3: the existing process-wide
embedding batch semaphore is the only admission limiter; no per-caller
process-local budgets were added.

## Known limitations

- The conservative token fallback (when `tiktoken` is not installed)
  intentionally overestimates CJK/code-heavy text; budget trimming may shed
  slightly earlier than strictly necessary. Installing `tiktoken` switches
  accounting to a real tokenizer with no code change.
- The legacy Ollama batch fan-out issues one HTTP request per passage;
  throughput-bound deployments should prefer the modern `/api/embed`
  endpoint (true batching).
- `finish_reason` is surfaced via client metrics and the RAG trace, not as
  a new SSE event type in this PR.

Closes scope for issue #511 findings EMBED-001, EMBED-002, RERANK-001,
RERANK-002, RERANK-003, RERANK-004, FULL-ENH-01/03/04/05 and
RAG-DEEP-01/02/03 (workstream B2, PR 2 of 3).
