# Chat "0 content output" regression — root cause (code-confirmed) + fix plan

Branch: `claude/chat-output-regression-18p9op`  •  Repo at 63e723d  •  Reviewer-validated (all 5 code claims CONFIRMED)

## Mechanism (all code-verified in this repo)
Chat `/chat/stream` → `rag_engine.query(stream=True)` → `_stream_llm_response` → `LLMClient.chat_completion_stream`.

1. `chat_completion_stream` (llm_client.py:413-417) DROPS every delta whose `content` is empty →
   all `reasoning`/`reasoning_content` deltas are discarded; only real `content` is streamed.
2. It also suppresses inline `<think>…</think>`. If a `<think>` opens but `</think>` never arrives
   before stream end, `_thinking_active` stays True and ALL remaining text is suppressed. There is
   **no post-loop `_buffer` flush** (llm_client.py:~564) — a legit terminated answer still sitting in
   `_buffer` at stream-end is also dropped (second latent bug).
3. `_stream_llm_response` (rag_engine.py:2306-2329) sets `emitted_content=True` on ANY yielded chunk
   — **including a whitespace-only chunk** — and after the loop `return`s at 2316 with **NO
   non-streaming fallback** when zero content was produced. (GLM's report: old code had a fallback here.)
4. Non-streaming `chat_completion` (llm_client.py:245-250) reads `message.content` then
   `sanitize_assistant_content`, whose `_UNTERMINATED_THINK_TAIL_RE` (assistant_sanitizer.py:31,74)
   strips an unterminated `<think>…` (no close) to `""`. So a budget-truncated reasoning response is
   ALSO empty in non-streaming.
5. Budgets (rag_engine.py:736/742): INSTANT `max_tokens = settings.instant_max_tokens`
   (default 4096 in config.py:56, but **this deployment set 16384** in settings_kv); THINKING
   hardcoded **32768**. Utility calls use tiny budgets (query-transform 100, retrieval-eval 64) that
   reasoning models can't answer within → already returning empty per GLM logs.

## Why GLM's restored fallback "didn't fire"
Two concrete, code-backed reasons:
- **Whitespace defeats the guard.** If the model streams any whitespace-only `content` (e.g. `"\n\n"`),
  `chat_completion_stream` yields it, `emitted_content=True`, and `if emitted_content: return` skips the
  fallback. User sees a blank answer.
- **Same-budget fallback is also empty.** If it's budget exhaustion (finish_reason=length, unterminated
  `<think>`), the non-streaming fallback at the SAME 16384/32768 budget returns `""` too (sanitizer
  strips the unterminated tail). Fallback runs but yields nothing.

## Two sub-modes — need ONE live datum to pick (diag_stream_vs_nonstream.py)
- **A (budget/non-convergence):** stream=0 content, finish_reason="length"; the model burns the whole
  budget reasoning. Fix needs budget reduction + honest empty-handling, NOT just a fallback.
- **B (filter/whitespace):** stream has content deltas (model answered) but suppressed / only whitespace;
  non-stream has real content. A *correct* fallback (non-whitespace guard) fixes it.
GLM's own evidence (tiny-budget utility calls also return empty) strongly favors A being real/dominant.

## Fix plan (layers; A-answer selects emphasis)
- **L1 (both):** in `_stream_llm_response`, count only NON-whitespace content; after the loop, if none,
  fall back to non-streaming `chat_completion`; yield if non-empty; else emit an honest error chunk
  (not a silent blank done).
- **L2 (latent bugs):** flush trailing `_buffer` after the stream loop when not `_thinking_active`;
  treat stream-end-while-`_thinking_active` as "no content" so L1 triggers.
- **L3 (sub-mode A):** reasoning models over-reason under huge budgets. Lower `instant_max_tokens`
  (16384→~4096 or less) and the hardcoded 32768; raise the tiny utility budgets (64/100) — or route
  utility calls to a non-reasoning path. Meta-root-cause: app's reasoning-filter + budgets assume
  models that emit content promptly; the deployment switched to reasoning models that don't.

## Status: root cause CONFIRMED at code level. Awaiting GLM live diagnostic (finish_reason +
## content-delta counts for the real heavy prompt) before finalizing L3 emphasis and implementing.
