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

async function run(events: object[]) {
  const contents: string[] = [];
  const sourcesCalls: unknown[][] = [];
  const memoryCalls: unknown[][] = [];
  const validationCalls: unknown[] = [];
  const finalContentCalls: string[] = [];
  const errors: string[] = [];
  let completed = false;
  let finalContentBeforeComplete = false;

  const callbacks: ChatStreamCallbacks = {
    onMessage: (c) => contents.push(c),
    onSources: (s) => sourcesCalls.push(s),
    onMemories: (m) => memoryCalls.push(m),
    onCitationValidation: (v) => validationCalls.push(v),
    onFinalContent: (c) => {
      finalContentCalls.push(c);
      if (!completed) finalContentBeforeComplete = true;
    },
    onError: (e) => errors.push(e.message),
    onComplete: () => {
      completed = true;
    },
  };

  await parseSSEStream(makeReader(events), callbacks);
  return {
    contents,
    sourcesCalls,
    memoryCalls,
    validationCalls,
    finalContentCalls,
    finalContentBeforeComplete,
    errors,
    completed,
  };
}

describe("parseSSEStream — reasoning suppression", () => {
  it("ignores events typed as 'reasoning'", async () => {
    const out = await run([
      { type: "reasoning", content: "secret thought" },
      { type: "content", content: "visible answer" },
    ]);
    expect(out.contents.join("")).toBe("visible answer");
    expect(out.contents.join("")).not.toContain("secret thought");
  });

  it("ignores 'thinking_content' typed events", async () => {
    const out = await run([
      { type: "thinking_content", content: "hidden" },
      { type: "content", content: "real" },
    ]);
    expect(out.contents.join("")).toBe("real");
  });

  it("does not stream events whose type is reasoning_content even if .content is present", async () => {
    const out = await run([
      { type: "reasoning_content", content: "leak attempt" },
      { type: "content", content: "ok" },
    ]);
    expect(out.contents.join("")).toBe("ok");
  });

  it("ignores 'thinking' events", async () => {
    const out = await run([
      { type: "thinking", content: "internal" },
      { type: "content", content: "answer" },
    ]);
    expect(out.contents.join("")).toBe("answer");
  });
});

describe("parseSSEStream — memories", () => {
  it("parses memories_used into onMemories callback (structured shape)", async () => {
    const out = await run([
      { type: "content", content: "Per [M1], here." },
      {
        type: "done",
        sources: [],
        memories_used: [
          {
            id: "42",
            memory_label: "M1",
            content: "User likes lists.",
            category: "preference",
          },
        ],
        score_type: "distance",
      },
    ]);
    expect(out.memoryCalls.length).toBe(1);
    const mem = out.memoryCalls[0][0] as { memory_label: string; content: string; id: string };
    expect(mem.memory_label).toBe("M1");
    expect(mem.content).toBe("User likes lists.");
    expect(mem.id).toBe("42");
  });

  it("normalizes legacy bare-string memories_used into structured records", async () => {
    const out = await run([
      {
        type: "done",
        sources: [],
        memories_used: ["legacy memory text"],
        score_type: "distance",
      },
    ]);
    const mem = out.memoryCalls[0][0] as { memory_label: string; content: string };
    expect(mem.memory_label).toBe("M1");
    expect(mem.content).toBe("legacy memory text");
  });
});

describe("parseSSEStream — citation_validation", () => {
  it("forwards citation_validation events to onCitationValidation", async () => {
    const out = await run([
      {
        type: "done",
        sources: [],
        memories_used: [],
        score_type: "distance",
        citation_validation: {
          valid: ["S1"],
          invalid: ["S99"],
          uncited_factual_warning: false,
          has_evidence: true,
        },
      },
    ]);
    expect(out.validationCalls.length).toBe(1);
    const cv = out.validationCalls[0] as { valid: string[]; invalid: string[] };
    expect(cv.invalid).toContain("S99");
    expect(cv.valid).toContain("S1");
  });
});

