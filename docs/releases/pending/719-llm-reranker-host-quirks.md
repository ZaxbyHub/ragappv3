# 719 — LLM/reranker host-provider quirks (Ollama, LM Studio, TEI, vLLM) and connection reuse

## Summary

Fixes six host-behavior mismatches in the LLM/reranker clients (issue #719,
Workstream C PR 14 of 14):

- **In-band stream errors now fail the stream** (vLLM-style
  `data: {"error": ...}` after HTTP 200): `chat_completion_stream` raises
  `LLMError` (status `inband_error`, one breaker failure) instead of skipping
  the payload and recording a breaker success with `last_metrics.status=ok`.
- **Streamed responses drain to EOF after `[DONE]`** so httpx pools the
  connection for the next call instead of closing it (removes the ~0.27 s
  first-call reconnect on Windows/localhost measured in the S2 attribution).
  Trailing bytes after `[DONE]` are consumed, not parsed.
- **Non-dict SSE payloads are skipped** instead of crashing the generator with
  an uncaught `AttributeError`.
- **A Qwen instant-mode stream that still yields `reasoning_content` logs one
  WARNING per stream** naming the ignored `enable_thinking=false` control
  (upstream LM Studio builds ignore the kwarg on recent Qwen GGUFs).
- **The reranker splits over-cap `/rerank` candidate sets** at the new
  `RERANKER_MAX_BATCH` setting (default 32, matching TEI's bundled
  `--max-client-batch-size`, now declared in compose): a 40-chunk set against
  a stock TEI still reranks (batches merged, one sigmoid, same normalized
  `_rerank_score` semantics) instead of silently degrading to vector order on
  the 422. At or below the cap the request is byte-identical to before.
- **The LM Studio `/api/v0/models/load` fallback is removed**: that endpoint
  never existed in LM Studio's v0 reference, so the retry 404'd on exactly the
  old servers it existed for. A 404 from `/api/v1/models/load` now logs a
  targeted warning and `prime_residency()` returns `False` without a second
  request.
- **Ollama operator constraint documented** (INSTALLATION.md, `.env.example`):
  `OLLAMA_NUM_CTX` must equal the Ollama host's automatic context length
  (`OLLAMA_CONTEXT_LENGTH`) — on a mismatch the first chat turn reloads the
  model and `keep_alive` reverts to the 5-minute default. The chat surface
  itself (OpenAI-compatible) cannot carry a per-request context size, so the
  preload-only application is unchanged.

## Tests

- `backend/tests/test_c14_host_quirks.py` — AC2 reasoning-leak warning, AC8
  no-v0-fallback (frozen acceptance checks).
- `backend/tests/test_c14_tei_batch_cap.py` — AC4 over-cap batch split.
- `backend/tests/test_c14_stream_inband_error.py` — AC5 in-band error raise.
- `backend/tests/test_c14_stream_drain.py` — AC6 body drained after `[DONE]`.
- `backend/tests/test_c14b_extra_coverage.py` — validator, non-dict skip, and
  the reconfigure-None regression guard.
- `backend/tests/test_provider_contracts_571.py` — the v0-fallback test now
  pins the removed behavior (single v1 request, no v0, `False`); the Ollama
  preload contract is unchanged.

## Follow-through (non-executable ACs)

AC1/AC3/AC7 need live hosts; their executable proxies (docs + AC2/AC6) ship
here. Operators: verify `ollama ps` after one chat turn (AC1) and one
Instant-mode turn on a live LM Studio Qwen build (AC3); re-run the S2
attribution probe for the connection-reuse count (AC7).
