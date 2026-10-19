/**
 * Tests for granular store selectors used by RightPane to avoid
 * re-rendering on streaming token growth (P1.6).
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  useChatStore,
  useLastCompletedAssistantSources,
  useLastCompletedAssistantCitationConfidence,
  useLastUserContent,
  useSourcesForSourceId,
  useCompletedAssistantMessageIdsKey,
  parseCompletedAssistantIds,
} from "./useChatStore";

beforeEach(() => {
  useChatStore.setState({
    messageIds: [],
    messagesById: {},
    streamingMessageId: null,
    input: "",
    isStreaming: false,
    abortFn: null,
    inputError: null,
    expandedSources: new Set(),
    activeChatId: null,
  });
});

describe("granular RightPane selectors", () => {
  it("useLastCompletedAssistantSources skips the actively streaming message", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a-old",
      role: "assistant",
      content: "old",
      sources: [{ id: "s-old", filename: "old.pdf" }],
    });
    s.addMessage({
      id: "a-stream",
      role: "assistant",
      content: "streaming",
      sources: [{ id: "s-stream", filename: "live.pdf" }],
    });
    s.setStreamingMessageId("a-stream");

    const { result } = renderHook(() => useLastCompletedAssistantSources());
    expect(result.current?.[0]?.id).toBe("s-old");
  });

  it("useLastCompletedAssistantSources falls back to streaming once it ends", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a",
      role: "assistant",
      content: "live",
      sources: [{ id: "s1", filename: "live.pdf" }],
    });
    s.setStreamingMessageId("a");
    const { result, rerender } = renderHook(() =>
      useLastCompletedAssistantSources()
    );
    expect(result.current).toBeUndefined();

    s.setStreamingMessageId(null);
    rerender();
    expect(result.current?.[0]?.id).toBe("s1");
  });

  it("useLastUserContent returns the most recent user message", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u1", role: "user", content: "first" });
    s.addMessage({ id: "a1", role: "assistant", content: "ok" });
    s.addMessage({ id: "u2", role: "user", content: "second" });
    const { result } = renderHook(() => useLastUserContent());
    expect(result.current).toBe("second");
  });

  it("useSourcesForSourceId returns the parent message's sources", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a1",
      role: "assistant",
      content: "first answer",
      sources: [{ id: "src-1", filename: "first.pdf" }],
    });
    s.addMessage({
      id: "a2",
      role: "assistant",
      content: "second answer",
      sources: [{ id: "src-2", filename: "second.pdf" }],
    });
    const { result } = renderHook(() => useSourcesForSourceId("src-1"));
    expect(result.current?.[0]?.id).toBe("src-1");
  });

  it("useCompletedAssistantMessageIdsKey is stable across token growth", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({ id: "a-done", role: "assistant", content: "done" });
    s.addMessage({ id: "a-live", role: "assistant", content: "" });
    s.setStreamingMessageId("a-live");

    const { result, rerender } = renderHook(() =>
      useCompletedAssistantMessageIdsKey()
    );
    const before = result.current;
    expect(parseCompletedAssistantIds(before)).toEqual(["a-done"]);

    // Streaming token growth on a-live should NOT change the key.
    s.appendToMessage("a-live", "Hello");
    rerender();
    expect(result.current).toBe(before);

    s.appendToMessage("a-live", " world");
    rerender();
    expect(result.current).toBe(before);
  });

  it("useCompletedAssistantMessageIdsKey grows when streaming completes", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({ id: "a", role: "assistant", content: "" });
    s.setStreamingMessageId("a");

    const { result, rerender } = renderHook(() =>
      useCompletedAssistantMessageIdsKey()
    );
    expect(parseCompletedAssistantIds(result.current)).toEqual([]);

    s.setStreamingMessageId(null);
    rerender();
    expect(parseCompletedAssistantIds(result.current)).toEqual(["a"]);
  });
});

describe("useLastCompletedAssistantCitationConfidence (#965)", () => {
  it("returns the most recent completed assistant message's score record", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a-old",
      role: "assistant",
      content: "old",
      citationConfidence: { W1: 0.25 },
    });
    s.addMessage({
      id: "a-new",
      role: "assistant",
      content: "new",
      citationConfidence: { W1: 0.5, K1: 0 },
    });

    const { result } = renderHook(() =>
      useLastCompletedAssistantCitationConfidence()
    );
    expect(result.current).toEqual({ W1: 0.5, K1: 0 });
  });

  it("skips the actively streaming message", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a-live",
      role: "assistant",
      content: "streaming",
      citationConfidence: { W9: 0.9 },
    });
    s.setStreamingMessageId("a-live");

    const { result } = renderHook(() =>
      useLastCompletedAssistantCitationConfidence()
    );
    expect(result.current).toBeUndefined();
  });

  it("stops the scan at an explicitly empty record so older scores never leak", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({
      id: "a-old",
      role: "assistant",
      content: "old",
      citationConfidence: { W1: 0.25 },
    });
    s.addMessage({ id: "a-new", role: "assistant", content: "new", citationConfidence: {} });

    const { result } = renderHook(() =>
      useLastCompletedAssistantCitationConfidence()
    );
    expect(result.current).toEqual({});
  });

  it("is undefined when no completed assistant message carries scores", () => {
    const s = useChatStore.getState();
    s.addMessage({ id: "u", role: "user", content: "q" });
    s.addMessage({ id: "a", role: "assistant", content: "no scores" });

    const { result } = renderHook(() =>
      useLastCompletedAssistantCitationConfidence()
    );
    expect(result.current).toBeUndefined();
  });
});