describe("parseSSEStream — repaired_content (#217)", () => {
  it("forwards repaired_content to onFinalContent before completing", async () => {
    const out = await run([
      { type: "content", content: "Claim [S99] here." },
      {
        type: "done",
        sources: [],
        memories_used: [],
        score_type: "distance",
        citation_validation: { valid: [], invalid: ["S99"], uncited_factual_warning: false, has_evidence: true },
        repaired_content: "Claim here.",
      },
    ]);
    expect(out.finalContentCalls).toEqual(["Claim here."]);
    expect(out.finalContentBeforeComplete).toBe(true);
    expect(out.completed).toBe(true);
  });

  it("does not call onFinalContent when no repaired_content is present", async () => {
    const out = await run([
      { type: "content", content: "Clean answer [S1]." },
      { type: "done", sources: [], memories_used: [], score_type: "distance" },
    ]);
    expect(out.finalContentCalls).toEqual([]);
  });
});

describe("parseSSEStream - regression: backend done completes once (F-001)", () => {
  it("calls onComplete once for the backend JSON done event without requiring [DONE]", async () => {
    let completeCalls = 0;
    const callbacks: ChatStreamCallbacks = {
      onMessage: () => {},
      onComplete: () => {
        completeCalls += 1;
      },
    };

    await parseSSEStream(
      makeReader([{ type: "content", content: "ok" }, { type: "done" }], false),
      callbacks
    );

    expect(completeCalls).toBe(1);
  });
});

// Build a reader from raw string chunks (NOT JSON.stringify'd). Used to feed
// malformed SSE data the makeReader() helper cannot express.
function makeRawReader(chunks: string[]): ReadableStreamDefaultReader<Uint8Array> {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return stream.getReader();
}

describe("parseSSEStream - malformed input handling (TEST-FE-003)", () => {
  // The parser is expected to drop a malformed (non-JSON) data chunk and keep
  // streaming rather than throwing or aborting the connection. See
  // sessions.ts parseSSEStream's bare-catch around JSON.parse.

  it("drops a non-JSON data chunk and continues streaming valid events", async () => {
    const contents: string[] = [];
    let completed = false;
    const errors: string[] = [];

    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onComplete: () => {
        completed = true;
      },
      onError: (e) => errors.push(e.message),
    };

    // A garbage data frame, then a valid content event, then [DONE].
    await parseSSEStream(
      makeRawReader([
        "data: this is not json\n\n",
        `data: ${JSON.stringify({ type: "content", content: "after-garbage" })}\n\n`,
        "data: [DONE]\n\n",
      ]),
      callbacks
    );

    // The valid event after the garbage must still be delivered.
    expect(contents).toContain("after-garbage");
    // The stream must complete normally (not abort on the bad chunk).
    expect(completed).toBe(true);
    // The malformed chunk must not surface as a user-visible error.
    expect(errors).toEqual([]);
  });

  it("drops a data chunk with the wrong JSON shape but keeps well-formed ones", async () => {
    const contents: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onComplete: () => {},
    };

    await parseSSEStream(
      makeRawReader([
        `data: ${JSON.stringify({ unrelated: "shape" })}\n\n`,
        `data: ${JSON.stringify({ type: "content", content: "good" })}\n\n`,
        "data: [DONE]\n\n",
      ]),
      callbacks
    );

    expect(contents).toEqual(["good"]);
  });

  it("completes when a malformed chunk precedes the [DONE] marker", async () => {
    let completed = false;
    const callbacks: ChatStreamCallbacks = {
      onMessage: () => {},
      onComplete: () => {
        completed = true;
      },
    };

    await parseSSEStream(
      makeRawReader(["data: {broken\n\n", "data: [DONE]\n\n"]),
      callbacks
    );

    expect(completed).toBe(true);
  });

  it("handles an event split across multiple stream chunks", async () => {
    const contents: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onComplete: () => {},
    };

    const eventJson = JSON.stringify({ type: "content", content: "split-ok" });
    // The same logical SSE frame arrives in two byte chunks: "data: <partial>"
    // then "<rest>\n\n" — the parser must buffer across reads and reassemble.
    await parseSSEStream(
      makeRawReader([
        `data: ${eventJson.slice(0, 10)}`,
        `${eventJson.slice(10)}\n\n`,
        "data: [DONE]\n\n",
      ]),
      callbacks
    );

    expect(contents).toEqual(["split-ok"]);
  });
});
