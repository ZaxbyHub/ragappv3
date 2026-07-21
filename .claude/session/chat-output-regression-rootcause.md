# Chat "0 content output" regression — ROOT CAUSE CONFIRMED + FIX

Branch: `claude/chat-output-regression-18p9op` • Repo at 63e723d • Status: fixed, tested, CI gates green locally.

## Final root cause (code-confirmed + runtime-proven on Python 3.11)

**The SSE heartbeat loop added by issue #231 (reverse-proxy hardening, one of the 80 upgrade
commits) kills the RAG generator whenever the model is silent for >15s.**

`backend/app/api/routes/chat.py` (pre-fix lines 308-318):

```python
chunk = await asyncio.wait_for(rag_gen_ait.__anext__(), timeout=HEARTBEAT_INTERVAL)  # 15.0s
except asyncio.TimeoutError:
    yield ": heartbeat\n\n"
    continue
```

`asyncio.wait_for` **cancels** the awaited `__anext__()` task on timeout. Cancelling `__anext__`
throws `CancelledError` INTO the async generator at its current suspension point (deep inside
`chat_completion_stream`'s `aiter_lines`), terminating the whole generator. The next loop
iteration's `__anext__()` raises `StopAsyncIteration` → route finalizes with empty
`collected_content` and its own initialized `sources=[]` → `done` with 0 content / 0 sources.

Why every turn hit it: `llm_client.chat_completion_stream` (413-417) silently skips all
`reasoning`/`reasoning_content` deltas, so during a reasoning model's thinking phase the engine
yields NOTHING for 15-100s (GLM diagnostics: nemotron ~60-100s, ChatGPTN ~15-25s before first
content). The 15s silence window fires on essentially every real RAG turn in both modes.

## Evidence chain
1. GLM live diagnostics: both models return real content at every budget, streaming and
   non-streaming, `finish_reason='stop'`; reasoning arrives as a separate field (no `<think>`
   in content). Ruled out budget exhaustion, `<think>` filtering, endpoint issues.
2. Timing: real failing turn ends at ~20s (Drafting at ~5s + 15s HEARTBEAT_INTERVAL) while
   actual generation takes 30-142s. Exact match to a Drafting+15s cancellation.
3. Runtime repro (scratchpad/repro_waitfor_kill.py, Python 3.11.15): generator killed by
   CancelledError, events `['stage','heartbeat']`, 0 content — bug reproduced in isolation.
4. Explains GLM's failed hot-patch: the generator dies by CancelledError mid-`async for`, so
   the code after the streaming loop (the restored fallback + its log line) never executes.

## Fix (committed on this branch)
`chat.py`: replaced cancel-on-timeout `wait_for` with a persistent-pull pattern:
- `next_chunk_task = asyncio.ensure_future(rag_gen_ait.__anext__())` created once per chunk;
- `await asyncio.wait({next_chunk_task}, timeout=HEARTBEAT_INTERVAL)` — on timeout the task
  KEEPS RUNNING; the route emits `: heartbeat` and re-waits on the same task;
- `finally:` cancels a still-pending pull on client disconnect (no task leak);
- `HEARTBEAT_INTERVAL` hoisted to module level (same name; patchable in tests).

Tests (`backend/tests/test_reverse_proxy_hardening_231.py`):
- `test_heartbeat_emitted_on_timeout` rewritten off the implementation-coupled
  `asyncio.wait_for` mock (that mock encoded the bug) to a real stall.
- NEW `test_stalled_generator_survives_heartbeat_and_delivers_content`: content emitted after a
  multi-heartbeat stall must reach the client and the engine's done sources must survive.

Verification: repro proves mechanism; heartbeat+chat/stream batch 168 passed ×2 (stable);
`ruff check .` clean; all 5 `scripts/check_*.py` contract scripts pass. The one-off
4-failure batch run did not reproduce on either tree state (env flake, unrelated files).

## Secondary (real but NOT this regression; not fixed here)
- Tiny utility budgets: query-transform max_tokens=100, retrieval-eval max_tokens=64 return
  empty on reasoning models (budget consumed by the reasoning field) — degrades retrieval
  quality; pipeline fail-opens. Candidate follow-up.
- `_stream_llm_response` still has no non-streaming fallback when a stream truly yields no
  content; `chat_completion_stream` has no post-loop `_buffer` flush. Latent, not triggered
  by these models (they DO emit content once the route stops killing the stream).
- Deployment settings: `instant_max_tokens=16384` vs config default 4096 (works; just large).

## Deploy note for GLM
Rebuild image from this branch, or hot-patch `backend/app/api/routes/chat.py` into the
container (docker cp + restart). Revert the rag_engine.py hot-patch fallback if desired —
it's harmless but wasn't the fix.
