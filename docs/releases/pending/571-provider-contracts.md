# Provider contracts: native keep-alive/TTL, usage-based token counts, structured output, explicit context windows (#571)

## Summary

Ollama and LM Studio are now held ready by their own native residency mechanisms instead
of a synthetic 30-second `max_tokens=1` ping loop; token budgets can use the providers'
authoritative usage counts instead of a `len(text) // 4` guess; JSON-expecting stages
(planner, curator/chunk enrichment, Draft Room stages, research packets) send a
provider-side JSON-schema constraint; and every client factory pins an explicit context
window instead of following provider server defaults (issue #571, audit finding E08).

## New operator-visible settings

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | `-1` | Ollama `keep_alive` sent on the one-shot native preload call for the thinking/editorial backends. `-1` keeps the model loaded indefinitely — the same effective residency the retired ping loop provided. Ollama also accepts duration strings (`"30m"`) or seconds. |
| `OLLAMA_NUM_CTX` | `4096` | Explicit context window pinned on the loaded Ollama instance. 4096 is Ollama's documented default, so this is a controls change, not a capability change. The OpenAI-compatible surface cannot set it per request. |
| `LM_STUDIO_TTL` | `86400` | Idle TTL in seconds carried on every Instant (LM Studio) chat payload. The idle timer resets on every request; 24h default is far more generous than LM Studio's 60-minute JIT default. |
| `LM_STUDIO_CONTEXT_LENGTH` | `4096` | Context length requested from LM Studio's native model-load endpoint (v1, with a legacy v0 fallback for pre-0.4.0 servers). |

All four are env/restart-applied: they are consumed when client factories run at startup
and are intentionally not part of the runtime settings API (a hot change could not reach
the already-constructed clients' residency posture without a reconnect).

## Behavior changes

- **No synthetic keep-alive traffic.** The 30-second ping loop is removed. Ollama models
  are preloaded once at startup with `keep_alive`/`num_ctx`; LM Studio receives `ttl` on
  every request and a context-length-bearing load call at startup. If priming fails (e.g.
  the provider is briefly unavailable at startup), the model still loads on demand with
  provider defaults — startup and chat are never blocked by priming.
- **LM Studio JIT single-model caveat (unchanged provider behavior, now documented):**
  LM Studio's Auto-Evict keeps at most one JIT-loaded model resident per server. When a
  different model is requested on the same LM Studio server, the Instant model can be
  evicted even with a long TTL. The retired ping loop never protected against this either.
- **Exact token accounting.** `chat_completion` / `chat_completion_stream` now read the
  provider's `usage` object (Ollama maps `prompt_eval_count`/`eval_count` onto it;
  streaming requests it via `stream_options.include_usage`). `last_metrics` gains
  provider-exact `prompt_tokens`/`completion_tokens` keys; the previous
  `*_tokens_estimate` values remain and are still the only values when a provider omits
  `usage`.
- **Structured output.** The planner decision, chunk-enrichment (curator), Draft Room
  structured stages, and the research packet now pass a `json_schema` `response_format`,
  so malformed model JSON is a provider-side constraint violation instead of a downstream
  parse failure. Existing tolerant parsing and the one-repair path are unchanged.
- **Embeddings default dialect.** A bare Ollama host URL (no explicit path) in
  `OLLAMA_EMBEDDING_URL` now resolves to the modern `/api/embed` route instead of the
  superseded `/api/embeddings`. Explicit `/api/embeddings`, `/api/embed`, `/v1/embeddings`
  (OpenAI/TEI) URLs are unchanged; the default TEI deployment is unaffected.

## Compatibility and rollback

- `LLMClient` gained four optional constructor parameters (all default `None`); existing
  constructions and the `reconfigure()` surface are unchanged. The new residency fields
  are factory-fixed by design.
- Streaming requests now carry `stream_options: {"include_usage": true}`. Both named
  providers document support; an operator pointing the client at an exotic
  OpenAI-compatible server that rejects unknown fields would need that server updated.
- Rollback for any sub-change is reverting the corresponding client/lifespan/embeddings
  edit; nothing here touches persisted data or the database schema.

## Known limitations

- Live-provider behavior was verified against vendor documentation (Ollama OpenAI
  compatibility + native API docs; LM Studio v1 REST + TTL/Auto-Evict docs), not against
  a reachable live host — no Ollama/LM Studio instance was reachable from the working
  environment (the same constraint recorded by the source audit).
- Residency settings are restart-applied, not hot-reloaded (see above).
