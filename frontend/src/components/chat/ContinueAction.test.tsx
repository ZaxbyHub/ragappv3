// frontend/src/components/chat/ContinueAction.test.tsx
// Issue #573 (AC2) — acceptance check C2: "continue generation" when the
// model stopped because it hit the length limit (finish_reason === "length").
//
// DISCRIMINATING check: expected RED at base commit ae2e15a0 — the backend
// already sends llm_metrics.finish_reason on the SSE done event but the
// frontend drops it (sessions.ts done handler), ChatStreamCallbacks has no
// callback for it, Message has no finishReason field, and no ContinueAction
// component exists. GREEN once the plumbing below ships.
//
// Frozen contracts:
//   (a) SSE layer (frontend/src/lib/api/core.ts + sessions.ts):
//         ChatStreamCallbacks gains
//           onFinishReason?: (reason: string) => void
//         parseSSEStream fires it exactly once with the done event's
//         llm_metrics.finish_reason (when present), before onComplete.
//         A done frame without llm_metrics.finish_reason fires nothing.
//         (Message gains finishReason?: string so the UI can branch on it —
//         the store field itself is exercised indirectly through (b)+(c).)
//   (b) Component: frontend/src/components/chat/ContinueAction.tsx
//         Export: ContinueAction
//         Props:  {
//                   content: string;   // truncated partial assistant content
//                   onContinue: (payload: { content: string }) => void;
//                 }
//         Renders a button whose accessible name matches /continue/i; the
//         message row renders it when finishReason === "length"; clicking
//         calls onContinue exactly once with { content } (the truncated
//         context payload the continuation resumes from).
//   (c) Wiring: useSendMessage.ts or TranscriptPane.tsx references
//       ContinueAction (source-scan guardrail).

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { parseSSEStream, type ChatStreamCallbacks } from "@/lib/api";
import { ContinueAction } from "./ContinueAction";

const USE_SEND_MESSAGE_PATH = resolve(__dirname, "../../hooks/useSendMessage.ts");
const TRANSCRIPT_PANE_PATH = resolve(__dirname, "TranscriptPane.tsx");

// --- SSE driver (established api.sse.test.ts pattern) -----------------------

function makeReader(events: object[]): ReadableStreamDefaultReader<Uint8Array> {
  const encoder = new TextEncoder();
  const sseBody = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(sseBody));
      controller.close();
    },
  });
  return stream.getReader();
}

describe("parseSSEStream surfaces llm_metrics.finish_reason (issue #573 AC2 / C2a)", () => {
  it("fires onFinishReason with 'length' from the done frame's llm_metrics, before onComplete", async () => {
    const finishReasons: string[] = [];
    const order: string[] = [];
    let completed = false;

    const callbacks = {
      onMessage: () => {},
      onFinishReason: (reason: string) => {
        finishReasons.push(reason);
        order.push("finishReason");
      },
      onComplete: () => {
        completed = true;
        order.push("complete");
      },
    } as ChatStreamCallbacks;

    await parseSSEStream(
      makeReader([
        { type: "content", content: "truncated partial answer" },
        {
          type: "done",
          sources: [],
          turn_id: "turn-1",
          llm_metrics: { finish_reason: "length", reasoning_duration_ms: 5 },
        },
      ]),
      callbacks
    );

    expect(
      finishReasons,
      "the done event's llm_metrics.finish_reason==='length' must surface via onFinishReason (issue #573 AC2)"
    ).toEqual(["length"]);
    expect(completed).toBe(true);
    expect(order.indexOf("finishReason")).toBeLessThan(order.indexOf("complete"));
  });

  it("does not fire onFinishReason when the done frame omits llm_metrics.finish_reason", async () => {
    const finishReasons: string[] = [];
    const callbacks = {
      onMessage: () => {},
      onFinishReason: (reason: string) => finishReasons.push(reason),
      onComplete: () => {},
    } as ChatStreamCallbacks;

    await parseSSEStream(
      makeReader([
        { type: "content", content: "answer" },
        { type: "done", sources: [], llm_metrics: { reasoning_duration_ms: 5 } },
      ]),
      callbacks
    );

    expect(finishReasons).toEqual([]);
  });
});

describe("ContinueAction component contract (issue #573 AC2 / C2b)", () => {
  it("renders a Continue control and calls onContinue with the truncated content payload on click", () => {
    const onContinue = vi.fn();
    render(
      <ContinueAction
        content="The model hit the token limit mid-sent"
        onContinue={onContinue}
      />
    );

    const button = screen.getByRole("button", { name: /continue/i });
    expect(button).toBeInTheDocument();

    fireEvent.click(button);

    expect(onContinue).toHaveBeenCalledTimes(1);
    expect(onContinue).toHaveBeenCalledWith({
      content: "The model hit the token limit mid-sent",
    });
  });
});

describe("ContinueAction wiring (issue #573 AC2 / C2c)", () => {
  it("TranscriptPane renders <ContinueAction onContinue={...}> backed by a handler that resends via sendDirect", () => {
    const hookSource = readFileSync(USE_SEND_MESSAGE_PATH, "utf-8");
    const paneSource = readFileSync(TRANSCRIPT_PANE_PATH, "utf-8");

    expect(
      paneSource.includes("<ContinueAction"),
      "TranscriptPane.tsx must render <ContinueAction ...> so the action is reachable in the real transcript (issue #573 AC2 wiring)"
    ).toBe(true);
    expect(
      paneSource.includes("onContinue={"),
      "TranscriptPane.tsx must pass an onContinue handler to ContinueAction (issue #573 AC2 wiring)"
    ).toBe(true);
    // The handler itself (wherever it lives) must route the truncated content
    // back through the direct-send primitive rather than being a dead import.
    const combined = hookSource + "\n" + paneSource;
    expect(
      /handleContinue[\s\S]{0,800}?sendDirect\(/.test(combined) ||
        /continue[A-Z]\w*[\s\S]{0,800}?sendDirect\(/.test(combined),
      "a continue handler must call sendDirect so clicking Continue actually resends the truncated content as context (issue #573 AC2 wiring)"
    ).toBe(true);
  });
});
