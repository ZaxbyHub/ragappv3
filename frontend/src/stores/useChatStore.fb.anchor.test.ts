// frontend/src/stores/useChatStore.fb.anchor.test.ts
// PR #835 feedback PRR-012c: the a07 citationAnchor check only exercises the
// anchored branch (clickedMessageId set). This pins the selector's two
// branches against the real store: un-anchored lookups keep the first-match
// fallback (the documented degrade path for selections whose anchoring
// message left the store), and an anchor pointing at a removed message
// degrades to the same fallback instead of throwing.

import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useChatStore, useSourcesForSourceId } from "./useChatStore";
import type { Message } from "./useChatStore";

const msg1 = {
  id: "m1",
  role: "assistant",
  content: "first",
  sources: [
    { id: "x", filename: "x-title.pdf" },
    { id: "y", filename: "y-title.pdf" },
  ],
} as unknown as Message;
const msg2 = {
  id: "m2",
  role: "assistant",
  content: "second",
  sources: [
    { id: "x", filename: "x-title.pdf" },
    { id: "z", filename: "z-title.pdf" },
  ],
} as unknown as Message;

function seed() {
  useChatStore.setState({
    messageIds: ["m1", "m2"],
    messagesById: { m1: msg1, m2: msg2 },
    streamingMessageId: null,
  });
}

describe("useSourcesForSourceId anchor branches (fb PRR-012c)", () => {
  beforeEach(() => {
    seed();
  });

  it("un-anchored lookup falls back to the first citing message", () => {
    const { result } = renderHook(() => useSourcesForSourceId("x"));
    expect(result.current).toBe(msg1.sources);
    expect(result.current?.some((s) => s.id === "y")).toBe(true);
  });

  it("anchored lookup prefers the clicked message (the a07 behavior)", () => {
    const { result } = renderHook(() => useSourcesForSourceId("x", "m2"));
    expect(result.current).toBe(msg2.sources);
    expect(result.current?.some((s) => s.id === "z")).toBe(true);
  });

  it("an anchor whose message was removed degrades to first-match, not a crash", () => {
    useChatStore.setState((s) => {
      const messagesById = { ...s.messagesById };
      delete messagesById.m2;
      return {
        messageIds: s.messageIds.filter((id) => id !== "m2"),
        messagesById,
      };
    });
    const { result } = renderHook(() => useSourcesForSourceId("x", "m2"));
    expect(result.current).toBe(msg1.sources);
  });

  it("an anchor message that does not cite the chunk falls back to first-match", () => {
    const { result } = renderHook(() => useSourcesForSourceId("x", "nonexistent"));
    expect(result.current).toBe(msg1.sources);
  });
});
