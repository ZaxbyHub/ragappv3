import { describe, it, expect } from "vitest";
import { parseSSEStream, type ChatStreamCallbacks } from "./sessions";
import type { Source } from "./core";

// Issue #1070: the SSE sources mapper must PREFER a source's own score_type
// (currently only "fts", the per-source kind for a real LanceDB BM25 `_score`
// on the no-`_distance` path) over the done payload's global typing, and fall
// back to the global default — then to "distance" — for sources without one.
// Overwriting every source with the global kind would misread an FTS score
// through the lower-is-better distance polarity.

function makeReader(events: object[]): ReadableStreamDefaultReader<Uint8Array> {
  const encoder = new TextEncoder();
  const sseBody = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(sseBody));
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
  return stream.getReader();
}

async function runSourcesEvent(event: object): Promise<Source[]> {
  const sourcesCalls: Source[][] = [];
  const callbacks: ChatStreamCallbacks = {
    onSources: (s) => sourcesCalls.push(s),
    onComplete: () => {},
  };
  await parseSSEStream(makeReader([event]), callbacks);
  expect(sourcesCalls).toHaveLength(1);
  return sourcesCalls[0];
}

function wireSource(overrides: Partial<Source>): Source {
  return {
    id: "42_default_0",
    file_id: "42",
    filename: "handbook.pdf",
    snippet: "The coolant interval is 500 hours.",
    ...overrides,
  };
}

describe("parseSSEStream - per-source score_type precedence (issue #1070)", () => {
  it("keeps the source's own 'fts' kind over the global 'distance' typing", async () => {
    const sources = await runSourcesEvent({
      type: "done",
      score_type: "distance",
      sources: [
        wireSource({ id: "a", score: 0.62, score_type: "fts" }),
        wireSource({ id: "b", score: 0.31 }),
      ],
    });
    expect(sources[0].score_type).toBe("fts");
    // The sibling without a per-source kind keeps the global typing.
    expect(sources[1].score_type).toBe("distance");
  });

  it("keeps the source's own 'fts' kind over the global 'rerank' typing", async () => {
    const sources = await runSourcesEvent({
      type: "done",
      score_type: "rerank",
      sources: [wireSource({ score: 0.62, score_type: "fts" })],
    });
    expect(sources[0].score_type).toBe("fts");
  });

  it("applies the global score_type to sources without their own kind", async () => {
    const sources = await runSourcesEvent({
      type: "done",
      score_type: "rerank",
      sources: [wireSource({ score: 0.9 })],
    });
    expect(sources[0].score_type).toBe("rerank");
  });

  it("defaults the global typing to 'distance' when the payload omits it", async () => {
    const sources = await runSourcesEvent({
      type: "done",
      sources: [wireSource({ score: 0.4 })],
    });
    expect(sources[0].score_type).toBe("distance");
  });
});
