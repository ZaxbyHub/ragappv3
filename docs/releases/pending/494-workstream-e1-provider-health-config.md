# fix: unify provider health, configuration and service behavior (E1, issue #494)

## What changed

Provider-circuit honesty: embedding (single + batch), non-streaming chat, reranker, and the
model-availability probes now classify HTTP outage statuses (5xx/429) *inside* the circuit-breaker
operation, so consecutive 503s open the breaker and short-circuit the next call without an HTTP
request (the status check used to run after the breaker had already recorded success). The
`model_checker_cb` breaker is wired at the per-dialect probe boundary; "model not found" listing
answers do not charge it, only transport/host failures do.

Deep-health probes are now isolated: `EmbeddingService.embed_probe(timeout)` sends the ping with a
per-request deadline (no shared-field mutation — the old `service.timeout` swap never reached the
persistent client's 60s timeout) and bypasses both embedding caches, so a warm cached ping can no
longer mask a provider outage. Pool-stats logging reports configured limits plus live httpcore
counts when introspectable and says "live counts unavailable" otherwise — never fabricated zeros.

RAG runtime: maintenance mode now gates the multi-query orchestration path (and its fallbacks),
not just single-query retrieval; `SearchSemaphoreTimeoutError` raised from variant searches
propagates to the 503 busy response instead of being wrapped/dropped; fallback clients dedup by
`(base_url, model)` so a second model on the same endpoint is actually tried; the SSE parser
follows the spec (field name at first colon, one optional leading space, multi-line data
assembly, CRLF) so compact `data:{...}` frames yield real answers; agentic synthesis provider
failures return an honest failed tool result (and the planner falls back) instead of echoing the
input as a successful answer. Prompt-version activation is atomic — creating an active version
deactivates the prior one in the same transaction.

Settings contract: startup replay now consumes the exported `PERSISTED_FUNCTIONAL_FIELDS`
single-source list (the 20-field save/replay drift — ingestion mode, instant skip flags, wiki
lint + curator settings — is closed and guarded by an import-time equality check); the file
watcher reconciles start/stop/cadence on save without an app restart; legacy fields
(`chunk_size`, `chunk_overlap`, `vector_top_k`) convert through one shared implementation at
construction *and* on the live update path; `retrieval_window=0` is accepted and documented as
"disables neighbor-chunk expansion"; the Settings connection probe POSTs a minimal embedding
request instead of GET-ing a POST-only endpoint; `thinking_max_tokens` and `instant_max_tokens`
actually reach the chat clients (they were saved-but-ignored); new `instant_enable_thinking`
call-role setting documents the Instant thinking kwarg (default preserves today's Gemma-4
behavior; `true` omits the kwarg for thinking-capable Instant models).

Email: the IMAP paths now bind to the installed aioimaplib 2.0.1 contract — the greeting is
awaited (it returns None; the old `!= 'OK'` comparison failed every successful connection),
fetch parses `Response(result, lines)` literals via the `{N}` size hint, SEARCH serializes as
`SEARCH UNSEEN` (`charset=None` keyword instead of a positional None criterion), and the unseen
counter checks `response.result`. Test fakes mirror the real library shapes.

API/ops/docs: the custom validation handler encodes `input` with `jsonable_encoder` (text/plain
bodies now get a structured 422, not a 500) — this is the scoped standard error envelope for
request-validation failures, consumed client-side where FastAPI 422 arrays now format as
`field: message` lines instead of `[object Object]`; docker-compose forwards every documented
`.env.example` variable to the backend container (47 added; host-only exceptions and secrets
documented in `.env.example`); the health route documents its single-worker module cache and
warns once when `WEB_CONCURRENCY != 1`.

Settings UI: load failure clears the skeleton and offers Retry; correcting an invalid field
re-enables Save without discarding edits; edits made during an in-flight save survive as dirty;
zero-valued numeric settings render as 0 (nullish fallbacks); the save footer never exposes a
false "Saving…" status to assistive technology when idle.

## Why

Issue #494 (Workstream E1) owned 28 audited findings plus FU/DEEP-D-03/LIVE-10/ENH-015/legacy-10b
obligations: settings and service status did not accurately control running behavior or survive
restart, and provider/email modes did not behave according to their real contracts. Each finding
was re-anchored, reproduced by a frozen acceptance check (40 checks: 31 discriminating,
2 new-surface, 7 preserving), fixed, and verified RED→GREEN.

## Migration

- No schema migration. `settings_kv` rows written by the newly-replayed fields are valid inputs
  to older code (it ignored them).
- Deployments that saved any of the 20 previously-drifted fields will now see those saved values
  applied at startup (previously silently reverted to defaults).
- Instant-mode requests from callers that omit an explicit `max_tokens` now default to the
  configured `instant_max_tokens` (4096) instead of a hardcoded 32768 — matching the setting's
  documented intent.

## Caveats

- `retrieval_window=0` is now accepted by the API; the retrieval engine already treated 0 as
  "no expansion" — only the validator disagreed.
- The bandit baseline was regenerated: pure line-shift (137 entries before and after, identical
  per-file/test-id counts — zero new suppressions).
- Standard error envelope scope: request-validation failures (the FastAPI `detail` array) are the
  documented envelope adopted here; other handlers keep their existing shapes.
- Out of scope per the issue: FU-002 (CSRF, Workstream X), FU-007/C2 orphan recovery, FU-008/C3
  batched polling, FU-009/E2 virtualization flake, API-003/D2, deployment inventory/F2, and all
  open dependency PRs (dispositioned in the PR body, none merged).
