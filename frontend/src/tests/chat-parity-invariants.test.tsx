// frontend/src/tests/chat-parity-invariants.test.tsx
// Issue #573 (AC9) — acceptance check C9 (PRESERVING): structural invariants
// the #573 chat-UI work must not disturb. Expected GREEN at base commit
// ae2e15a0; any RED is a regression from the implementation.
//
// Source-scan guardrails (jsdom rendering of TranscriptPane would need the
// full store graph for no extra coverage — the exact container markup is
// pinned textually instead):
//   1. TranscriptPane's transcript container keeps role="log" +
//      aria-live="polite" (with aria-relevant="additions" and the
//      "Chat messages" label).
//   2. The fork data model is untouched: backend/app/models/database.py
//      still creates the unique index idx_chat_messages_session_turn_role on
//      (session_id, turn_id, role) and still declares the chat_sessions
//      columns forked_from_session_id / fork_message_index.
//   3. TranscriptPane still imports forkChatSession / truncateChatSession
//      from "@/lib/api" and still calls them with the same shapes
//      (forkChatSession(id, index), truncateChatSession(id, keepSeq)).

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const FRONTEND_SRC = resolve(__dirname, "..");
const TRANSCRIPT_PANE = resolve(FRONTEND_SRC, "components/chat/TranscriptPane.tsx");
const REPO_ROOT = resolve(FRONTEND_SRC, "..", "..");
const DATABASE_PY = resolve(REPO_ROOT, "backend", "app", "models", "database.py");

describe("chat parity invariants (issue #573 AC9 / C9)", () => {
  it("TranscriptPane transcript container exposes role=\"log\" and aria-live", () => {
    const source = readFileSync(TRANSCRIPT_PANE, "utf-8");

    expect(
      source.includes('role="log"'),
      "TranscriptPane must keep the transcript container's role=\"log\" (issue #573 AC9)"
    ).toBe(true);
    expect(
      source.includes('aria-live="polite"'),
      "TranscriptPane must keep aria-live=\"polite\" on the transcript container (issue #573 AC9)"
    ).toBe(true);
    expect(
      source.includes('aria-relevant="additions"'),
      "TranscriptPane must keep aria-relevant=\"additions\" on the transcript container (issue #573 AC9)"
    ).toBe(true);
    expect(
      source.includes('aria-label="Chat messages"'),
      "TranscriptPane must keep the \"Chat messages\" label on the transcript container (issue #573 AC9)"
    ).toBe(true);
  });

  it("database.py keeps the unique turn-role index on chat_messages", () => {
    const source = readFileSync(DATABASE_PY, "utf-8");

    expect(
      source.includes("idx_chat_messages_session_turn_role"),
      "backend/app/models/database.py must keep idx_chat_messages_session_turn_role (fork/turn data model untouched, issue #573 AC9)"
    ).toBe(true);
    const indexMatch = source.match(
      /CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_session_turn_role\s+ON chat_messages\(([^)]*)\)/
    );
    expect(
      indexMatch,
      "the idx_chat_messages_session_turn_role CREATE UNIQUE INDEX statement must survive (issue #573 AC9)"
    ).not.toBeNull();
    expect(indexMatch![1].replace(/\s+/g, "")).toBe("session_id,turn_id,role");
  });

  it("database.py keeps the chat_sessions fork columns", () => {
    const source = readFileSync(DATABASE_PY, "utf-8");

    expect(
      source.includes("forked_from_session_id"),
      "chat_sessions must keep forked_from_session_id (issue #573 AC9)"
    ).toBe(true);
    expect(
      source.includes("fork_message_index"),
      "chat_sessions must keep fork_message_index (issue #573 AC9)"
    ).toBe(true);
  });

  it("TranscriptPane still imports and calls forkChatSession / truncateChatSession with the same shapes", () => {
    const source = readFileSync(TRANSCRIPT_PANE, "utf-8");

    expect(source).toContain(
      'forkChatSession, truncateChatSession'
    );

    // Call shapes: forkChatSession(parseInt(activeChatId), msgIndex) and
    // truncateChatSession(parseInt(activeChatId), <keep-seq expression>).
    const forkCalls = source.match(/forkChatSession\(\s*parseInt\(activeChatId\)\s*,\s*msgIndex\s*\)/g) ?? [];
    expect(
      forkCalls.length,
      "forkChatSession must keep its (sessionId, messageIndex) call shape (issue #573 AC9)"
    ).toBeGreaterThanOrEqual(1);

    const truncateCalls = source.match(/truncateChatSession\(\s*parseInt\(activeChatId\)\s*,/g) ?? [];
    expect(
      truncateCalls.length,
      "truncateChatSession must keep its (sessionId, keepSeq) call shape (issue #573 AC9)"
    ).toBeGreaterThanOrEqual(1);
  });
});
