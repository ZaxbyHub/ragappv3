import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

describe("parseSSEStream - evidence candidate events (issue #508)", () => {
  const CANDIDATES = [
    {
      id: "42_default_0",
      file_id: "42",
      filename: "handbook.pdf",
      section: "Maintenance",
      source_label: "S1",
      page_number: 3,
      snippet: "The coolant interval is 500 hours.",
      score: 0.87,
      score_type: "rerank",
      metadata: { page_number: 3 },
    },
  ];

  async function runEvidence(events: object[]) {
    const candidateCalls: unknown[][] = [];
    const contents: string[] = [];
    const errors: string[] = [];
    let completed = false;

    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onEvidenceCandidates: (c) => candidateCalls.push(c),
      onError: (e) => errors.push(e.message),
      onComplete: () => {
        completed = true;
      },
    };

    await parseSSEStream(makeReader(events), callbacks);
    return { candidateCalls, contents, errors, completed };
  }

  it("forwards version-1 evidence candidates to onEvidenceCandidates and keeps streaming", async () => {
    const out = await runEvidence([
      { type: "evidence", version: 1, phase: "candidates", candidates: CANDIDATES },
      { type: "content", content: "answer" },
    ]);

    expect(out.candidateCalls).toHaveLength(1);
    expect(out.candidateCalls[0]).toEqual(CANDIDATES);
    expect(out.contents).toEqual(["answer"]);
    expect(out.completed).toBe(true);
    expect(out.errors).toEqual([]);
  });

  it("forwards an empty candidates array without crashing", async () => {
    const out = await runEvidence([
      { type: "evidence", version: 1, phase: "candidates", candidates: [] },
    ]);

    expect(out.candidateCalls).toHaveLength(1);
    expect(out.candidateCalls[0]).toEqual([]);
    expect(out.errors).toEqual([]);
    expect(out.completed).toBe(true);
  });

  it("ignores evidence events with an unknown version", async () => {
    const out = await runEvidence([
      { type: "evidence", version: 2, phase: "candidates", candidates: CANDIDATES },
    ]);

    expect(out.candidateCalls).toEqual([]);
    expect(out.errors).toEqual([]);
  });

  it("ignores evidence events with no version", async () => {
    const out = await runEvidence([
      { type: "evidence", phase: "candidates", candidates: CANDIDATES },
    ]);

    expect(out.candidateCalls).toEqual([]);
    expect(out.errors).toEqual([]);
  });

  it("ignores evidence events whose candidates are malformed", async () => {
    const out = await runEvidence([
      { type: "evidence", version: 1, phase: "candidates", candidates: "not-an-array" },
    ]);

    expect(out.candidateCalls).toEqual([]);
    expect(out.errors).toEqual([]);
  });

  it("drops a malformed JSON evidence chunk and keeps streaming", async () => {
    const candidateCalls: unknown[][] = [];
    const contents: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onEvidenceCandidates: (c) => candidateCalls.push(c),
      onComplete: () => {},
    };

    await parseSSEStream(
      makeRawReader([
        'data: {"type": "evidence", "version": 1, "candidates": [bro\n\n',
        `data: ${JSON.stringify({ type: "content", content: "after-bad-evidence" })}\n\n`,
        "data: [DONE]\n\n",
      ]),
      callbacks
    );

    expect(candidateCalls).toEqual([]);
    expect(contents).toEqual(["after-bad-evidence"]);
  });

  it("drives the real parser from the shared backend fixture line", async () => {
    const fixturePath = resolve(
      __dirname,
      "../../../backend/tests/fixtures/evidence_candidates_sse_line.txt"
    );
    const line = readFileSync(fixturePath, "utf8").trimEnd();

    const candidateCalls: unknown[][] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: () => {},
      onEvidenceCandidates: (c) => candidateCalls.push(c),
      onComplete: () => {},
    };

    await parseSSEStream(makeRawReader([`data: ${line}\n\n`, "data: [DONE]\n\n"]), callbacks);

    expect(candidateCalls).toHaveLength(1);
    const candidates = candidateCalls[0] as Array<{
      id: string;
      filename: string;
      snippet: string;
      score: number;
    }>;
    expect(candidates).toHaveLength(1);
    expect(candidates[0]).toMatchObject({
      id: "42_default_0",
      filename: "handbook.pdf",
      snippet: "The coolant interval is 500 hours.",
      score: 0.87,
    });
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

describe("parseSSEStream - transport EOF without a completion marker (CHAT-004, issue #507)", () => {
  // A reader that closes (done=true) without the backend JSON done event or
  // the [DONE] marker is an INTERRUPTED stream, not a successful completion.
  // The parser must surface it as a single ChatInterruptedError so the hook
  // can mark the turn retryable — never fire onComplete, never error twice.

  it("fires onError exactly once when the stream ends without a done marker", async () => {
    const contents: string[] = [];
    const errors: Error[] = [];
    let completeCalls = 0;
    const callbacks: ChatStreamCallbacks = {
      onMessage: (c) => contents.push(c),
      onError: (e) => errors.push(e),
      onComplete: () => {
        completeCalls += 1;
      },
    };

    // Partial content frame(s), then the reader closes — no done event, no [DONE].
    await parseSSEStream(makeReader([{ type: "content", content: "partial" }], false), callbacks);

    expect(contents).toEqual(["partial"]);
    expect(errors).toHaveLength(1);
    expect(errors[0].name).toBe("ChatInterruptedError");
    expect(errors[0].message).toBe("The response stream ended before the answer finished.");
    expect(completeCalls).toBe(0);
  });

  it("fires onError when the reader closes mid-frame", async () => {
    const errors: Error[] = [];
    let completeCalls = 0;
    const callbacks: ChatStreamCallbacks = {
      onError: (e) => errors.push(e),
      onComplete: () => {
        completeCalls += 1;
      },
    };

    // A truncated frame (no terminating \n\n) buffered when EOF hits must be
    // treated as an interrupted stream, not silently dropped as a clean end.
    await parseSSEStream(makeRawReader(['data: {"type":"con']), callbacks);

    expect(errors).toHaveLength(1);
    expect(errors[0].name).toBe("ChatInterruptedError");
    expect(completeCalls).toBe(0);
  });
});

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
