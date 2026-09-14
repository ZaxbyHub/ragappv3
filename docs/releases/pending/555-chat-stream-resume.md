# 555 — Resumable SSE chat streams: per-turn event log and Last-Event-ID

Issue: #555 (Workstream H, PR 4 of 4; audit finding E01b) · 2026-09-14

## What changed

A client that loses its `/chat/stream` connection mid-answer (proxy drop,
backgrounded tab) can now reconnect and receive exactly the missed frames —
no duplicates, no gaps — instead of losing the remainder of the answer.

- **Per-turn event log (new `chat_stream_events` table).** Every replayable
  SSE frame of a durable chat turn is persisted keyed by
  `(session_id, turn_id, seq)`; the payload is the exact JSON the wire
  carries, so initial and replayed frames are byte-identical. Heartbeats are
  transport comments and are never logged.
- **Generation decoupled from the connection.** On the durable path the
  generation runs in a per-turn producer task; HTTP connections are readers
  over the event log. A dropped client no longer stops the generation or
  finalizes the turn `interrupted` — the turn completes server-side and the
  full answer lands in history. The #553 cancellation-safe finalize is
  preserved and now triggers only on real cancellation (server shutdown).
- **`Last-Event-ID` resume.** The route reads the SSE `Last-Event-ID` header
  (400 on a malformed value). A reconnecting durable turn replays exactly the
  frames after the client's last id and follows the live remainder.
  Frames are emitted with `id:` fields via `fastapi.sse.format_sse_event`
  (pinned FastAPI 0.141.1). Replay is application work; the framework does
  not provide it.
- **Frontend auto-resume.** `chatStream` (fetch-based client — no
  `EventSource` auto-resend) tracks the last received event id and re-POSTs
  the same body with `Last-Event-ID` on a mid-answer break (bounded: 3
  attempts, backoff). Non-durable calls and pre-`turn_id` clients keep the
  previous behavior (interruption surfaces immediately; the hook marks the
  turn retryable).
- **Scope honesty.** Only the connection-drop case is claimed. The
  `kill -9` process-resume DoD test ships skip-gated on I4 (#559): until a
  generation can outlive its request, a restarted process replays persisted
  frames and never fabricates a completion.

## Deliberate divergence from repo precedent

#516 (Draft Room) chose invalidate-and-refetch over replay for the same
class of defect. Chat takes true replay because a reconnect must not
re-fetch and re-render the whole transcript, and because frames are already
persisted server-side for durability — the reader is a tail over data that
exists anyway. Draft Room keeps its refetch contract; the divergence is
per-surface, not a precedent shift.

## Rollout and compatibility

- Additive table (`chat_stream_events`); no change to the `chat_messages`
  terminal-state contract beyond the disconnect outcome above; no data
  migration to undo — rollback is a plain revert (the table becomes unused).
- Clients that never send `Last-Event-ID` receive the full normal stream.
- An admission (CHAT capacity) lease now spans the generation's real
  lifetime instead of a single connection's; a dropped client's turn
  consumes a slot until it finishes (bounded by generation time).
- Retention: a session's older turns' event rows are purged when a new turn
  starts; rows older than a day are dropped opportunistically.
- Event-log writes are guarded and never fail the stream; a fully
  unavailable database degrades to a clean empty stream (HTTP 200).

## Tests

Backend: `tests/test_chat_stream_replay.py` (8), durability suite updated +
extended (14, including a producer-shutdown interrupted-finalize test and a
broken-pool degradation test), `tests/test_chat_stream_process_kill.py`
(skip-gated on I4/#559), schema-drift guard bumped 70 → 71.
Frontend: `src/lib/api/__tests__/sessions.reconnect.test.ts` (reconnect
contract + id-parsing units).
