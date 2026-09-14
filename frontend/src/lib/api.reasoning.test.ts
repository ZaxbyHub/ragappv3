// frontend/src/lib/api.reasoning.test.ts
// Issue #554 AC4 / AC11: the distinct reasoning SSE event type
// ("reasoning_delta") is forwarded to the new onReasoning callback and never
// leaks into onMessage, while the legacy reasoning/thinking drop-set keeps
// dropping reasoning / reasoning_content / thinking / thinking_content, and
// old consumers without an onReasoning handler keep working (additive event).
import { describe, it, expect } from "vitest";
import { parseSSEStream, type ChatStreamCallbacks } from "./api";

function makeReader(
  events: object[],
  appendDoneMarker = true
): ReadableStreamDefaultReader<Uint8Array> {
  const encoder = new TextEncoder();
  const sseBody = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(sseBody));
      if (appendDoneMarker) {
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      }
      controller.close();
    },
  });
  return stream.getReader();
}

describe("parseSSEStream - reasoning_delta forwarding (issue #554 AC4)", () => {
  it("forwards reasoning_delta events to onReasoning in order and keeps them out of onMessage", async () => {
    const reasoning: string[] = [];
    const contents: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onReasoning: (r) => reasoning.push(r),
      onComplete: () => {},
    };

    await parseSSEStream(
      makeReader([
        { type: "reasoning_delta", text: "Plan " },
        { type: "reasoning_delta", text: "verify the sources." },
        { type: "content", content: "Answer." },
      ]),
      callbacks
    );

    expect(
      reasoning.join(""),
      "AC4-C4: reasoning_delta events must reach the onReasoning callback in order"
    ).toBe("Plan verify the sources.");
    expect(
      contents.join(""),
      "AC4-C4: reasoning_delta text must never leak into onMessage"
    ).toBe("Answer.");
  });

  it("keeps streaming and completes after reasoning_delta events", async () => {
    const contents: string[] = [];
    const reasoning: string[] = [];
    const errors: string[] = [];
    let completed = false;
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onReasoning: (r) => reasoning.push(r),
      onError: (e) => errors.push(e.message),
      onComplete: () => {
        completed = true;
      },
    };

    await parseSSEStream(
      makeReader([
        { type: "reasoning_delta", text: "thinking..." },
        { type: "content", content: "after reasoning" },
      ]),
      callbacks
    );

    expect(completed).toBe(true);
    expect(errors).toEqual([]);
    expect(contents).toEqual(["after reasoning"]);
    expect(reasoning).toEqual(["thinking..."]);
  });

  it("ignores reasoning_delta events with missing or non-string text without crashing", async () => {
    const reasoning: string[] = [];
    const contents: string[] = [];
    const errors: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onReasoning: (r) => reasoning.push(r),
      onError: (e) => errors.push(e.message),
      onComplete: () => {},
    };

    await parseSSEStream(
      makeReader([
        { type: "reasoning_delta" },
        { type: "reasoning_delta", text: 42 },
        { type: "reasoning_delta", text: "well-formed" },
        { type: "content", content: "ok" },
      ]),
      callbacks
    );

    expect(
      reasoning,
      "AC4-C4: only well-formed reasoning_delta text may reach onReasoning"
    ).toEqual(["well-formed"]);
    expect(contents).toEqual(["ok"]);
    expect(errors).toEqual([]);
  });
});

describe("parseSSEStream - compat: legacy suppression and old consumers (issue #554 AC11)", () => {
  it("compat: legacy reasoning/thinking event types stay dropped and never reach onReasoning", async () => {
    const reasoning: string[] = [];
    const contents: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onReasoning: (r) => reasoning.push(r),
      onComplete: () => {},
    };

    await parseSSEStream(
      makeReader([
        { type: "reasoning", content: "secret one" },
        { type: "reasoning_content", content: "secret two" },
        { type: "thinking", content: "secret three" },
        { type: "thinking_content", content: "secret four" },
        { type: "content", content: "ok" },
      ]),
      callbacks
    );

    expect(contents.join("")).toBe("ok");
    expect(reasoning).toEqual([]);
  });

  it("compat: an old consumer without onReasoning still completes and receives content", async () => {
    const contents: string[] = [];
    const errors: string[] = [];
    let completed = false;
    // Deliberately NO onReasoning handler: an older caller must treat the new
    // event type as inert (additive rollout, issue #554 AC11).
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onError: (e) => errors.push(e.message),
      onComplete: () => {
        completed = true;
      },
    };

    await parseSSEStream(
      makeReader([
        { type: "reasoning_delta", text: "invisible to this consumer" },
        { type: "content", content: "the answer" },
      ]),
      callbacks
    );

    expect(completed).toBe(true);
    expect(contents).toEqual(["the answer"]);
    expect(errors).toEqual([]);
  });
});
