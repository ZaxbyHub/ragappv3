---
issue: 688
title: Chat and canvas backend responses honour their documented contracts (done-payload parity, trace flag, safe auto-title, idempotent remember, truncation-aware range edit, flushed canvas draft)
---

# Chat and canvas responses honour their documented contracts

Workstream A PR 6 of 7 (audit remediation, frontier audit 2026-09-23).

## What changed

- **The non-streaming `/chat` response carries the citation-honesty fields**
  (#688 / TQ-instruction-batch-3-08). `ChatResponse` gained
  `citation_confidence` (pass-through typing: dict, float or int — the value is
  forwarded verbatim from the engine's done chunk) and `unverifiable_claims`,
  and `non_stream_chat_response` now reads both from the done chunk exactly as
  the streaming consumer always has. Non-streaming clients no longer silently
  lose the honesty metadata streaming clients receive for the same turn.
- **`rag_trace_in_response` now reaches the client** (TQ-instruction-batch-3-07).
  The engine attaches its `RAGTrace` to the done message when the flag is on;
  the route now forwards it as `trace` on the SSE done event under the same
  flag. With the flag off (the default) `trace` stays absent even if an engine
  attaches one.
- **A manual rename always beats an auto-title write** (TQ-sibling-batch-07-04).
  The two remaining unguarded auto-title UPDATEs — the untitled branch of
  `_auto_name_session` and `add_message`'s no-LLM fallback — now carry the
  PRR-004 guard `AND (title IS NULL OR title = '')`, matching their sibling
  writes. A rename committed between the title read and the write survives.
  The manual-rename endpoint itself remains (by design) unconditional.
- **A "remember ..." directive is idempotent** (T1-32-S-08). The engine now
  looks up `MemoryStore.find_memory_by_content(content, source="chat",
  vault_id)` before inserting: a directive re-sent after a cancelled turn (or
  repeated in a later turn) confirms the existing memory instead of storing a
  duplicate. The idempotency key is content + source + vault — at least as
  strong as one-per-durable-turn. Two truly concurrent same-directive turns
  can still race past the check; the stronger unique-index guarantee belongs
  to the epic's deferred schema lane.
- **Canvas range edits refuse degenerate model replies** (TQ-instruction-batch-3-01).
  A reply cut off by the output token budget (`finish_reason == "length"`,
  read from the LLM client's `last_metrics`) returns 502
  `canvas_model_truncated` and records no version. An empty (or
  whitespace-only) reply for a non-empty selection returns 422
  `canvas_empty_model_reply` and records no version — a deliberate
  "delete these lines" instruction now 422s; users delete by editing. A reply
  that empties the whole artifact keeps the existing 422
  `canvas_content_required`.
- **The canvas draft no longer loses its last ≤500 ms** (TQ-sibling-batch-05-04).
  The debounced draft write tracks its pending `(artifact, text)` pair and
  flushes it on unmount and on artifact switch (the Composer's
  `flushPendingDraft` pattern). Discard paths (save, restore, reload, and
  reverting to the saved content) cancel the pending write and reset the
  last-written marker, so no pending write can resurrect a cleared draft.

## Guardrails shipped

- `backend/tests/test_a06_contract_guards.py`: a census asserting every
  auto-title UPDATE outside the manual-rename endpoint carries a title guard;
  stream-vs-non-stream done-key parity against `ChatResponse`'s fields; the
  trace flag's iff-contract; the exact canvas failure codes; and the
  remember-directive key across turns.
- `frontend/src/components/canvas/CanvasPage.a06.discardGuard.test.tsx`:
  post-save and post-revert unmounts never resurrect a cleared draft.

## Compatibility

Response changes are additive only (new optional fields; `trace` appears only
when the flag is on). No schema migration, no `chat_completion` interface
change, no public field renames.
